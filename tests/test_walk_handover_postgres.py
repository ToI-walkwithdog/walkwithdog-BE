"""Issue #93 on disposable PostgreSQL: migration, atomic writes and competing requests."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query

from test_date_slot_booking_postgres import DateSlotMigrationFixture, IsolatedPostgres, TEST_URL
from test_walk_handover import COMPLETE, ISSUE, HandoverFixture
from app import walk_service
from app.database import Base
from app.models import VolunteerProfile, Walk
from app.walk_models import WalkHandover


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class HandoverMigrationTests(DateSlotMigrationFixture, unittest.TestCase):
    def migrated(self):
        engine = self.setup_predecessor()
        for name in ("028_date_slot_booking.sql", "029_reservation_cancellation.sql",
                     "030_shelter_code_verification.sql", "031_walk_marker_idempotency.sql"):
            self.apply(engine, name)
        with engine.begin() as connection:
            self.seed_legacy(connection)
            connection.exec_driver_sql(
                "INSERT INTO walks (id, walk_request_id, status, end_reason, started_at, ended_at, "
                "duration_seconds, total_distance_m) VALUES "
                "(1, 1, 'COMPLETED', 'USER', '2030-01-07 00:00Z', '2030-01-07 00:01Z', 60, 123)"
            )
        self.apply(engine, "032_walk_handovers.sql")
        return engine

    def test_032_preserves_legacy_values_and_has_same_catalog_as_orm(self):
        migrated = self.migrated()
        self.apply(migrated, "032_walk_handovers.sql")
        with migrated.connect() as connection:
            legacy = connection.exec_driver_sql(
                "SELECT status, end_reason, duration_seconds, total_distance_m, handover_required FROM walks WHERE id = 1"
            ).one()
            self.assertEqual(tuple(legacy), ("COMPLETED", "USER", 60, 123, False))
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM walk_handovers").scalar_one(), 0)

        orm = self.isolated_engine()
        Base.metadata.create_all(orm)

        def catalog(engine):
            with engine.connect() as connection:
                inspector = inspect(connection)
                walk_field = next(c for c in inspector.get_columns("walks") if c["name"] == "handover_required")
                cols = {c["name"]: (str(c["type"]), c["nullable"], c["default"])
                        for c in inspector.get_columns("walk_handovers")}
                checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("walk_handovers")}
                keys = inspector.get_foreign_keys("walk_handovers")
                uniques = inspector.get_unique_constraints("walk_handovers")
                return str(walk_field["type"]), walk_field["nullable"], walk_field["default"], cols, checks, keys, uniques

        self.assertEqual(catalog(migrated), catalog(orm))

    def test_032_rejects_duplicate_or_inconsistent_records_through_constraints(self):
        engine = self.migrated()
        statement = """
            INSERT INTO walk_handovers (
                walk_id, volunteer_user_id, handover_result, dog_returned, equipment_returned, condition_shared,
                issue_description, self_confirmed_at, issue_reported_at, level_evaluation_pending_at
            )
            VALUES (
                %(walk_id)s, %(volunteer)s, %(result)s, TRUE, %(equipment)s, TRUE,
                %(description)s, %(confirmed)s, %(reported)s, %(pending)s
            )
        """
        timestamp = "2030-01-07T00:02:00Z"
        complete = dict(walk_id=1, volunteer=1, result="COMPLETE", equipment=True, description=None,
                        confirmed=timestamp, reported=None, pending=timestamp)
        issue = dict(complete, result="ISSUE", equipment=False, description="물품 분실",
                     confirmed=None, reported=timestamp, pending=None)
        cases = [
            (dict(complete, walk_id=999), "walk_handovers_walk_id_fkey"),
            (dict(complete, volunteer=999), "walk_handovers_volunteer_user_id_fkey"),
            (dict(complete, equipment=False), "ck_walk_handover_result"),
            (dict(complete, confirmed=None), "ck_walk_handover_result"),
            (dict(issue, description="   "), "ck_walk_handover_result"),
            (dict(issue, equipment=True), "ck_walk_handover_result"),
            (dict(issue, pending=timestamp), "ck_walk_handover_pending"),
        ]
        for data, constraint in cases:
            with self.subTest(constraint=constraint):
                with self.assertRaises(IntegrityError) as error, engine.begin() as connection:
                    connection.exec_driver_sql(statement, data)
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        with engine.begin() as connection:
            connection.exec_driver_sql(statement, complete)
        with self.assertRaises(IntegrityError) as error, engine.begin() as connection:
            connection.exec_driver_sql(statement, issue)
        self.assertEqual(error.exception.orig.diag.constraint_name, "uq_walk_handovers_walk")


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class WalkHandoverPostgresTests(IsolatedPostgres, HandoverFixture, unittest.TestCase):
    def setUp(self):
        self.setup_handover_api(self.isolated_engine())

    def race(self, first, second, lock_function, *, second_lock=("walks", "FOR UPDATE")):
        """Hold the first Walk lock until the other request reaches its blocking SELECT."""
        original = getattr(walk_service, lock_function)
        held, attempted, release = Event(), Event(), Event()
        table, lock_clause = second_lock

        def hold_first(*args, **kwargs):
            locked = original(*args, **kwargs)
            if not held.is_set():
                held.set()
                if not release.wait(8):
                    raise AssertionError("test did not release the first Walk lock")
            return locked

        def observe_lock_attempt(connection, cursor, statement, parameters, context, executemany):
            if held.is_set() and f"FROM {table}" in statement and statement.rstrip().endswith(lock_clause):
                attempted.set()

        event.listen(self.engine, "before_cursor_execute", observe_lock_attempt)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", observe_lock_attempt)
        with patch.object(walk_service, lock_function, side_effect=hold_first), ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            try:
                self.assertTrue(held.wait(8), "first request did not acquire the Walk lock")
                second_future = pool.submit(second)
                self.assertTrue(attempted.wait(8), "second request did not attempt to lock the Walk")
            finally:
                release.set()
            return first_future.result(timeout=10), second_future.result(timeout=10)

    def test_concurrent_identical_requests_return_one_created_and_one_replay(self):
        self.finish()
        first, second = self.race(self.handover, self.handover, "lock_handover_walk")
        self.assertEqual((first.status_code, second.status_code), (201, 200), (first.text, second.text))
        self.assertEqual(first.json(), second.json())
        row, = self.handover_rows()
        self.assertEqual(row.level_evaluation_pending_at, row.self_confirmed_at)

    def test_concurrent_conflicting_requests_keep_first_receipt(self):
        self.finish()
        first, second = self.race(self.handover, lambda: self.handover(body=ISSUE), "lock_handover_walk")
        self.assertEqual((first.status_code, second.status_code), (201, 409), (first.text, second.text))
        self.assertEqual(self.handover().json(), first.json())
        self.assertEqual(len(self.handover_rows()), 1)

    def test_handover_waits_for_end_to_commit_and_does_not_overwrite_end_metrics(self):
        self.advance(60)
        self.assertEqual(self.upload(self.walk_id, [self.point(0), self.point(1, 30, longitude=0.0003)])
                         .status_code, 200)
        # End holds User FOR UPDATE first. Handover waits at its account guard before it can lock the Walk.
        ended, receipt = self.race(lambda: self.end(self.walk_id), self.handover, "locked_walk",
                                   second_lock=("users", "FOR SHARE"))
        self.assertEqual((ended.status_code, receipt.status_code), (200, 201), (ended.text, receipt.text))
        detail = self.detail(self.walk_id).json()
        self.assertEqual(detail["walk"], ended.json())
        self.assertEqual(detail["handover_status"], "SELF_REPORTED")

    def test_failed_insert_transaction_rolls_back_handover_and_pending_but_keeps_end(self):
        self.advance(60)
        ended = self.finish()
        records = self.rows(Walk), self.rows(VolunteerProfile)
        original = walk_service.flush_new_row

        def fail_after_insert(db, row):
            original(db, row)
            self.assertIsNotNone(row.level_evaluation_pending_at)
            db.connection().exec_driver_sql("SELECT 1 / 0")

        with patch.object(walk_service, "flush_new_row", side_effect=fail_after_insert):
            self.assertEqual(self.handover().status_code, 500)
        self.assertEqual(self.handover_rows(), [])
        self.assertEqual((self.rows(Walk), self.rows(VolunteerProfile)), records)
        detail = self.detail(self.walk_id).json()
        self.assertEqual(detail["walk"], ended)
        self.assertEqual(detail["handover_status"], "NOT_CONFIRMED")
        self.assertEqual(self.handover().status_code, 201)

    def test_unique_constraint_recovery_returns_saved_receipt_after_savepoint_rollback(self):
        self.finish()
        stored = self.handover()
        self.assertEqual(stored.status_code, 201, stored.text)
        original = Query.one_or_none
        reads = []

        def miss_first_handover(query):
            if query.column_descriptions[0]["entity"] is WalkHandover:
                reads.append(1)
                if len(reads) == 1:
                    return None
            return original(query)

        with patch.object(Query, "one_or_none", miss_first_handover):
            replay = self.handover(body=COMPLETE)
        self.assertEqual((replay.status_code, replay.json()), (200, stored.json()))
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(self.handover_rows()), 1)
