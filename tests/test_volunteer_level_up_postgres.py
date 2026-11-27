"""Issue #97 integration tests on disposable PostgreSQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from test_date_slot_booking_postgres import DateSlotMigrationFixture, IsolatedPostgres, TEST_URL
from test_volunteer_level_up import POLICY, LevelFixture
from test_walk_handover import COMPLETE
from walk_request_test_support import START
from app import volunteer_level_service as levels
from app.database import Base
from app.models import User, VolunteerProfile, Walk
from app.walk_models import VolunteerLevelEvaluation, VolunteerLevelEvent, WalkHandover, WalkPrivateMemo
from app.walk_record_schemas import WalkEnd, WalkHandoverCreate
from app.walk_service import close_stale_walks, end_walk, save_handover


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class LevelUpMigrationTests(DateSlotMigrationFixture, unittest.TestCase):
    def test_034_preserves_old_data_and_matches_orm_constraints(self):
        migrated = self.setup_predecessor()
        for file in (
            "028_date_slot_booking.sql", "029_reservation_cancellation.sql", "030_shelter_code_verification.sql",
            "031_walk_marker_idempotency.sql", "032_walk_handovers.sql", "033_walk_private_memos.sql",
        ):
            self.apply(migrated, file)
        with migrated.begin() as connection:
            self.seed_legacy(connection)
            connection.exec_driver_sql(
                "INSERT INTO walks (id, walk_request_id, status, started_at, ended_at, end_reason) VALUES "
                "(1, 1, 'COMPLETED', '2030-01-07 00:00Z', '2030-01-07 00:10Z', 'USER')"
            )
            connection.exec_driver_sql(
                "INSERT INTO volunteer_profiles (user_id, level, total_walk_count) VALUES (1, 7, 12)"
            )
            before = [connection.exec_driver_sql(f"SELECT * FROM {table}").all()
                      for table in ("walks", "volunteer_profiles")]
        self.apply(migrated, "034_volunteer_level_up.sql")
        with migrated.begin() as connection:
            self.assertEqual([connection.exec_driver_sql(f"SELECT * FROM {table}").all()
                              for table in ("walks", "volunteer_profiles")], before)
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM volunteer_level_evaluations").scalar_one(), 0)
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM volunteer_level_events").scalar_one(), 0)
            connection.exec_driver_sql(
                "INSERT INTO volunteer_level_evaluations (walk_id, user_id, status, policy_version, evaluated_at) "
                "VALUES (1, 1, 'COMPLETED', 'level-test', now())"
            )
            insert_event = (
                "INSERT INTO volunteer_level_events (walk_id, user_id, before_level, after_level, policy_version, earned_at) "
                "VALUES (1, %(user)s, 1, %(level)s, 'level-test', now())"
            )
            connection.exec_driver_sql(insert_event, {"user": 1, "level": 2})
        for params, constraint in (
            ({"user": 2, "level": 2}, "fk_volunteer_level_events_evaluation"),
            ({"user": 1, "level": 6}, "ck_volunteer_level_event_increase"),
            ({"user": 1, "level": 2}, "uq_volunteer_level_events_user_walk"),
        ):
            with self.subTest(constraint=constraint):
                with self.assertRaises(IntegrityError) as error, migrated.begin() as connection:
                    connection.exec_driver_sql(insert_event, params)
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        self.apply(migrated, "034_volunteer_level_up.sql")
        with migrated.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT before_level, after_level FROM volunteer_level_events")
                             .one(), (1, 2))

        orm = self.isolated_engine()
        Base.metadata.create_all(orm)

        def catalog(engine):
            with engine.connect() as connection:
                inspector = inspect(connection)
                return [
                    (
                        {c["name"]: (str(c["type"]), c["nullable"], c["default"])
                         for c in inspector.get_columns(table)},
                        {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table)},
                        inspector.get_foreign_keys(table),
                        inspector.get_unique_constraints(table),
                    )
                    for table in ("volunteer_level_evaluations", "volunteer_level_events")
                ]
        self.assertEqual(catalog(migrated), catalog(orm))


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class VolunteerLevelUpPostgresTests(IsolatedPostgres, LevelFixture, unittest.TestCase):
    def setUp(self):
        self.setup_level_api(self.isolated_engine())

    def test_policy_start_is_inclusive_and_only_eligible_walks_are_credited(self):
        self.finish_and_handover()
        cases = [
            self.seed_level_walk(-1),
            self.seed_level_walk(1, reason="AUTO_TIMEOUT"),
            self.seed_level_walk(2, status="INTERRUPTED", reason="STAFF", handover="ISSUE"),
            self.seed_level_walk(3, handover="ISSUE"),
            self.seed_level_walk(4, handover=None),
            self.seed_level_walk(5, handover=None, required=False),
            self.seed_level_walk(6, submitted_by=2),
        ]
        self.advance(7 * 86400)
        profile = self.profile()
        self.assertEqual(self.run_level_batch(), 1)
        self.assertEqual(self.summary().json()["level_evaluation_status"], "COMPLETED")
        for walk_id in cases:
            with self.subTest(walk_id=walk_id):
                summary = self.summary(walk_id).json()
                self.assertEqual(summary["level_evaluation_status"], "NOT_ELIGIBLE")
                self.assertIsNone(summary["level_up_event"])
        self.assertEqual(self.profile(), profile)
        self.assertEqual(self.rows(VolunteerLevelEvent), [])

    def test_all_thresholds_and_max_level_with_no_downgrade_or_duplicate_credits(self):
        self.finish_and_handover()
        walks = [self.walk_id, *(self.seed_level_walk(day) for day in range(1, 41))]
        existing_high_level = self.seed_level_walk(41, user=6)
        with self.Session.begin() as db:
            db.get(VolunteerProfile, 6).level = 7
        self.advance(45 * 86400)
        stats = self.profile()
        other_stats = self.profile(user=6)
        self.assertEqual(self.run_level_batch(), 42)
        self.assertEqual(self.run_level_batch(), 0)
        self.assertEqual(len(self.rows(VolunteerLevelEvaluation)), 42)
        self.assertEqual(self.profile(), dict(stats, level=5))
        self.assertEqual(self.profile(user=6), other_stats)

        counts = {3: (1, 2), 10: (2, 3), 20: (3, 4), 40: (4, 5)}
        for count, walk_id in enumerate(walks, start=1):
            with self.subTest(credited=count):
                summary = self.summary(walk_id).json()
                self.assertEqual(summary["level_evaluation_status"], "COMPLETED")
                event = summary["level_up_event"]
                if count in counts:
                    self.assertEqual((event["before_level"], event["after_level"]), counts[count])
                else:
                    self.assertIsNone(event)
        self.assertIsNone(self.summary(existing_high_level, user=6).json()["level_up_event"])
        self.assertEqual(len(self.rows(VolunteerLevelEvent)), 4)

    def test_two_workers_serialize_same_user_and_retry_without_double_credit(self):
        self.finish_and_handover()
        self.assertEqual(self.run_level_batch(), 1)
        second = self.seed_level_walk(1)
        third = self.seed_level_walk(2)
        now = START + timedelta(days=3)
        locked, release = Event(), Event()
        original = levels.lock_pending_walk

        def hold_user_lock(db, walk_id, user_id, policy, at):
            walk = original(db, walk_id, user_id, policy, at)
            if walk_id == second and walk is not None and not locked.is_set():
                locked.set()
                if not release.wait(8):
                    raise AssertionError("level worker did not release its lock")
            return walk

        def first_worker():
            with self.Session.begin() as db:
                return levels.evaluate_walk(db, second, 1, POLICY, now)

        with patch.object(levels, "lock_pending_walk", side_effect=hold_user_lock), ThreadPoolExecutor(max_workers=1) as pool:
            winner = pool.submit(first_worker)
            try:
                self.assertTrue(locked.wait(8))
                self.assertEqual(self.run_level_batch(now), 0)
            finally:
                release.set()
            self.assertTrue(winner.result(timeout=10))
        self.assertEqual(self.run_level_batch(now), 1)
        self.assertEqual(self.run_level_batch(now), 0)
        self.assertEqual(len(self.rows(VolunteerLevelEvaluation)), 3)
        event = self.summary(third).json()["level_up_event"]
        self.assertEqual((event["before_level"], event["after_level"]), (1, 2))
        self.assertEqual(len(self.rows(VolunteerLevelEvent)), 1)

    def test_stale_walk_settlement_competes_safely_and_is_never_credited(self):
        self.finish_and_handover()
        stale_id = self.seed_level_walk(1, status="IN_PROGRESS", reason=None, handover=None)
        now = START + timedelta(days=2)
        with ThreadPoolExecutor(max_workers=1) as pool, self.Session.begin() as db:
            closed = close_stale_walks(db, now=now)
            self.assertEqual([walk.id for walk in closed], [stale_id])
            self.assertEqual(pool.submit(self.run_level_batch, now).result(timeout=8), 0)
        self.assertEqual(self.run_level_batch(now), 1)
        self.assertEqual(self.summary(stale_id).json()["level_evaluation_status"], "NOT_ELIGIBLE")
        self.assertEqual(self.detail(stale_id).json()["walk"]["end_reason"], "AUTO_TIMEOUT")
        self.assertEqual(len(self.rows(VolunteerLevelEvaluation)), 1)
        self.assertEqual(self.rows(VolunteerLevelEvent), [])

    def test_worker_retries_after_concurrent_manual_end_without_crediting_the_new_unconfirmed_walk(self):
        self.finish_and_handover()
        ending_id = self.seed_level_walk(1, status="IN_PROGRESS", reason=None, handover=None)
        self.advance(86400 + 60)
        with ThreadPoolExecutor(max_workers=1) as pool, self.Session.begin() as db:
            ended = end_walk(db, ending_id, db.get(User, 1), WalkEnd())
            self.assertEqual((ended.status, ended.end_reason), ("COMPLETED", "USER"))
            self.assertEqual(pool.submit(self.run_level_batch).result(timeout=8), 0)
        profile = self.profile()
        self.assertEqual(self.run_level_batch(), 1)
        self.assertEqual(self.profile(), profile)
        self.assertEqual(self.summary(ending_id).json()["level_evaluation_status"], "NOT_ELIGIBLE")

    def test_worker_reads_pending_work_only_after_handover_has_committed(self):
        self.advance(60)
        self.finish()
        with ThreadPoolExecutor(max_workers=1) as pool, self.Session.begin() as db:
            _, created = save_handover(db, self.walk_id, db.get(User, 1), WalkHandoverCreate(**COMPLETE))
            self.assertTrue(created)
            self.assertEqual(pool.submit(self.run_level_batch).result(timeout=8), 0)
        self.assertEqual(self.run_level_batch(), 1)
        self.assertEqual(self.summary().json()["level_evaluation_status"], "COMPLETED")

    def test_failure_rolls_back_credit_level_and_event_but_lifespan_retries_persisted_work(self):
        self.finish_and_handover()
        self.seed_level_walk(1)
        self.advance(2 * 86400)
        self.assertEqual(self.run_level_batch(), 2)
        milestone = self.seed_level_walk(3)
        self.advance(4 * 86400)
        with self.Session.begin() as db:
            db.add(WalkPrivateMemo(walk_id=milestone, author_user_id=1, memo="합성 메모", version=1))
        preserved = (Walk, WalkHandover, WalkPrivateMemo, VolunteerProfile)
        before = [self.rows(model) for model in preserved]
        original = levels.evaluate_walk

        def fail_after_writes(db, walk_id, user_id, policy, now):
            self.assertTrue(original(db, walk_id, user_id, policy, now))
            db.flush()
            db.connection().exec_driver_sql("SELECT 1 / 0")

        with patch.object(levels, "evaluate_walk", side_effect=fail_after_writes):
            self.assertEqual(self.run_level_batch(), 0)
        failed = self.summary(milestone).json()
        self.assertEqual(failed["level_evaluation_status"], "FAILED")
        self.assertIsNone(failed["level_up_event"])
        self.assertEqual(self.rows(VolunteerLevelEvent), [])
        self.assertEqual([self.rows(model) for model in preserved], before)
        self.assertEqual(self.run_level_batch(), 0)

        from app import main

        self.advance(4 * 86400 + 61)

        async def restart_worker():
            async with main.lifespan(main.app):
                async with asyncio.timeout(8):
                    while len(self.rows(VolunteerLevelEvent)) == 0:
                        await asyncio.sleep(0.03)

        with patch.object(main, "SessionLocal", self.Session):
            asyncio.run(restart_worker())
        restored = self.summary(milestone).json()
        self.assertEqual(restored["level_evaluation_status"], "COMPLETED")
        self.assertEqual(restored["level_up_event"]["after_level"], 2)
        self.assertEqual(restored["private_memo"]["memo"], "합성 메모")
        self.assertEqual([self.rows(model) for model in preserved[:-1]], before[:-1])
        self.assertEqual(self.run_level_batch(), 0)
