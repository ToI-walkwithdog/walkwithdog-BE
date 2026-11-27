"""Acceptance and regression tests for F-06 real-time walk tracking & restoration."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.models import VolunteerProfile, Walk, WalkPoint
from app.walk_service import as_utc, close_stale_walks
from walk_record_test_support import START, WalkFixture


class WalkTrackingF06Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)

    # 1. 좌표 묶음 저장 후 GET /walks/{id}에서 순서대로 경로가 조회된다.
    def test_path_retrieval_in_order(self):
        walk = self.begin()
        self.advance(60)
        points = [
            self.point(0, 10, latitude="37.500000", longitude="127.000000"),
            self.point(1, 20, latitude="37.500100", longitude="127.000100"),
            self.point(2, 30, latitude="37.500200", longitude="127.000200"),
        ]
        upload_resp = self.upload(walk["id"], points)
        self.assertEqual(upload_resp.status_code, 200)

        detail_resp = self.detail(walk["id"])
        self.assertEqual(detail_resp.status_code, 200)
        data = detail_resp.json()
        self.assertEqual(len(data["points"]), 3)
        for idx, pt in enumerate(data["points"]):
            self.assertEqual(pt["sequence_no"], idx)
        self.assertEqual(data["points"][0]["latitude"], "37.500000")
        self.assertEqual(data["points"][2]["latitude"], "37.500200")

    # 2. 묶음 사이에 시간 공백이 있어도 이어서 저장되고 last_*가 갱신된다.
    def test_batch_continuation_with_gap(self):
        walk = self.begin()
        # Batch 1
        self.advance(30)
        batch1 = [
            self.point(0, 10, latitude="37.500000", longitude="127.000000"),
            self.point(1, 20, latitude="37.500010", longitude="127.000010"),
        ]
        self.assertEqual(self.upload(walk["id"], batch1).status_code, 200)

        # Time gap: advance by 300s
        self.advance(330)
        batch2 = [
            self.point(2, 300, latitude="37.500020", longitude="127.000020", accuracy_m=12),
            self.point(3, 310, latitude="37.500030", longitude="127.000030", accuracy_m=15),
        ]
        self.assertEqual(self.upload(walk["id"], batch2).status_code, 200)

        # Check walk last_*
        with self.Session() as db:
            row = db.get(Walk, walk["id"])
            self.assertEqual(Decimal(str(row.last_latitude)), Decimal("37.500030"))
            self.assertEqual(Decimal(str(row.last_longitude)), Decimal("127.000030"))
            self.assertEqual(Decimal(str(row.last_accuracy_m)), Decimal("15.00"))
            self.assertIsNotNone(row.last_point_recorded_at)
            self.assertIsNotNone(row.last_point_received_at)

    # 3. 정확도 50m 초과 좌표가 묶음을 거부시키지 않고 저장되며, 총 거리 계산에서는 제외된다.
    def test_accuracy_over_50m_accepted_and_excluded_from_distance(self):
        walk = self.begin()
        self.advance(60)
        # Point 0: reliable (acc 10)
        # Point 1: 51m accuracy (acc 60 > 50m), displacement ~11m
        # Point 2: reliable (acc 10)
        points = [
            self.point(0, 10, latitude="37.500000", longitude="127.000000", accuracy_m=10),
            self.point(1, 20, latitude="37.500100", longitude="127.000000", accuracy_m=60),
            self.point(2, 30, latitude="37.500100", longitude="127.000000", accuracy_m=10),
        ]
        resp = self.upload(walk["id"], points)
        self.assertEqual(resp.status_code, 200)

        # Verify all 3 points stored in DB
        with self.Session() as db:
            pts = db.query(WalkPoint).filter_by(walk_id=walk["id"]).all()
            self.assertEqual(len(pts), 3)

        # End walk and verify distance
        end_resp = self.end(walk["id"])
        self.assertEqual(end_resp.status_code, 200)
        # Because point 1 has accuracy 60m, segments (0->1) and (1->2) are unreliable,
        # so total_distance_m should be 0.00
        self.assertEqual(Decimal(str(end_resp.json()["total_distance_m"])), Decimal("0.00"))

    # 4. 이후 좌표가 오지 않아도 마지막 위치와 last_point_received_at이 유지된다.
    def test_last_position_and_received_at_retained(self):
        walk = self.begin()
        self.advance(100)
        self.assertEqual(self.upload(walk["id"], [
            self.point(0, 10, latitude="37.500500", longitude="127.000500", accuracy_m=8)
        ]).status_code, 200)

        with self.Session() as db:
            row1 = db.get(Walk, walk["id"])
            saved_received_at = row1.last_point_received_at
            saved_lat = row1.last_latitude

        # Advance time by 500s without new points
        self.advance(600)
        with self.Session() as db:
            row2 = db.get(Walk, walk["id"])
            self.assertEqual(row2.last_point_received_at, saved_received_at)
            self.assertEqual(row2.last_latitude, saved_lat)

    # 5. GET /walks/active가 walk_id, 마지막 위치, last_sequence_no를 반환하고, 이어서 last_sequence_no + 1로 전송 성공. 없으면 404.
    def test_active_walk_restoration_and_next_sequence(self):
        # When no active walk
        resp404 = self.client.get("/walks/active", headers=self.auth(1))
        self.assertEqual(resp404.status_code, 404)

        # Begin walk and send 2 points
        walk = self.begin(user=1)
        self.advance(30)
        self.assertEqual(self.upload(walk["id"], [
            self.point(0, 10, latitude="37.500100", longitude="127.000100", accuracy_m=10),
            self.point(1, 20, latitude="37.500200", longitude="127.000200", accuracy_m=10),
        ]).status_code, 200)

        # Query GET /walks/active
        active_resp = self.client.get("/walks/active", headers=self.auth(1))
        self.assertEqual(active_resp.status_code, 200)
        active_data = active_resp.json()
        self.assertEqual(active_data["walk_id"], walk["id"])
        self.assertEqual(active_data["last_sequence_no"], 1)
        self.assertEqual(Decimal(str(active_data["last_latitude"])), Decimal("37.500200"))
        self.assertGreaterEqual(active_data["elapsed_seconds"], 20)

        # App continues by sending sequence_no 2 (last_sequence_no + 1)
        next_point = self.point(2, 30, latitude="37.500300", longitude="127.000300", accuracy_m=10)
        cont_resp = self.upload(walk["id"], [next_point])
        self.assertEqual(cont_resp.status_code, 200)
        self.assertEqual(cont_resp.json()["last_sequence_no"], 2)

    # 6. 마지막 좌표 수신 후 1시간 경과 → 점검 시 COMPLETED, ended_at == last_point_received_at, end_reason == AUTO_TIMEOUT, 봉사 이력 반영
    def test_auto_end_stale_walk_after_1_hour(self):
        walk = self.begin(user=1)
        self.advance(60)
        self.upload(walk["id"], [self.point(0, 10)])

        # Advance past 3600 seconds (1 hour)
        self.advance(60 + 3601)

        with self.Session.begin() as db:
            closed = close_stale_walks(db)
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0].id, walk["id"])

        with self.Session() as db:
            row = db.get(Walk, walk["id"])
            self.assertEqual(row.status, "COMPLETED")
            self.assertEqual(row.end_reason, "AUTO_TIMEOUT")
            self.assertEqual(row.ended_at, row.last_point_received_at)
            self.assertIsNotNone(row.duration_seconds)

            # Volunteer profile updated
            profile = db.get(VolunteerProfile, 1)
            self.assertEqual(profile.total_walk_count, 1)

    # 7. 1시간 미만이면 종료되지 않는다. 좌표를 받은 적 없는 산책은 시작 시각 기준으로 만료된다.
    def test_not_expired_under_1_hour_and_no_points_start_basis(self):
        walk1 = self.begin(user=1, dog=1)
        self.advance(1800)  # 30 mins < 1 hour
        with self.Session.begin() as db:
            closed = close_stale_walks(db)
            self.assertEqual(len(closed), 0)

        with self.Session() as db:
            self.assertEqual(db.get(Walk, walk1["id"]).status, "IN_PROGRESS")

        # Walk without points expires after 3600s from started_at
        self.advance(3601)
        with self.Session.begin() as db:
            closed = close_stale_walks(db)
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0].id, walk1["id"])

        with self.Session() as db:
            row = db.get(Walk, walk1["id"])
            self.assertEqual(row.status, "COMPLETED")
            self.assertEqual(row.end_reason, "AUTO_TIMEOUT")
            self.assertEqual(row.ended_at, row.started_at)

    # 8. 점검을 두 번 실행해도 결과가 같다. (idempotent)
    def test_close_stale_walks_idempotency(self):
        walk = self.begin(user=1)
        self.advance(3700)

        with self.Session.begin() as db:
            closed1 = close_stale_walks(db)
            self.assertEqual(len(closed1), 1)

        with self.Session.begin() as db:
            closed2 = close_stale_walks(db)
            self.assertEqual(len(closed2), 0)

        with self.Session() as db:
            row = db.get(Walk, walk["id"])
            self.assertEqual(row.status, "COMPLETED")
            profile = db.get(VolunteerProfile, 1)
            # Ensure not incremented twice
            self.assertEqual(profile.total_walk_count, 1)

    # 9. 자동 종료된 산책에 좌표를 보내면 409이다. 수동 종료의 end_reason은 USER, 안전 중단은 STAFF이다.
    def test_send_points_to_auto_ended_walk_409_and_reasons(self):
        walk = self.begin(user=1)
        self.advance(3700)
        with self.Session.begin() as db:
            close_stale_walks(db)

        # Uploading to auto-ended walk returns 409
        self.assertEqual(self.upload(walk["id"], [self.point(0, 10)]).status_code, 409)

        # Reset clock for next walks within reservation window
        self.advance(0)

        # Manual user completion sets end_reason = 'USER'
        walk_user = self.begin(user=1, dog=2)
        self.advance(60)
        end_resp_user = self.end(walk_user["id"], user=1)
        self.assertEqual(end_resp_user.status_code, 200)
        self.assertEqual(end_resp_user.json()["end_reason"], "USER")

        # Staff safety interruption sets end_reason = 'STAFF'
        self.advance(0)
        walk_staff = self.begin(user=1, dog=8)
        self.advance(60)
        end_resp_staff = self.end(walk_staff["id"], user=3, status="INTERRUPTED")
        self.assertEqual(end_resp_staff.status_code, 200)
        self.assertEqual(end_resp_staff.json()["end_reason"], "STAFF")

    # 10. 회귀: 예약 시간대 시작 규칙, 동시 진행 1개 제한, 조회 권한이 그대로다.
    def test_regression_guard_rules(self):
        # 1. Starting walk outside approved window fails
        req_id = self.seed_request(user=1, dog=1, status="APPROVED",
                                   start=START + timedelta(days=5), end=START + timedelta(days=5, hours=1))
        # Current time is START, so starting day 5 request fails with 409
        self.assertEqual(self.start(req_id, user=1).status_code, 409)

        # 2. Concurrency limit: only 1 in-progress walk allowed per volunteer
        w1 = self.begin(user=1, dog=1)
        req_id2 = self.seed_request(user=1, dog=2, status="APPROVED")
        self.assertEqual(self.start(req_id2, user=1).status_code, 409)

        # 3. Access permissions: other volunteers cannot view walk detail
        self.assertEqual(self.detail(w1["id"], user=2).status_code, 403)
        self.assertEqual(self.detail(w1["id"], user=1).status_code, 200)
