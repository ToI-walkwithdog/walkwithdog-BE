"""Actual concurrent HTTP transactions and real PostgreSQL constraint diagnostics.

An opt-in empty local toi_walk_test_* DB is required. Each test retains a fresh
schema; no production data, database drops, or table resets are used.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
import os
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from walk_request_test_support import RequestFixture, START, END
from app.models import Dog, VolunteerProfile, WalkRequest
from sqlalchemy import create_engine, event, inspect
from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("WALK_REQUESTS_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "WALK_REQUESTS_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다")
class WalkRequestPostgresTests(RequestFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(TEST_URL)
        validate_local_engine(cls.admin)
        if not cls.admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        with cls.admin.connect() as connection:
            inspector = inspect(connection)
            custom_schemas = [s for s in inspector.get_schema_names() if s not in ("public", "information_schema") and not s.startswith("pg_")]
            if inspector.get_table_names() or custom_schemas:
                raise PreparationError("새로운 빈 테스트 DB만 허용합니다.")

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        schema = "wr_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={schema}"})
        self.setup_api(engine)

    def race(self, first, second, hook="app.walk_request_service.lock_resources"):
        """Pause the first independent DB transaction after its real row locks."""
        module_name, attribute = hook.rsplit(".", 1)
        original = getattr(import_module(module_name), attribute)
        entered, release = Event(), Event()

        def gated(*args, **kwargs):
            result = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                if not release.wait(8):
                    raise AssertionError("concurrency test barrier timed out")
            return result

        with patch(hook, side_effect=gated), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first)
            try:
                self.assertTrue(entered.wait(8), "first transaction did not acquire locks")
                loser = second()
            finally:
                release.set()
            winner = future.result(timeout=10)
        return winner, loser

    def assert_states(self, expected):
        with self.Session() as db:
            for identifier, state in expected.items():
                request = db.get(WalkRequest, identifier)
                self.assertEqual(request.status, state)
                if state == "PENDING":
                    self.assertIsNone(request.reviewed_by)
                    self.assertIsNone(request.reviewed_at)

    def setting_off(self):
        return self.client.patch("/dogs/1/walk-settings", headers=self.auth(3), json={"walkable": False})

    def test_same_application_double_approval(self):
        request = self.create()
        winner, loser = self.race(lambda: self.act(request["id"], "APPROVE"), lambda: self.act(request["id"], "APPROVE", user=5))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.act(request["id"], "APPROVE", user=5).status_code, 409)
        self.assert_states({request["id"]: "APPROVED"})
        with self.Session() as db:
            self.assertEqual(db.get(WalkRequest, request["id"]).reviewed_by, 3)

    def test_overlapping_dog_approvals_only_one_commits(self):
        first, second = self.create(), self.create(user=2)
        winner, loser = self.race(lambda: self.act(first["id"], "APPROVE"), lambda: self.act(second["id"], "APPROVE", user=5))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.act(second["id"], "APPROVE").status_code, 409)
        self.assert_states({first["id"]: "APPROVED", second["id"]: "PENDING"})

    def test_overlapping_volunteer_approvals_for_different_dogs_only_one_commits(self):
        first, second = self.create(), self.create(dog=9)
        winner, loser = self.race(lambda: self.act(first["id"], "APPROVE"), lambda: self.act(second["id"], "APPROVE", user=5))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.act(second["id"], "APPROVE").status_code, 409)
        self.assert_states({first["id"]: "APPROVED", second["id"]: "PENDING"})

    def test_concurrent_approve_and_reject_do_not_mix_review_metadata(self):
        request = self.create()
        winner, loser = self.race(lambda: self.act(request["id"], "APPROVE"),
                                  lambda: self.act(request["id"], "REJECT", user=5, rejection_reason="충돌"))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.act(request["id"], "REJECT", rejection_reason="재시도").status_code, 409)
        with self.Session() as db:
            stored = db.get(WalkRequest, request["id"])
            self.assertEqual(stored.status, "APPROVED")
            self.assertIsNone(stored.rejection_reason)

    def test_concurrent_approve_and_cancel_then_cancel_retry_preserves_approval(self):
        request = self.create()
        winner, loser = self.race(lambda: self.act(request["id"], "APPROVE"), lambda: self.act(request["id"], "CANCEL", user=1))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        retry = self.act(request["id"], "CANCEL", user=1)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["reviewed_at"], winner.json()["reviewed_at"])
        self.assertEqual(retry.json()["reviewed_by"], winner.json()["reviewed_by"])
        self.assert_states({request["id"]: "CANCELLED"})
        self.create()  # cancellation releases the active-duplicate key

    def test_cancellation_wins_concurrent_approval_and_cannot_be_reactivated(self):
        request = self.create()
        winner, loser = self.race(lambda: self.act(request["id"], "CANCEL", user=1), lambda: self.act(request["id"], "APPROVE"))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.act(request["id"], "APPROVE").status_code, 409)
        self.assert_states({request["id"]: "CANCELLED"})

    def test_same_volunteer_concurrent_first_requests_create_one_profile(self):
        winner, loser = self.race(lambda: self.post(), lambda: self.post(dog=9))
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.create(dog=9)
        with self.Session() as db:
            self.assertEqual(db.query(VolunteerProfile).filter_by(user_id=1).count(), 1)
            self.assertEqual(db.query(WalkRequest).filter_by(user_id=1).count(), 2)

    def test_same_user_partial_overlap_is_serialized_and_remains_rejected_on_retry(self):
        winner, loser = self.race(lambda: self.post(), lambda: self.post(start=START + timedelta(minutes=30), end=END + timedelta(minutes=30)))
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.assertEqual(self.post(start=START + timedelta(minutes=30), end=END + timedelta(minutes=30)).status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(WalkRequest).count(), 1)

    def test_distinct_volunteers_retry_concurrent_proposals_as_two_pending_requests(self):
        winner, loser = self.race(lambda: self.post(), lambda: self.post(user=2))
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.create(user=2)
        with self.Session() as db:
            self.assertEqual(db.query(WalkRequest).filter_by(status="PENDING").count(), 2)

    def test_setting_off_wins_concurrent_application_no_profile_is_left(self):
        winner, loser = self.race(self.setting_off, lambda: self.post(), hook="app.routers.dogs.apply_walk_settings")
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.post().status_code, 409)
        with self.Session() as db:
            self.assertIsNone(db.get(VolunteerProfile, 1))
            self.assertEqual(db.query(WalkRequest).count(), 0)

    def test_application_wins_concurrent_setting_change_but_approval_revalidates(self):
        winner, loser = self.race(lambda: self.post(), self.setting_off)
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.assertEqual(self.setting_off().status_code, 200)
        self.assertEqual(self.act(winner.json()["id"], "APPROVE").status_code, 409)
        self.assert_states({winner.json()["id"]: "PENDING"})

    def test_setting_off_wins_concurrent_approval(self):
        request = self.create()
        winner, loser = self.race(self.setting_off, lambda: self.act(request["id"], "APPROVE"), hook="app.routers.dogs.apply_walk_settings")
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.act(request["id"], "APPROVE").status_code, 409)
        self.assert_states({request["id"]: "PENDING"})

    def test_approval_wins_concurrent_setting_change_history_is_preserved(self):
        request = self.create()
        winner, loser = self.race(lambda: self.act(request["id"], "APPROVE"), self.setting_off)
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        # Issue #2 allows a later setting change; it does not cancel prior approvals.
        self.assertEqual(self.setting_off().status_code, 200)
        self.assert_states({request["id"]: "APPROVED"})

    def test_profile_row_lock_serializes_but_level_no_longer_blocks_approval(self):
        request = self.create(user=6, dog=2)
        with self.Session.begin() as change:
            profile = change.query(VolunteerProfile).filter_by(user_id=6).with_for_update().one()
            profile.level = 1
            change.flush()
            self.assertEqual(self.act(request["id"], "APPROVE").status_code, 409)
        self.assertEqual(self.act(request["id"], "APPROVE").status_code, 200)
        self.assert_states({request["id"]: "APPROVED"})

    def test_request_row_is_locked_not_just_parent_resources(self):
        request = self.create()
        with self.Session.begin() as other:
            other.query(WalkRequest).filter_by(id=request["id"]).with_for_update().one()
            self.assertEqual(self.act(request["id"], "APPROVE").status_code, 409)
        self.assertEqual(self.act(request["id"], "APPROVE").status_code, 200)

    def test_real_dog_exclusion_constraint_diagnostic_and_rollback(self):
        first, second = self.create(), self.create(user=2)
        self.assertEqual(self.act(first["id"], "APPROVE").status_code, 200)
        with patch("app.walk_request_service.check_approved_conflicts"):
            failed = self.act(second["id"], "APPROVE")
        self.assertEqual(failed.status_code, 409, failed.text)
        self.assertIn("강아지", failed.json()["detail"])
        self.assertIn("DB", failed.json()["detail"])
        self.assert_states({first["id"]: "APPROVED", second["id"]: "PENDING"})

    def test_real_volunteer_exclusion_constraint_diagnostic_and_rollback(self):
        first, second = self.create(), self.create(dog=9)
        self.assertEqual(self.act(first["id"], "APPROVE").status_code, 200)
        with patch("app.walk_request_service.check_approved_conflicts"):
            failed = self.act(second["id"], "APPROVE")
        self.assertEqual(failed.status_code, 409, failed.text)
        self.assertIn("봉사자", failed.json()["detail"])
        self.assertIn("DB", failed.json()["detail"])
        self.assert_states({first["id"]: "APPROVED", second["id"]: "PENDING"})

    def test_real_partial_unique_constraint_diagnostic_and_rollback(self):
        first = self.create()
        with patch("app.walk_request_service.check_active_overlap"):
            failed = self.post()
        self.assertEqual(failed.status_code, 409, failed.text)
        self.assertIn("중복", failed.json()["detail"])
        with self.Session() as db:
            self.assertEqual(db.query(WalkRequest).count(), 1)
            self.assertEqual(db.query(VolunteerProfile).filter_by(user_id=1).count(), 1)
        self.assert_states({first["id"]: "PENDING"})

    def test_actual_unexpected_check_error_is_500_and_new_profile_rolls_back(self):
        def break_constraint(mapper, connection, target):
            target.status = "INVALID"

        event.listen(WalkRequest, "before_insert", break_constraint)
        try:
            response = self.post()
        finally:
            event.remove(WalkRequest, "before_insert", break_constraint)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("INSERT", response.text)
        self.assertNotIn("ck_status", response.text)
        with self.Session() as db:
            self.assertEqual(db.query(WalkRequest).count(), 0)
            self.assertIsNone(db.get(VolunteerProfile, 1))
            self.assertEqual(db.get(Dog, 1).energy_level, 2)


if __name__ == "__main__":
    unittest.main()
