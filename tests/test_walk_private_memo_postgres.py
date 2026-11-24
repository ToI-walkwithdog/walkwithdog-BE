"""Issue #95 on disposable PostgreSQL: DDL, concurrent versions and rollback isolation."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError

from test_date_slot_booking_postgres import DateSlotMigrationFixture, IsolatedPostgres, TEST_URL
from test_walk_private_memo import MEMO, PrivateMemoFixture
from app.database import Base
from app.models import VolunteerProfile, Walk
from app.walk_models import WalkHandover, WalkPrivateMemo
from app.walk_review_service import upsert_walk_private_memo


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class PrivateMemoMigrationTests(DateSlotMigrationFixture, unittest.TestCase):
    def test_033_preserves_existing_walks_and_has_same_constraints_as_orm(self):
        migrated = self.setup_predecessor()
        for name in ("028_date_slot_booking.sql", "029_reservation_cancellation.sql",
                     "030_shelter_code_verification.sql", "031_walk_marker_idempotency.sql", "032_walk_handovers.sql"):
            self.apply(migrated, name)
        with migrated.begin() as connection:
            self.seed_legacy(connection)
            connection.exec_driver_sql(
                "INSERT INTO walks (id, walk_request_id, status, end_reason, started_at, ended_at, "
                "duration_seconds, total_distance_m) VALUES "
                "(1, 1, 'COMPLETED', 'USER', '2030-01-07 00:00Z', '2030-01-07 00:01Z', 60, 123)"
            )
            before = connection.exec_driver_sql("SELECT * FROM walks WHERE id = 1").one()

        self.apply(migrated, "033_walk_private_memos.sql")
        insert = """
            INSERT INTO walk_private_memos (walk_id, author_user_id, memo, version)
            VALUES (%(walk)s, %(author)s, %(memo)s, %(version)s)
        """
        valid = dict(walk=1, author=1, memo=MEMO, version=1)
        with migrated.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM walks WHERE id = 1").one(), before)
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM walk_private_memos").scalar_one(), 0)
        invalid = [
            (dict(valid, walk=999), "walk_private_memos_walk_id_fkey"),
            (dict(valid, author=999), "walk_private_memos_author_user_id_fkey"),
            (dict(valid, memo="  "), "ck_walk_private_memo_content"),
            (dict(valid, version=0), "ck_walk_private_memo_version"),
        ]
        for params, constraint in invalid:
            with self.subTest(constraint=constraint):
                with self.assertRaises(IntegrityError) as error, migrated.begin() as connection:
                    connection.exec_driver_sql(insert, params)
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        with migrated.begin() as connection:
            connection.exec_driver_sql(insert, valid)
        with self.assertRaises(IntegrityError) as error, migrated.begin() as connection:
            connection.exec_driver_sql(insert, valid)
        self.assertEqual(error.exception.orig.diag.constraint_name, "uq_walk_private_memos_walk")
        self.apply(migrated, "033_walk_private_memos.sql")
        with migrated.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT memo, version FROM walk_private_memos").one(), (MEMO, 1))

        orm = self.isolated_engine()
        Base.metadata.create_all(orm)

        def catalog(engine):
            with engine.connect() as connection:
                inspector = inspect(connection)
                table = "walk_private_memos"
                columns = {column["name"]: (str(column["type"]), column["nullable"], column["default"])
                           for column in inspector.get_columns(table)}
                checks = {check["name"]: check["sqltext"] for check in inspector.get_check_constraints(table)}
                return columns, checks, inspector.get_foreign_keys(table), inspector.get_unique_constraints(table)

        self.assertEqual(catalog(migrated), catalog(orm))


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class WalkPrivateMemoPostgresTests(IsolatedPostgres, PrivateMemoFixture, unittest.TestCase):
    def setUp(self):
        self.setup_private_memo_api(self.isolated_engine())

    def race(self, first, second):
        """Pause the first request after locking its Walk and wait for the competing lock attempt."""
        held, attempted, release = Event(), Event(), Event()

        def is_walk_lock(statement):
            return "FROM walks" in statement and statement.rstrip().endswith("FOR UPDATE")

        def hold_lock(connection, cursor, statement, parameters, context, executemany):
            if is_walk_lock(statement) and not held.is_set():
                held.set()
                if not release.wait(8):
                    raise AssertionError("first Walk lock was not released")

        def observe_attempt(connection, cursor, statement, parameters, context, executemany):
            if held.is_set() and is_walk_lock(statement):
                attempted.set()

        for event_name, listener in (("after_cursor_execute", hold_lock), ("before_cursor_execute", observe_attempt)):
            event.listen(self.engine, event_name, listener)
            self.addCleanup(event.remove, self.engine, event_name, listener)
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(first)
            try:
                self.assertTrue(held.wait(8), "first request did not acquire the Walk lock")
                other = pool.submit(second)
                self.assertTrue(attempted.wait(8), "second request did not attempt the Walk lock")
            finally:
                release.set()
            return winner.result(timeout=10), other.result(timeout=10)

    def test_identical_concurrent_creates_and_lost_response_return_one_memo(self):
        self.finish_ready_walk()
        first, second = self.race(self.put_memo, self.put_memo)
        self.assertEqual((first.status_code, second.status_code), (201, 200), (first.text, second.text))
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.rows(WalkPrivateMemo)), 1)
        self.assertEqual(self.summary().json()["private_memo"]["memo"], MEMO)

    def test_concurrent_creates_with_different_content_do_not_overwrite(self):
        self.finish_ready_walk()
        first, second = self.race(self.put_memo, lambda: self.put_memo("다른 내용"))
        self.assertEqual((first.status_code, second.status_code), (201, 409), (first.text, second.text))
        self.assertEqual(self.summary().json()["private_memo"]["memo"], MEMO)
        self.assertEqual(len(self.rows(WalkPrivateMemo)), 1)

    def test_concurrent_update_versions_reject_stale_content(self):
        self.finish_ready_walk()
        self.assertEqual(self.put_memo().status_code, 201)
        self.advance(120)
        first, second = self.race(lambda: self.put_memo("첫 기기 수정", version=1),
                                  lambda: self.put_memo("다른 기기 수정", version=1))
        self.assertEqual((first.status_code, second.status_code), (200, 409), (first.text, second.text))
        self.assertEqual(first.json()["version"], 2)
        replay = self.put_memo("첫 기기 수정", version=1)
        self.assertEqual((replay.status_code, replay.json()), (200, first.json()))
        persisted = self.summary().json()["private_memo"]
        self.assertEqual((persisted["memo"], persisted["version"]), ("첫 기기 수정", 2))

    def test_memo_waiting_on_handover_sees_the_committed_eligibility(self):
        self.finish()
        self.assertEqual(self.put_memo().status_code, 409)
        handover, memo = self.race(self.handover, self.put_memo)
        self.assertEqual((handover.status_code, memo.status_code), (201, 201), (handover.text, memo.text))
        self.assertEqual(self.summary().json()["handover_status"], "SELF_REPORTED")

    def test_failed_memo_update_rolls_back_without_changing_end_or_handover(self):
        ended = self.finish_ready_walk()
        first = self.put_memo()
        self.assertEqual(first.status_code, 201, first.text)
        models = (Walk, WalkHandover, VolunteerProfile, WalkPrivateMemo)
        snapshot = [self.rows(model) for model in models]

        def fail_after_flush(db, walk_id, actor, payload):
            upsert_walk_private_memo(db, walk_id, actor, payload)
            db.connection().exec_driver_sql("SELECT 1 / 0")

        self.advance(120)
        with patch("app.routers.walk_reviews.upsert_walk_private_memo", side_effect=fail_after_flush):
            rejected = self.put_memo("저장 실패 합성 자료", version=1)
        self.assertEqual(rejected.status_code, 500)
        self.assertNotIn("저장 실패 합성 자료", rejected.text)
        self.assertEqual([self.rows(model) for model in models], snapshot)
        detail = self.detail(self.walk_id).json()
        self.assertEqual(detail["walk"], ended)
        self.assertEqual(detail["handover_status"], "SELF_REPORTED")
        self.assertEqual(self.summary().json()["private_memo"]["version"], 1)
        self.assertEqual(self.put_memo("재시도", version=1).json()["version"], 2)
