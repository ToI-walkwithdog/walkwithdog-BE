"""Recording policies, summary arithmetic, privacy and atomic API behavior."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from walk_motion_test_support import MotionRecordingAssertions
from walk_record_test_support import WalkFixture, START
from walk_request_test_support import NOW, END
from app.database import get_db
from app.models import Dog, VolunteerProfile, Walk, WalkIncident, WalkMarker, WalkPoint, WalkRequest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool


class WalkRecordingTests(MotionRecordingAssertions, WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)

    def test_start_approved_request_server_time_and_no_statistics_updates(self):
        before = self.rows(VolunteerProfile), self.rows(Dog)
        walk = self.begin()
        self.assertEqual(walk["status"], "IN_PROGRESS")
        self.assertEqual(walk["started_at"], START.isoformat().replace("+00:00", "Z"))
        self.assertIsNone(walk["ended_at"])
        self.assertIsNone(walk["total_distance_m"])
        self.assertIsNone(walk["average_pace"])
        self.assertEqual((self.rows(VolunteerProfile), self.rows(Dog)), before)
        self.assertEqual(self.start(walk["walk_request_id"]).status_code, 409)

    def test_start_owner_role_auth_approval_and_missing_request(self):
        identifier = self.seed_request(status="APPROVED")
        for user, expected in ((None, 401), (2, 403), (3, 403), (5, 403)):
            self.assertEqual(self.start(identifier, user).status_code, expected)
        self.assertEqual(self.start(999).status_code, 404)
        for i, state in enumerate(("PENDING", "REJECTED", "CANCELLED")):
            other = self.seed_request(status=state, end=END + timedelta(seconds=i + 1))
            self.assertEqual(self.start(other).status_code, 409)
        self.assertEqual(self.rows(Walk), [])

    def test_start_window_exact_boundaries_and_current_conditions(self):
        identifier = self.seed_request(status="APPROVED")
        for seconds in (-1, 3600):
            self.advance(seconds)
            self.assertEqual(self.start(identifier).status_code, 409)
        self.advance(0)
        for changes in ({"walkable": False}, {"difficulty": "EXPERIENCED"}, {"slots": []}):
            self.assertEqual(self.client.patch("/dogs/1/walk-settings", headers=self.auth(3), json=changes).status_code, 200)
            self.assertEqual(self.start(identifier).status_code, 409)
            self.client.patch("/dogs/1/walk-settings", headers=self.auth(3), json={
                "walkable": True, "difficulty": "BEGINNER",
                "slots": [{"day_of_week": 0, "start_time": "09:00", "end_time": "12:00"}]})
        with self.Session.begin() as db:
            db.get(Dog, 1).shelter_id = 2
        self.assertEqual(self.start(identifier).status_code, 409)
        with self.Session.begin() as db:
            db.get(Dog, 1).shelter_id = 1
        self.assertEqual(self.start(identifier).status_code, 201)

    def test_start_rejects_client_owned_status_times_and_metrics(self):
        identifier = self.seed_request(status="APPROVED")
        for key, value in (("user_id", 2), ("status", "COMPLETED"), ("started_at", START.isoformat()),
                           ("total_distance_m", 100), ("duration_seconds", 30)):
            self.assertEqual(self.start(identifier, **{key: value}).status_code, 422)
        self.assertEqual(self.rows(Walk), [])

    def test_existing_session_blocks_next_dog_or_volunteer_until_ended(self):
        first = self.begin()
        next_request = self.seed_request(dog=9, status="APPROVED", start=END, end=END + timedelta(hours=1))
        self.advance(3600)
        self.assertEqual(self.start(next_request).status_code, 409)
        self.assertEqual(self.end(first["id"]).status_code, 200)
        self.assertEqual(self.start(next_request).status_code, 201)

    def test_sorted_batch_retries_and_mixed_duplicates(self):
        walk = self.begin()
        self.advance(120)
        p0, p1, p2 = self.point(), self.point(1, 10, longitude="0.0001"), self.point(2, 20, longitude="0.0002")
        first = self.upload(walk["id"], [p1, p0])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual((first.json()["accepted_count"], first.json()["last_sequence_no"]), (2, 1))
        retry = self.upload(walk["id"], [p0, p1])
        self.assertEqual((retry.json()["accepted_count"], retry.json()["duplicate_count"]), (0, 2))
        mixed = self.upload(walk["id"], [p2, p1])
        self.assertEqual((mixed.json()["accepted_count"], mixed.json()["duplicate_count"], mixed.json()["last_sequence_no"]), (1, 1, 2))
        self.assertEqual([p["sequence_no"] for p in self.detail(walk["id"]).json()["points"]], [0, 1, 2])

    def test_first_sequence_and_gaps_are_atomic_conflicts(self):
        walk = self.begin()
        self.advance(120)
        for batch in ([self.point(1)], [self.point(), self.point(2, 20)]):
            self.assertEqual(self.upload(walk["id"], batch).status_code, 409)
            self.assertEqual(self.rows(WalkPoint), [])
        for seq in (-1, True, "0"):
            self.assertEqual(self.upload(walk["id"], [self.point(seq)]).status_code, 422)
        self.assertEqual(self.upload(walk["id"], []).status_code, 422)
        self.assertEqual(self.upload(walk["id"], [self.point()] * 501).status_code, 422)

    def test_same_sequence_matching_duplicates_and_conflicting_payloads(self):
        walk = self.begin()
        self.advance(120)
        result = self.upload(walk["id"], [self.point(), self.point()])
        self.assertEqual((result.json()["accepted_count"], result.json()["duplicate_count"]), (1, 1))
        before = self.rows(WalkPoint)
        self.assertEqual(self.upload(walk["id"], [self.point(1, 10), self.point(1, 10, accuracy_m=6)]).status_code, 409)
        self.assertEqual(self.upload(walk["id"], [self.point(accuracy_m=6)]).status_code, 409)
        self.assertEqual(self.rows(WalkPoint), before)

    def test_point_times_must_be_aware_within_session_and_strictly_increase(self):
        walk = self.begin()
        self.advance(120)
        for batch in ([self.point(seconds=-1)], [self.point(seconds=121)],
                      [self.point(), self.point(1)], [self.point(seconds=10), self.point(1, 5)]):
            self.assertEqual(self.upload(walk["id"], batch).status_code, 409)
            self.assertEqual(self.rows(WalkPoint), [])
        for stamp in ("2030-01-07T00:00:00", str(int(START.timestamp())), 12345, "bad"):
            self.assertEqual(self.upload(walk["id"], [{**self.point(), "recorded_at": stamp}]).status_code, 422)

    def test_coordinates_accuracy_finiteness_and_quality(self):
        walk = self.begin()
        self.advance(120)
        for field, value in (("latitude", 91), ("longitude", -181), ("latitude", "NaN"),
                             ("longitude", "Infinity"), ("latitude", True), ("accuracy_m", -1),
                             ("accuracy_m", "NaN"), ("accuracy_m", True)):
            self.assertEqual(self.upload(walk["id"], [{**self.point(), field: value}]).status_code, 422)
        self.assertEqual(self.upload(walk["id"], [self.point(), self.point(1, 10, accuracy_m=51)]).status_code, 409)
        self.assertEqual(self.rows(WalkPoint), [])
        self.assertEqual(self.upload(walk["id"], [self.point(accuracy_m=None), self.point(1, 10, accuracy_m=50)]).status_code, 200)

    def test_teleport_rejected_and_corrected_retry_accepted(self):
        walk = self.begin()
        self.advance(120)
        self.assertEqual(self.upload(walk["id"], [self.point(), self.point(1, 1, longitude=1)]).status_code, 409)
        self.assertEqual(self.rows(WalkPoint), [])
        self.assertEqual(self.upload(walk["id"], [self.point(), self.point(1, 10, longitude="0.0001")]).status_code, 200)

    def test_precision_canonicalization_and_equivalent_offset_retry(self):
        walk = self.begin()
        self.advance(120)
        point = self.point(latitude="37.1234567", longitude="127.1234567", accuracy_m="3.456")
        self.assertEqual(self.upload(walk["id"], [point]).status_code, 200)
        stored = self.detail(walk["id"]).json()["points"][0]
        self.assertEqual((stored["latitude"], stored["longitude"], stored["accuracy_m"]), ("37.123457", "127.123457", "3.46"))
        retry = {**point, "latitude": "37.123457", "longitude": "127.123457", "accuracy_m": "3.46",
                 "recorded_at": "2030-01-07T09:00:00+09:00"}
        self.assertEqual(self.upload(walk["id"], [retry]).json()["duplicate_count"], 1)

    def test_summary_distance_time_pace_and_request_profile_preservation(self):
        walk = self.begin()
        before = self.rows(VolunteerProfile), self.rows(WalkRequest)
        self.advance(120)
        self.assertEqual(self.upload(walk["id"], [self.point(), self.point(1, 10, longitude="0.0001"), self.point(2, 20, longitude="0.0002")]).status_code, 200)
        ended = self.end(walk["id"])
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual((ended.json()["status"], ended.json()["total_distance_m"], ended.json()["duration_seconds"], ended.json()["average_pace"]),
                         ("COMPLETED", "22.24", 120, "899.28"))
        self.assertEqual((self.rows(VolunteerProfile), self.rows(WalkRequest)), before)
        self.assertEqual(self.end(walk["id"]).status_code, 409)

    def test_no_points_zero_time_and_single_point_have_no_pace(self):
        walk = self.begin()
        ended = self.end(walk["id"]).json()
        self.assertEqual((ended["total_distance_m"], ended["duration_seconds"], ended["average_pace"]), ("0.00", 0, None))
        other = self.begin(user=2, dog=3)
        self.advance(10)
        self.assertEqual(self.upload(other["id"], [self.point()], user=2).status_code, 200)
        self.assertIsNone(self.end(other["id"], user=2).json()["average_pace"])

    def test_long_gap_does_not_bridge_distance_but_raw_points_remain(self):
        walk = self.begin()
        self.advance(600)
        response = self.upload(walk["id"], [self.point(), self.point(1, 301, longitude="0.0001"), self.point(2, 311, longitude="0.0002")])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.end(walk["id"]).json()["total_distance_m"], "11.12")
        self.assertEqual(len(self.rows(WalkPoint)), 3)

    def test_subsecond_duration_with_distance_has_null_pace(self):
        walk = self.begin()
        self.advance(0.8)
        self.assertEqual(self.upload(walk["id"], [self.point(), self.point(1, 0.5, longitude="0.00001")]).status_code, 200)
        result = self.end(walk["id"]).json()
        self.assertEqual((result["total_distance_m"], result["duration_seconds"], result["average_pace"]), ("1.11", 0, None))

    def test_unrepresentable_pace_is_null_instead_of_numeric_overflow(self):
        walk = self.begin()
        self.advance(120)
        self.upload(walk["id"], [self.point(), self.point(1, 10, longitude="0.000001")])
        self.advance(2_000_000)
        result = self.end(walk["id"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["total_distance_m"], "0.11")
        self.assertIsNone(result.json()["average_pace"])

    def test_haversine_handles_antimeridian_without_teleport(self):
        walk = self.begin()
        self.advance(120)
        response = self.upload(walk["id"], [self.point(longitude="179.99999"), self.point(1, 10, longitude="-179.99999")])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.end(walk["id"]).json()["total_distance_m"], "2.22")

    def test_raw_location_writes_owner_only_and_read_access_is_private(self):
        walk = self.begin()
        for user in (None, 2, 3, 4, 5, 7):
            expected = 401 if user is None else 403
            self.assertEqual(self.upload(walk["id"], [self.point()], user).status_code, expected)
            self.assertEqual(self.marker(walk["id"], user).status_code, expected)
        for user, code in ((None, 401), (1, 200), (2, 403), (3, 200), (4, 403), (5, 200), (7, 403)):
            self.assertEqual(self.detail(walk["id"], user).status_code, code)

    def test_marker_types_timestamps_and_server_owned_fields(self):
        walk = self.begin()
        self.advance(120)
        for marker_type in ("SNIFF", "POOP", "DOG_ENCOUNTER", "PERSON_ENCOUNTER", "BARKING"):
            self.assertEqual(self.marker(walk["id"], marker_type=marker_type, memo="관찰").status_code, 201)
        for extra in ({"marker_type": "UNKNOWN"}, {"walk_id": 999}, {"user_id": 2}, {"latitude": 91}):
            self.assertEqual(self.marker(walk["id"], **extra).status_code, 422)
        self.assertEqual(self.marker(walk["id"], seconds=-1).status_code, 409)
        self.assertEqual(self.marker(walk["id"], seconds=121).status_code, 409)
        self.assertEqual(len(self.rows(WalkMarker)), 5)

    def test_incident_permissions_nullable_location_and_server_open_status(self):
        walk = self.begin()
        self.advance(120)
        for user in (1, 3, 5):
            result = self.incident(walk["id"], user)
            self.assertEqual(result.status_code, 201, result.text)
            self.assertEqual((result.json()["reported_by"], result.json()["status"]), (user, "OPEN"))
            self.assertIsNone(result.json()["latitude"])
        for user in (None, 2, 4, 7):
            self.assertEqual(self.incident(walk["id"], user).status_code, 401 if user is None else 403)
        for extra in ({"description": " "}, {"latitude": 0}, {"incident_type": "UNKNOWN"},
                      {"reported_by": 5}, {"status": "RESOLVED"}, {"occurred_at": "2030-01-07T00:00"}):
            self.assertEqual(self.incident(walk["id"], **extra).status_code, 422)
        self.assertEqual(self.incident(walk["id"], latitude=0, longitude=0).status_code, 201)
        self.assertEqual(self.incident(walk["id"], seconds=121).status_code, 409)

    def test_staff_can_interrupt_but_not_claim_normal_completion(self):
        walk = self.begin()
        self.advance(120)
        self.assertEqual(self.end(walk["id"], user=3).status_code, 403)
        self.assertEqual(self.end(walk["id"], user=4, status="INTERRUPTED").status_code, 403)
        self.assertEqual(self.end(walk["id"], user=2, status="INTERRUPTED").status_code, 403)
        self.assertEqual(self.end(walk["id"], user=3, status="INTERRUPTED").json()["status"], "INTERRUPTED")

    def test_terminal_walk_immutable_even_identical_point_retries(self):
        walk = self.begin()
        self.advance(120)
        self.upload(walk["id"], [self.point()])
        self.marker(walk["id"])
        self.incident(walk["id"])
        self.end(walk["id"])
        before = self.rows(Walk), self.rows(WalkPoint), self.rows(WalkMarker), self.rows(WalkIncident)
        self.assertEqual(self.upload(walk["id"], [self.point()]).status_code, 409)
        self.assertEqual(self.marker(walk["id"]).status_code, 409)
        self.assertEqual(self.incident(walk["id"], user=3).status_code, 409)
        self.assertEqual(self.end(walk["id"]).status_code, 409)
        self.assertEqual((self.rows(Walk), self.rows(WalkPoint), self.rows(WalkMarker), self.rows(WalkIncident)), before)
        self.assertEqual(self.detail(walk["id"]).status_code, 200)

    def test_detail_pagination_counts_order_and_read_query_bound(self):
        walk = self.begin()
        self.advance(120)
        self.upload(walk["id"], [self.point(), self.point(1, 10), self.point(2, 20)])
        for _ in range(3):
            self.marker(walk["id"])
            self.incident(walk["id"])
        statements = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            response = self.detail(walk["id"], query="?limit=1&offset=1")
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data["point_count"], data["marker_count"], data["incident_count"]), (3, 3, 3))
        self.assertEqual(data["points"][0]["sequence_no"], 1)
        self.assertEqual((len(data["markers"]), len(data["incidents"])), (1, 1))
        self.assertEqual(len(statements), 9)
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements))

    def test_clock_regression_cannot_end_before_latest_observation(self):
        walk = self.begin()
        self.advance(120)
        self.marker(walk["id"], seconds=100)
        self.advance(99)
        self.assertEqual(self.end(walk["id"]).status_code, 409)
        self.advance(-1)
        self.assertEqual(self.end(walk["id"]).status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.get(Walk, walk["id"]).status, "IN_PROGRESS")

    def test_missing_walk_invalid_query_and_forged_summary_inputs(self):
        self.assertEqual(self.upload(999, [self.point()]).status_code, 404)
        self.assertEqual(self.marker(999).status_code, 404)
        self.assertEqual(self.incident(999).status_code, 404)
        self.assertEqual(self.end(999).status_code, 404)
        self.assertEqual(self.detail(999).status_code, 404)
        walk = self.begin()
        for query in ("?limit=0", "?limit=501", "?offset=-1"):
            self.assertEqual(self.detail(walk["id"], query=query).status_code, 422)
        for payload in ({"status": "CANCELLED"}, {"total_distance_m": 999}, {"ended_at": START.isoformat()}):
            self.assertEqual(self.end(walk["id"], **payload).status_code, 422)

    def test_historical_shelter_permissions_and_ongoing_recording_survive_transfer(self):
        walk = self.begin()
        self.advance(120)
        with self.Session.begin() as db:
            db.get(Dog, 1).shelter_id = 2
        self.assertEqual(self.detail(walk["id"], 3).status_code, 200)
        self.assertEqual(self.detail(walk["id"], 4).status_code, 403)
        self.assertEqual(self.upload(walk["id"], [self.point()]).status_code, 200)
        self.assertEqual(self.incident(walk["id"], 3).status_code, 201)
        self.assertEqual(self.incident(walk["id"], 4).status_code, 403)

    def test_commit_failures_rollback_points_markers_incidents_and_summary(self):
        walk = self.begin()
        self.advance(120)
        self.upload(walk["id"], [self.point()])
        before = self.rows(Walk), self.rows(WalkPoint), self.rows(WalkMarker), self.rows(WalkIncident)

        def fail_db():
            with self.Session() as db, patch.object(db, "commit", side_effect=SQLAlchemyError("private SQL")):
                yield db

        self.app.dependency_overrides[get_db] = fail_db
        for operation in (lambda: self.upload(walk["id"], [self.point(1, 10)]), lambda: self.marker(walk["id"]),
                          lambda: self.incident(walk["id"]), lambda: self.end(walk["id"])):
            response = operation()
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("private", response.text)
            self.assertEqual((self.rows(Walk), self.rows(WalkPoint), self.rows(WalkMarker), self.rows(WalkIncident)), before)

    def test_start_commit_failure_and_known_sqlstate_classification(self):
        class PGError(Exception):
            def __init__(self, code, name):
                self.pgcode, self.diag = code, SimpleNamespace(constraint_name=name)

        identifier = self.seed_request(status="APPROVED")
        for code, name, expected in (("23505", "uq_walks_request", 409), ("23505", "uq_walk_points_sequence", 409),
                                     ("55P03", None, 409), ("40P01", None, 409), ("40001", None, 409),
                                     ("23505", "unknown_unique", 500), ("23514", "ck_status", 500)):
            def fail_db():
                with self.Session() as db, patch.object(db, "commit", side_effect=IntegrityError("private SQL", {}, PGError(code, name))):
                    yield db
            self.app.dependency_overrides[get_db] = fail_db
            response = self.start(identifier)
            self.assertEqual(response.status_code, expected, response.text)
            self.assertNotIn("private", response.text)
            self.assertEqual(self.rows(Walk), [])

    def test_end_to_end_application_approval_start_recording_and_finish(self):
        with patch("app.walk_request_service.utc_now", return_value=NOW):
            request = self.create()
            self.assertEqual(self.act(request["id"], "APPROVE").status_code, 200)
        walk = self.start(request["id"])
        self.assertEqual(walk.status_code, 201)
        self.advance(120)
        identifier = walk.json()["id"]
        self.assertEqual(self.upload(identifier, [self.point(), self.point(1, 10, longitude="0.0001")]).status_code, 200)
        self.assertEqual(self.marker(identifier).status_code, 201)
        self.assertEqual(self.incident(identifier).status_code, 201)
        self.assertEqual(self.end(identifier).status_code, 200)
        self.assertEqual(self.act(request["id"], "CANCEL", user=1).status_code, 409)
        self.assertEqual(self.detail(identifier).json()["walk"]["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
