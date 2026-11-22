"""Acceptance tests for F-08 Live Walk Tracking (Shelter, SC-15)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.models import Dog, Shelter, User, UserRole, Walk, WalkIncident, WalkPoint, WalkRequest
from app.routers import shelter_incidents, shelter_live_walks, walks
from walk_record_test_support import START, WalkFixture
from walk_request_test_support import END


class LiveWalkF08Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(shelter_incidents.router)
        self.app.include_router(shelter_live_walks.router)

    # 1. 위치 갱신 시 최신 좌표 반환 및 after_seq 증분 조회
    def test_live_walk_location_updates_and_incremental_after_seq(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 산책 시작 직후: 위치 없음
        r_list = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(r_list.status_code, 200)
        cards = r_list.json()
        self.assertEqual(len(cards), 1)
        self.assertIsNone(cards[0]["last_latitude"])
        self.assertIsNone(cards[0]["last_longitude"])
        self.assertIsNone(cards[0]["last_location_at"])

        # 포인트 0 업로드
        self.advance(10)
        p0 = self.point(sequence=0, seconds=10, latitude=37.5665, longitude=126.9780)
        up0 = self.upload(walk_id, [p0], user=1)
        self.assertEqual(up0.status_code, 200)

        # 목록에서 최신 좌표 반영 확인
        r_list2 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        card2 = r_list2.json()[0]
        self.assertEqual(card2["last_latitude"], "37.566500")
        self.assertEqual(card2["last_longitude"], "126.978000")
        self.assertIsNotNone(card2["last_location_at"])

        # 포인트 1 업로드 (10초 경과, 정상 속도 범위 내 14m 이동)
        self.advance(20)
        p1 = self.point(sequence=1, seconds=20, latitude=37.5666, longitude=126.9781)
        up1 = self.upload(walk_id, [p1], user=1)
        self.assertEqual(up1.status_code, 200)

        # 목록에서 다시 최신 좌표 확인
        r_list3 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        card3 = r_list3.json()[0]
        self.assertEqual(card3["last_latitude"], "37.566600")
        self.assertEqual(card3["last_longitude"], "126.978100")

        # 상세 조회: 전체 경로 (2개 포인트)
        r_detail = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3))
        self.assertEqual(r_detail.status_code, 200)
        detail = r_detail.json()
        self.assertEqual(len(detail["route"]), 2)
        self.assertEqual(detail["route"][0]["sequence_no"], 0)
        self.assertEqual(detail["route"][1]["sequence_no"], 1)
        self.assertEqual(detail["last_latitude"], "37.566600")

        # 증분 조회: after_seq=0 -> sequence_no > 0 (1개 포인트만 반환)
        r_up_0 = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates?after_seq=0", headers=self.auth(3))
        self.assertEqual(r_up_0.status_code, 200)
        updates_0 = r_up_0.json()
        self.assertEqual(len(updates_0["route"]), 1)
        self.assertEqual(updates_0["route"][0]["sequence_no"], 1)
        self.assertEqual(updates_0["last_latitude"], "37.566600")

        # 증분 조회: after_seq=1 -> 신규 포인트 없음
        r_up_1 = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates?after_seq=1", headers=self.auth(3))
        self.assertEqual(r_up_1.status_code, 200)
        updates_1 = r_up_1.json()
        self.assertEqual(len(updates_1["route"]), 0)
        self.assertEqual(updates_1["last_latitude"], "37.566600")

        # 증분 조회: after_seq 미지정 -> 전체 경로 반환
        r_up_all = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(3))
        self.assertEqual(r_up_all.status_code, 200)
        self.assertEqual(len(r_up_all.json()["route"]), 2)

    # 2. 산책 종료 시 목록에서 사라지고 상세/증분 조회 불가
    def test_walk_disappears_on_end_or_cancel(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 활성 상태일 때 목록 노출
        r1 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(len(r1.json()), 1)

        # 산책 종료
        self.advance(30)
        r_end = self.end(walk_id, user=1)
        self.assertEqual(r_end.status_code, 200)

        # 목록에서 사라짐
        r2 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(len(r2.json()), 0)

        # 상세 조회 시 404
        r_detail = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3))
        self.assertEqual(r_detail.status_code, 404)

        # 증분 조회 시 404
        r_updates = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(3))
        self.assertEqual(r_updates.status_code, 404)

    # 3. 산책 중인 강아지가 없으면 빈 목록 반환
    def test_empty_list_when_no_active_walks(self):
        # Shelter 1 (산책 시작 전)
        r1 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json(), [])

        # Shelter 2 (소속 산책 없음)
        r2 = self.client.get("/shelters/me/live-walks", headers=self.auth(4))
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json(), [])

    # 4. 60초 이상 위치 갱신 없으면 is_stale=True 판정 및 갱신 시 복구
    def test_is_stale_after_60_seconds_without_update(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # t=0: 산책 시작 직후 (0초 경과) -> is_stale=False
        r0 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertFalse(r0.json()[0]["is_stale"])

        # t=10: 포인트 0 등록
        self.advance(10)
        p0 = self.point(sequence=0, seconds=10, latitude=37.5, longitude=127.0)
        self.upload(walk_id, [p0], user=1)

        # t=20: 10초 경과 -> is_stale=False
        self.advance(20)
        r1 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertFalse(r1.json()[0]["is_stale"])

        # t=71: 포인트 등록(t=10)으로부터 61초 경과 -> is_stale=True
        self.advance(71)
        r_stale = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        card_stale = r_stale.json()[0]
        self.assertTrue(card_stale["is_stale"])
        self.assertIsNotNone(card_stale["last_location_at"])

        # 상세와 증분에서도 is_stale=True 확인
        r_det = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3))
        self.assertTrue(r_det.json()["is_stale"])

        r_up = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(3))
        self.assertTrue(r_up.json()["is_stale"])

        # t=72: 새 포인트 등록 시 즉시 is_stale=False 로 복구
        self.advance(72)
        p1 = self.point(sequence=1, seconds=72, latitude=37.5001, longitude=127.0001)
        self.upload(walk_id, [p1], user=1)

        r_recovered = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertFalse(r_recovered.json()[0]["is_stale"])

        # 포인트가 전혀 없는 산책의 경우 시작 시각 기준 60초 판정
        walk2_req = self.seed_request(user=2, dog=2, status="APPROVED")
        self.start(walk2_req, user=2)
        # 현재 t=72, walk2는 t=72에 시작됨 -> 0초 경과 -> is_stale=False
        r_w2 = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        w2_card = [c for c in r_w2.json() if c["dog_id"] == 2][0]
        self.assertFalse(w2_card["is_stale"])

        # t=135: walk2 시작(72)으로부터 63초 경과 -> is_stale=True
        self.advance(135)
        r_w2_stale = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        w2_stale_card = [c for c in r_w2_stale.json() if c["dog_id"] == 2][0]
        self.assertTrue(w2_stale_card["is_stale"])
        self.assertIsNone(w2_stale_card["last_location_at"])

    # 5. 상세에 START 마커와 F-07 위험 신고 마커 7종 포함
    def test_detail_contains_start_and_incident_markers(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 시작 좌표 업로드
        self.advance(5)
        p0 = self.point(sequence=0, seconds=5, latitude=37.5665, longitude=126.9780)
        self.upload(walk_id, [p0], user=1)

        # F-07 위험 신고 2건 등록
        self.advance(10)
        self.incident(walk_id, user=1, seconds=10, incident_type="INJURY",
                      description="발바닥 찰과상", latitude=37.5666, longitude=126.9781)
        self.advance(15)
        self.incident(walk_id, user=1, seconds=15, incident_type="LOST_DOG",
                      description="줄 놓침", latitude=37.5668, longitude=126.9783)

        # 상세 조회 시 markers 확인
        r_detail = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3))
        self.assertEqual(r_detail.status_code, 200)
        markers = r_detail.json()["markers"]
        self.assertEqual(len(markers), 3)

        # 1번째 마커: START
        self.assertEqual(markers[0]["marker_type"], "START")
        self.assertEqual(markers[0]["latitude"], "37.566500")
        self.assertEqual(markers[0]["longitude"], "126.978000")
        self.assertIsNone(markers[0]["incident_type"])

        # 2번째 마커: INCIDENT (INJURY)
        self.assertEqual(markers[1]["marker_type"], "INCIDENT")
        self.assertEqual(markers[1]["incident_type"], "INJURY")
        self.assertEqual(markers[1]["description"], "발바닥 찰과상")
        self.assertEqual(markers[1]["latitude"], "37.566600")

        # 3번째 마커: INCIDENT (LOST_DOG)
        self.assertEqual(markers[2]["marker_type"], "INCIDENT")
        self.assertEqual(markers[2]["incident_type"], "LOST_DOG")
        self.assertEqual(markers[2]["description"], "줄 놓침")
        self.assertEqual(markers[2]["latitude"], "37.566800")

        # 증분 조회에서도 markers 일치
        r_updates = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates?after_seq=0", headers=self.auth(3))
        self.assertEqual(r_updates.status_code, 200)
        self.assertEqual(len(r_updates.json()["markers"]), 3)

    # 6. 권한 검증: 봉사자 403, 타 보호소 403, 관리자 shelter_id 지정 조회
    def test_authorization_shelter_isolation_and_admin_access(self):
        walk = self.begin(user=1, dog=1)  # Shelter 1 소속 산책
        walk_id = walk["id"]

        # 봉사자(UserRole.USER) 접근 차단 -> 403
        r_vol_list = self.client.get("/shelters/me/live-walks", headers=self.auth(1))
        self.assertEqual(r_vol_list.status_code, 403)

        r_vol_detail = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(1))
        self.assertEqual(r_vol_detail.status_code, 403)

        r_vol_updates = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(1))
        self.assertEqual(r_vol_updates.status_code, 403)

        # 타 보호소(UserRole.SHELTER, shelter_id=2) 접근 차단
        # 목록: Shelter 2에는 산책이 없으므로 빈 목록
        r_sh2_list = self.client.get("/shelters/me/live-walks", headers=self.auth(4))
        self.assertEqual(r_sh2_list.status_code, 200)
        self.assertEqual(r_sh2_list.json(), [])

        # 타 보호소 지정 쿼리 시도시 -> 403
        r_sh2_tamper = self.client.get("/shelters/me/live-walks?shelter_id=1", headers=self.auth(4))
        self.assertEqual(r_sh2_tamper.status_code, 403)

        # Shelter 1 산책 상세 접근 -> 403
        r_sh2_detail = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(4))
        self.assertEqual(r_sh2_detail.status_code, 403)

        # Shelter 1 산책 증분 접근 -> 403
        r_sh2_updates = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(4))
        self.assertEqual(r_sh2_updates.status_code, 403)

        # 관리자(UserRole.ADMIN) 조회
        # shelter_id 미지정 시 400
        r_admin_no_id = self.client.get("/shelters/me/live-walks", headers=self.auth(5))
        self.assertEqual(r_admin_no_id.status_code, 400)

        # shelter_id=1 지정 시 200 및 산책 반환
        r_admin_sh1 = self.client.get("/shelters/me/live-walks?shelter_id=1", headers=self.auth(5))
        self.assertEqual(r_admin_sh1.status_code, 200)
        self.assertEqual(len(r_admin_sh1.json()), 1)

        # shelter_id=2 지정 시 200 및 빈 목록
        r_admin_sh2 = self.client.get("/shelters/me/live-walks?shelter_id=2", headers=self.auth(5))
        self.assertEqual(r_admin_sh2.status_code, 200)
        self.assertEqual(len(r_admin_sh2.json()), 0)

        # 관리자는 어느 보호소 산책 상세/증분이든 조회 가능
        r_admin_det = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(5))
        self.assertEqual(r_admin_det.status_code, 200)

        r_admin_up = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(5))
        self.assertEqual(r_admin_up.status_code, 200)

    # 7. 전화번호 노출 최소화: 상세 응답에만 포함되고 목록 및 증분에는 미포함
    def test_walker_phone_only_in_detail_response(self):
        # User 1에게 전화번호 부여
        with self.Session.begin() as db:
            user1 = db.get(User, 1)
            user1.phone = "010-9876-5432"

        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 카드 목록 응답: walker_phone 키 자체가 없어야 함
        r_list = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(r_list.status_code, 200)
        card = r_list.json()[0]
        self.assertNotIn("walker_phone", card)
        self.assertEqual(card["walker_nickname"], "사용자1")

        # 증분 응답: walker_phone 키 자체가 없어야 함
        r_updates = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(3))
        self.assertEqual(r_updates.status_code, 200)
        up_data = r_updates.json()
        self.assertNotIn("walker_phone", up_data)

        # 상세 응답: walker_phone 포함
        r_detail = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3))
        self.assertEqual(r_detail.status_code, 200)
        detail = r_detail.json()
        self.assertEqual(detail["walker_phone"], "010-9876-5432")
        self.assertEqual(detail["walker_nickname"], "사용자1")

    # 8. 카드 목록 응답 필수 필드 및 경과 시간 계산 검증
    def test_live_walk_card_fields_and_elapsed_time(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 125초 경과
        self.advance(125)

        r_list = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(r_list.status_code, 200)
        card = r_list.json()[0]

        self.assertEqual(card["walk_id"], walk_id)
        self.assertEqual(card["dog_id"], 1)
        self.assertEqual(card["dog_name"], "강아지1")
        self.assertEqual(card["walker_nickname"], "사용자1")
        self.assertEqual(card["elapsed_seconds"], 125)
        self.assertEqual(card["scheduled_duration_minutes"], 60)
        self.assertEqual(card["scheduled_end_at"].replace("Z", "+00:00"), END.isoformat())
        self.assertIn("started_at", card)
        self.assertIn("is_stale", card)
