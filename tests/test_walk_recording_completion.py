"""Issue #89: settlement boundaries, active walk deadline, GPS jumps, idempotent markers,
incident text and position rules, and walk end on SQLite."""
from datetime import timedelta, timezone
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from walk_record_test_support import WalkFixture
from walk_request_test_support import END, START
from app.models import Walk, WalkIncident, WalkMarker, WalkRequest


def stamp(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


def iso_z(value):
    return value.isoformat().replace("+00:00", "Z")


def utc(value):
    """Naive UTC for comparison; PostgreSQL returns aware values in the session time zone."""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value is not None and value.tzinfo else value


class CompletionFixture(WalkFixture):
    def setup_completion_api(self, engine):
        self.setup_walk_api(engine)

    def active(self, user=1):
        return self.client.get("/walks/active", headers=self.auth(user))

    def walk_row(self, walk_id):
        with self.Session() as db:
            walk = db.get(Walk, walk_id)
            return walk.status, walk.end_reason, utc(walk.ended_at)

    def tap(self, walk_id, user=1, seconds=0, **body):
        payload = {"marker_type": "SNIFF", "recorded_at": stamp(seconds), **body}
        return self.client.post(f"/walks/{walk_id}/markers", headers=self.auth(user), json=payload)

    def report(self, walk_id, user=1, seconds=0, **body):
        payload = {"incident_type": "INJURY", "occurred_at": stamp(seconds), **body}
        return self.client.post(f"/walks/{walk_id}/incidents", headers=self.auth(user), json=payload)

    def markers(self):
        with self.Session() as db:
            return db.query(WalkMarker).order_by(WalkMarker.id).all()

    def incidents(self):
        with self.Session() as db:
            return db.query(WalkIncident).order_by(WalkIncident.id).all()


class WalkRecordingCompletionTests(CompletionFixture, unittest.TestCase):
    def setUp(self):
        self.setup_completion_api(create_engine("sqlite://", connect_args={"check_same_thread": False},
                                                poolclass=StaticPool))
        self.walk = self.begin()
        self.walk_id = self.walk["id"]

    # 1. Settlement boundaries -------------------------------------------------------------
    def test_active_404_keeps_the_auto_timeout_settlement(self):
        self.advance(3601)
        response = self.active()
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.walk_row(self.walk_id), ("COMPLETED", "AUTO_TIMEOUT", utc(START)))

    def test_failed_start_keeps_the_settlement_of_an_expired_walk(self):
        other = self.seed_request(dog=2, status="APPROVED", start=START + timedelta(minutes=70),
                                  end=START + timedelta(minutes=130))
        self.advance(3601 + 600)
        failed = self.start(other, code=False)  # no verified shelter code
        self.assertEqual(failed.status_code, 409, failed.text)
        self.assertEqual(failed.json()["detail"]["code"], "WALK_START_CODE_REQUIRED")
        self.assertEqual(self.walk_row(self.walk_id), ("COMPLETED", "AUTO_TIMEOUT", utc(START)))

    def test_end_of_an_expired_walk_keeps_auto_timeout_for_the_volunteer_and_staff(self):
        second = self.begin(user=6, dog=2)  # another volunteer's walk, for the staff path
        self.advance(3601)
        ended = self.end(self.walk_id)
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual((ended.json()["status"], ended.json()["end_reason"]), ("COMPLETED", "AUTO_TIMEOUT"))
        self.assertEqual(self.walk_row(self.walk_id), ("COMPLETED", "AUTO_TIMEOUT", utc(START)))
        self.assertEqual(self.end(self.walk_id).status_code, 409)  # already ended
        staff = self.end(second["id"], user=3, status="INTERRUPTED")
        self.assertEqual((staff.status_code, staff.json()["end_reason"]), (200, "AUTO_TIMEOUT"))

    def test_points_and_new_markers_after_expiry_are_refused_after_the_settlement_commits(self):
        key = str(uuid4())
        self.advance(10)
        self.assertEqual(self.upload(self.walk_id, [self.point(0, 5)]).status_code, 200)
        self.assertEqual(self.tap(self.walk_id, seconds=10, client_marker_id=key, latitude=0, longitude=0,
                                  accuracy_m=5).status_code, 201)
        self.advance(10 + 3601)
        self.assertEqual(self.upload(self.walk_id, [self.point(1, 20)]).status_code, 409)
        self.assertEqual(self.walk_row(self.walk_id)[:2], ("COMPLETED", "AUTO_TIMEOUT"))
        self.assertEqual(self.tap(self.walk_id, seconds=20, client_marker_id=str(uuid4())).status_code, 409)
        replay = self.tap(self.walk_id, seconds=10, client_marker_id=key, latitude=0, longitude=0, accuracy_m=5)
        self.assertEqual(replay.status_code, 200, replay.text)  # the stored tap is still confirmed

    # 2. Active walk deadline -------------------------------------------------------------
    def test_auto_end_at_matches_the_actual_auto_end_boundary(self):
        body = self.active().json()
        self.assertIsNone(body["last_point_received_at"])
        self.assertEqual(body["auto_end_at"], iso_z(START + timedelta(hours=1)))
        self.advance(120)
        self.upload(self.walk_id, [self.point(0, 100)])
        body = self.active().json()
        self.assertEqual(body["last_point_received_at"], iso_z(START + timedelta(seconds=120)))
        self.assertEqual(body["auto_end_at"], iso_z(START + timedelta(seconds=120 + 3600)))
        self.advance(120 + 3600)  # exactly at auto_end_at: still in progress
        self.assertEqual(self.active().status_code, 200)
        self.advance(120 + 3601)
        self.assertEqual(self.active().status_code, 404)
        self.assertEqual(self.walk_row(self.walk_id)[:2], ("COMPLETED", "AUTO_TIMEOUT"))

    def test_reservation_end_and_overdue_only_inform_and_never_end_the_walk(self):
        for second in (600, 1800, 3000, 3600 + 600):
            self.advance(second)
            sequence = {600: 0, 1800: 1, 3000: 2, 4200: 3}[second]
            self.assertEqual(self.upload(self.walk_id, [self.point(sequence, second - 1)]).status_code, 200)
        self.assertGreater(START + timedelta(seconds=4200), END)
        body = self.active().json()
        self.assertTrue(body["is_overdue"])
        self.assertEqual(self.walk_row(self.walk_id)[0], "IN_PROGRESS")

    # 3. GPS jump -------------------------------------------------------------------------
    def test_a_gps_jump_is_stored_and_does_not_block_later_batches(self):
        self.advance(60)
        first = self.upload(self.walk_id, [self.point(0, 10), self.point(1, 11, longitude=1)])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.upload(self.walk_id, [self.point(2, 30, longitude=1)]).status_code, 200)
        self.assertEqual(self.upload(self.walk_id, [self.point(2, 30, longitude=1)]).json()["duplicate_count"], 1)
        self.assertEqual(self.upload(self.walk_id, [self.point(4, 40)]).status_code, 409)  # sequence gap stays strict

    # 4. Markers --------------------------------------------------------------------------
    def test_marker_key_replay_conflict_and_legacy_calls(self):
        self.advance(30)
        key = str(uuid4())
        body = {"client_marker_id": key, "latitude": 0, "longitude": 0, "accuracy_m": 5,
                "position_recorded_at": stamp(25)}
        created = self.tap(self.walk_id, seconds=30, **body)
        self.assertEqual(created.status_code, 201, created.text)
        again = self.tap(self.walk_id, seconds=30, **body)
        self.assertEqual((again.status_code, again.json()["id"]), (200, created.json()["id"]))
        changed = self.tap(self.walk_id, seconds=30, **{**body, "marker_type": "POOP"})
        self.assertEqual(changed.status_code, 409)
        for _ in range(2):  # earlier clients without a key keep creating separate markers
            self.assertEqual(self.tap(self.walk_id, seconds=30, latitude=0, longitude=0).status_code, 201)
        self.assertEqual(len(self.markers()), 3)
        self.assertEqual(self.end(self.walk_id).status_code, 200)
        self.assertEqual(self.tap(self.walk_id, seconds=30, **body).status_code, 200)  # replay after end
        self.assertEqual(self.tap(self.walk_id, seconds=30, **{**body, "client_marker_id": str(uuid4())}).status_code,
                         409)

    def test_marker_replay_survives_the_limit_and_other_users_cannot_use_it(self):
        self.advance(30)
        key = str(uuid4())
        body = {"client_marker_id": key, "latitude": 0, "longitude": 0, "accuracy_m": 5}
        with patch.dict(os.environ, {"WALK_MARKER_MAX_PER_WALK": "1"}):
            self.assertEqual(self.tap(self.walk_id, seconds=30, **body).status_code, 201)
            self.assertEqual(self.tap(self.walk_id, seconds=30, **body).status_code, 200)
            self.assertEqual(self.tap(self.walk_id, seconds=30, client_marker_id=str(uuid4()), latitude=0,
                                      longitude=0).status_code, 409)
        stranger = self.tap(self.walk_id, user=2, seconds=30, **body)
        self.assertEqual(stranger.status_code, 403)
        self.assertNotIn(str(self.markers()[0].id), stranger.text)
        self.assertEqual(len(self.markers()), 1)

    def test_marker_location_current_last_known_and_unavailable(self):
        self.advance(200)
        self.upload(self.walk_id, [self.point(0, 100, latitude="0.001", accuracy_m=8)])
        fresh = self.tap(self.walk_id, seconds=130, latitude=0, longitude=0, accuracy_m=10,
                         position_recorded_at=stamp(100))  # 30 s old: still CURRENT
        self.assertEqual(fresh.json()["location_source"], "CURRENT")
        old = self.tap(self.walk_id, seconds=131, latitude=0, longitude=0, accuracy_m=10,
                       position_recorded_at=stamp(100))  # 31 s old: last stored point instead
        self.assertEqual((old.json()["location_source"], old.json()["latitude"]), ("LAST_KNOWN", "0.001000"))
        weak = self.tap(self.walk_id, seconds=140, latitude=0, longitude=0, accuracy_m=60)
        self.assertEqual(weak.json()["location_source"], "LAST_KNOWN")
        missing = self.tap(self.walk_id, seconds=200)  # point is 100 s old: within 2 minutes
        self.assertEqual(missing.json()["location_source"], "LAST_KNOWN")
        self.advance(400)
        stale = self.tap(self.walk_id, seconds=221)  # point is 121 s old
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "MARKER_LOCATION_UNAVAILABLE")
        self.assertEqual(len(self.markers()), 4)
        stored = [m.position_recorded_at for m in self.markers()]
        self.assertEqual(utc(stored[1]), utc(START + timedelta(seconds=100)))

    def test_marker_without_any_good_point_is_refused(self):
        self.advance(100)
        self.upload(self.walk_id, [self.point(0, 50, accuracy_m=80)])  # a stored point, but too inaccurate
        response = self.tap(self.walk_id, seconds=60)
        self.assertEqual(response.json()["detail"]["code"], "MARKER_LOCATION_UNAVAILABLE")
        self.assertEqual(self.markers(), [])

    # 5. Incidents ------------------------------------------------------------------------
    def test_other_incident_text_rules(self):
        self.advance(60)
        for body in ({"incident_type": "OTHER"}, {"incident_type": "OTHER", "description": "   "},
                     {"incident_type": "OTHER", "description": "가" * 1001},
                     {"incident_type": "INJURY", "description": "가" * 1001}):
            with self.subTest(body=body):
                self.assertEqual(self.report(self.walk_id, seconds=10, **body).status_code, 422)
        ok = self.report(self.walk_id, seconds=10, incident_type="OTHER", description="  " + "가" * 1000 + " ")
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(ok.json()["description"], "가" * 1000)
        label = self.report(self.walk_id, seconds=20, incident_type="LOST_DOG")
        self.assertEqual(label.json()["description"], "도주")

    def test_incident_without_any_gps_is_accepted_with_null_coordinates(self):
        self.advance(60)
        response = self.report(self.walk_id, seconds=30)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual((response.json()["latitude"], response.json()["longitude"]), (None, None))

    def test_incident_uses_only_a_fresh_stored_point_and_a_retry_keeps_its_first_result(self):
        self.advance(300)
        self.upload(self.walk_id, [self.point(0, 100, latitude="0.002", accuracy_m=5)])
        key = str(uuid4())
        fresh = self.report(self.walk_id, seconds=130, client_report_id=key)  # point is 30 s old
        self.assertEqual((fresh.status_code, fresh.json()["latitude"]), (201, "0.002000"))
        stale = self.report(self.walk_id, seconds=131, incident_type="LOST_DOG")  # point is 31 s old
        self.assertEqual((stale.status_code, stale.json()["latitude"]), (201, None))
        self.upload(self.walk_id, [self.point(1, 129, latitude="0.009", accuracy_m=5)])  # GPS moved later
        retry = self.report(self.walk_id, seconds=130, client_report_id=key)
        self.assertEqual((retry.status_code, retry.json()["id"], retry.json()["latitude"]),
                         (200, fresh.json()["id"], "0.002000"))
        no_gps_key = str(uuid4())
        first = self.report(self.walk_id, seconds=60, incident_type="POOP_NO_BAG", client_report_id=no_gps_key)
        self.assertEqual(first.json()["latitude"], None)
        again = self.report(self.walk_id, seconds=60, incident_type="POOP_NO_BAG", client_report_id=no_gps_key)
        self.assertEqual((again.status_code, again.json()["id"], again.json()["latitude"]),
                         (200, first.json()["id"], None))
        self.assertEqual(len(self.incidents()), 3)

    def test_incident_keeps_the_walk_running_and_strangers_cannot_report(self):
        self.advance(60)
        self.assertEqual(self.report(self.walk_id, seconds=10).status_code, 201)
        self.assertEqual(self.walk_row(self.walk_id)[0], "IN_PROGRESS")
        self.assertEqual(self.report(self.walk_id, user=2, seconds=11, incident_type="LOST_DOG").status_code, 403)
        self.assertEqual(len(self.incidents()), 1)

    # 6. End and permissions --------------------------------------------------------------
    def test_end_permissions_and_state_after_end(self):
        self.advance(120)
        self.upload(self.walk_id, [self.point(0, 60), self.point(1, 90, longitude="0.0002")])
        self.assertEqual(self.end(self.walk_id, user=2).status_code, 403)
        self.assertEqual(self.end(self.walk_id, user=3).status_code, 403)  # staff may only interrupt
        self.assertEqual(self.walk_row(self.walk_id)[0], "IN_PROGRESS")
        ended = self.end(self.walk_id)
        self.assertEqual((ended.json()["status"], ended.json()["end_reason"]), ("COMPLETED", "USER"))
        self.assertGreater(float(ended.json()["total_distance_m"]), 0)
        before = self.walk_row(self.walk_id)
        self.assertEqual(self.end(self.walk_id).status_code, 409)
        self.assertEqual(self.upload(self.walk_id, [self.point(2, 100)]).status_code, 409)
        self.assertEqual(self.walk_row(self.walk_id), before)
        other = self.begin(dog=2)
        interrupted = self.end(other["id"], user=3, status="INTERRUPTED")
        self.assertEqual((interrupted.json()["status"], interrupted.json()["end_reason"]), ("INTERRUPTED", "STAFF"))

    # 7. Integrated flow ------------------------------------------------------------------
    def test_start_points_marker_incident_and_end_flow(self):
        self.advance(300)
        batch = [self.point(0, 60, accuracy_m=5), self.point(1, 70, longitude=1, accuracy_m=5),
                 self.point(2, 80, longitude="1.0001", accuracy_m=5)]
        self.assertEqual(self.upload(self.walk_id, batch).status_code, 200)
        key = str(uuid4())
        marker = self.tap(self.walk_id, seconds=90, client_marker_id=key, marker_type="OTHER", memo="포토스팟")
        self.assertEqual((marker.status_code, marker.json()["location_source"]), (201, "LAST_KNOWN"))
        self.assertEqual(self.tap(self.walk_id, seconds=90, client_marker_id=key, marker_type="OTHER",
                                  memo="포토스팟").status_code, 200)
        incident = self.report(self.walk_id, seconds=95, incident_type="OTHER", description="리드줄이 풀렸어요")
        self.assertEqual(incident.status_code, 201, incident.text)
        ended = self.end(self.walk_id)
        self.assertEqual(ended.status_code, 200, ended.text)
        detail = self.detail(self.walk_id).json()
        self.assertEqual((len(detail["points"]), len(detail["markers"]), len(detail["incidents"])), (3, 1, 1))
        self.assertEqual((detail["walk"]["status"], detail["marker_count"], detail["incident_count"]),
                         ("COMPLETED", 1, 1))
        with self.Session() as db:
            self.assertEqual(db.get(WalkRequest, self.walk["walk_request_id"]).status, "APPROVED")


if __name__ == "__main__":
    unittest.main()
