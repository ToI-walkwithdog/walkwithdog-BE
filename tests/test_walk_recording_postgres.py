"""Opt-in concurrent recording HTTP requests and real PostgreSQL failure rollback."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from walk_record_test_support import WalkFixture, START
from walk_request_test_support import END
from app.models import DogWalkSetting, Walk, WalkIncident, WalkMarker, WalkPoint, WalkRequest
from app.walk_request_service import locked_request
from sqlalchemy import create_engine, event, inspect
from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("WALK_RECORDS_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "WALK_RECORDS_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다")
class WalkRecordingPostgresTests(WalkFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(TEST_URL)
        validate_local_engine(cls.admin)
        if not cls.admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        with cls.admin.connect() as connection:
            inspector = inspect(connection)
            custom = [s for s in inspector.get_schema_names() if s not in ("public", "information_schema") and not s.startswith("pg_")]
            if inspector.get_table_names() or custom:
                raise PreparationError("새로운 빈 격리 테스트 DB만 허용합니다.")

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        schema = "gps_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        self.setup_walk_api(create_engine(TEST_URL, connect_args={"options": f"-csearch_path={schema}"}))

    def test_concurrent_start_creates_exactly_one_walk(self):
        identifier = self.seed_request(status="APPROVED")
        winner, loser = self.recording_race(lambda: self.start(identifier), lambda: self.start(identifier),
                                            hook="app.walk_service.validate_walk_start")
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.assertEqual(self.start(identifier).status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(Walk).count(), 1)
            self.assertEqual(db.get(WalkRequest, identifier).status, "APPROVED")

    def test_concurrent_identical_upload_is_retryable_without_duplicate_rows(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.upload(walk["id"], [self.point()]), lambda: self.upload(walk["id"], [self.point()]))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        retry = self.upload(walk["id"], [self.point()])
        self.assertEqual((retry.status_code, retry.json()["accepted_count"], retry.json()["duplicate_count"]), (200, 0, 1))
        self.assertEqual(len(self.rows(WalkPoint)), 1)

    def test_concurrent_append_batches_preserve_contiguous_order(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.upload(walk["id"], [self.point()]), lambda: self.upload(walk["id"], [self.point(1, 10)]))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.upload(walk["id"], [self.point(1, 10)]).status_code, 200)
        self.assertEqual([p["sequence_no"] for p in self.detail(walk["id"]).json()["points"]], [0, 1])

    def test_points_win_end_race_and_retry_summary_includes_committed_points(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(
            lambda: self.upload(walk["id"], [self.point(), self.point(1, 10, longitude="0.0001")]), lambda: self.end(walk["id"]))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        finished = self.end(walk["id"])
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["total_distance_m"], "11.12")

    def test_end_wins_upload_race_and_rejects_late_points(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.end(walk["id"]), lambda: self.upload(walk["id"], [self.point()]))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.upload(walk["id"], [self.point()]).status_code, 409)
        self.assertEqual(self.rows(WalkPoint), [])

    def test_double_end_cannot_recompute_summary_or_change_terminal_status(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.end(walk["id"]), lambda: self.end(walk["id"], user=3, status="INTERRUPTED"))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.detail(walk["id"]).json()["walk"]["status"], "COMPLETED")

    def test_marker_wins_end_race_and_is_preserved(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.marker(walk["id"]), lambda: self.end(walk["id"]))
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.assertEqual(self.end(walk["id"]).status_code, 200)
        self.assertEqual(len(self.rows(WalkMarker)), 1)

    def test_end_wins_incident_race_and_freezes_all_events(self):
        walk = self.begin()
        self.advance(120)
        winner, loser = self.recording_race(lambda: self.end(walk["id"]), lambda: self.incident(walk["id"], user=3))
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.incident(walk["id"], user=3).status_code, 409)
        self.assertEqual(self.rows(WalkIncident), [])

    def test_start_wins_cancellation_race_request_never_becomes_cancelled(self):
        identifier = self.seed_request(status="APPROVED")
        winner, loser = self.recording_race(lambda: self.start(identifier), lambda: self.act(identifier, "CANCEL", user=1),
                                            hook="app.walk_service.validate_walk_start")
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.assertEqual(self.act(identifier, "CANCEL", user=1).status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.get(WalkRequest, identifier).status, "APPROVED")

    def test_cancellation_before_start_boundary_wins_and_no_walk_is_created(self):
        identifier = self.seed_request(status="APPROVED")
        # Cancellation is checked just before the boundary; start arrives at it.
        from datetime import timedelta
        self.request_clock.return_value = START - timedelta(seconds=1)
        winner, loser = self.recording_race(lambda: self.act(identifier, "CANCEL", user=1), lambda: self.start(identifier),
                                            hook="app.routers.walk_requests.transition_request")
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.start(identifier).status_code, 409)
        self.assertEqual(self.rows(Walk), [])

    def test_settings_off_wins_start_race_and_prevents_start_after_retry(self):
        identifier = self.seed_request(status="APPROVED")
        off = lambda: self.client.patch("/dogs/1/walk-settings", headers=self.auth(3), json={"walkable": False})
        winner, loser = self.recording_race(off, lambda: self.start(identifier), hook="app.routers.dogs.apply_walk_settings")
        self.assertEqual((winner.status_code, loser.status_code), (200, 409))
        self.assertEqual(self.start(identifier).status_code, 409)
        self.assertEqual(self.rows(Walk), [])

    def test_start_wins_settings_race_later_setting_change_does_not_lose_recording(self):
        identifier = self.seed_request(status="APPROVED")
        off = lambda: self.client.patch("/dogs/1/walk-settings", headers=self.auth(3), json={"walkable": False})
        winner, loser = self.recording_race(lambda: self.start(identifier), off, hook="app.walk_service.validate_walk_start")
        self.assertEqual((winner.status_code, loser.status_code), (201, 409))
        self.assertEqual(off().status_code, 200)
        self.advance(120)
        self.assertEqual(self.upload(winner.json()["id"], [self.point()]).status_code, 200)
        self.assertEqual(self.end(winner.json()["id"]).status_code, 200)

    def test_real_walk_unique_constraint_remains_final_duplicate_guard(self):
        walk = self.begin()
        self.assertEqual(self.end(walk["id"]).status_code, 200)

        def guard_without_duplicate_check(db, identifier, actor):
            request, _, _ = locked_request(db, identifier, actor, "START")
            return request

        with patch("app.walk_service.validate_walk_start", side_effect=guard_without_duplicate_check):
            failed = self.start(walk["walk_request_id"])
        self.assertEqual(failed.status_code, 409, failed.text)
        self.assertIn("중복", failed.json()["detail"])
        self.assertEqual(len(self.rows(Walk)), 1)

    def test_actual_point_unique_error_rolls_back_batch_and_preserves_original(self):
        walk = self.begin()
        self.advance(120)
        self.upload(walk["id"], [self.point()])

        def duplicate(mapper, connection, target):
            target.sequence_no = 0

        event.listen(WalkPoint, "before_insert", duplicate)
        try:
            response = self.upload(walk["id"], [self.point(1, 10)])
        finally:
            event.remove(WalkPoint, "before_insert", duplicate)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(self.rows(WalkPoint)), 1)
        self.assertEqual(self.upload(walk["id"], [self.point(1, 10)]).status_code, 200)

    def test_actual_end_check_failure_rolls_back_summary_status_and_time(self):
        walk = self.begin()
        self.advance(120)
        self.upload(walk["id"], [self.point(), self.point(1, 10, longitude="0.0001")])

        def invalid_duration(mapper, connection, target):
            target.duration_seconds = -1

        event.listen(Walk, "before_update", invalid_duration)
        try:
            response = self.end(walk["id"])
        finally:
            event.remove(Walk, "before_update", invalid_duration)
        self.assertEqual(response.status_code, 500, response.text)
        stored = self.detail(walk["id"]).json()["walk"]
        self.assertEqual(stored["status"], "IN_PROGRESS")
        self.assertIsNone(stored["ended_at"])
        self.assertIsNone(stored["total_distance_m"])
        self.assertEqual(self.end(walk["id"]).status_code, 200)

    def test_real_numeric_precision_and_summary_roundtrip(self):
        walk = self.begin()
        self.advance(120)
        self.assertEqual(self.upload(walk["id"], [self.point(), self.point(1, 10, longitude="0.0001"), self.point(2, 20, longitude="0.0002")]).status_code, 200)
        result = self.end(walk["id"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((result.json()["total_distance_m"], result.json()["duration_seconds"], result.json()["average_pace"]), ("22.24", 120, "5395.68"))
        with self.Session() as db:
            self.assertIsNotNone(db.get(Walk, walk["id"]).started_at.tzinfo)
            self.assertEqual(db.query(WalkPoint).count(), 3)


if __name__ == "__main__":
    unittest.main()
