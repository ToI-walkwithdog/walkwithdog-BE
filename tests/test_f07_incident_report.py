"""Acceptance and regression tests for F-07 danger report (SC-07, SC-15)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.models import Dog, Shelter, User, UserRole, Walk, WalkIncident, WalkPoint, WalkRequest
from app.routers import shelter_incidents, walks
from app.walk_service import as_utc, close_stale_walks
from walk_record_test_support import START, WalkFixture


class WalkIncidentF07Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(shelter_incidents.router)

    # 1. 신고 5종 정상 저장, status OPEN, 상세 공백 시 한글 라벨 자동 저장
    def test_report_incident_success_and_default_korean_label(self):
        walk = self.begin()
        self.advance(60)

        # 1) INJURY - 상세 내용 직접 입력
        r1 = self.incident(walk["id"], incident_type="INJURY", description="다리를 살짝 삠", latitude=37.5, longitude=127.0)
        self.assertEqual(r1.status_code, 201)
        data1 = r1.json()
        self.assertEqual(data1["incident_type"], "INJURY")
        self.assertEqual(data1["description"], "다리를 살짝 삠")
        self.assertEqual(data1["status"], "OPEN")
        self.assertEqual(data1["latitude"], "37.500000")
        self.assertEqual(data1["longitude"], "127.000000")

        # 2) LOST_DOG - 빈 문자열 상세
        r2 = self.incident(walk["id"], incident_type="LOST_DOG", description="", latitude=37.5, longitude=127.0)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.json()["description"], "도주")

        # 3) SAFETY_THREAT - 공백 상세
        r3 = self.incident(walk["id"], incident_type="SAFETY_THREAT", description="   ", latitude=37.5, longitude=127.0)
        self.assertEqual(r3.status_code, 201)
        self.assertEqual(r3.json()["description"], "안전 위협")

        # 4) POOP_NO_BAG - 상세 필드 미제공
        r4 = self.client.post(
            f"/walks/{walk['id']}/incidents",
            headers=self.auth(1),
            json={"incident_type": "POOP_NO_BAG", "occurred_at": (START + timedelta(seconds=10)).isoformat(), "latitude": 37.5, "longitude": 127.0},
        )
        self.assertEqual(r4.status_code, 201)
        self.assertEqual(r4.json()["description"], "똥 쌈(배변봉투 없음)")

        # 5) ATE_FOREIGN_OBJECT - 상세 필드 미제공
        r5 = self.client.post(
            f"/walks/{walk['id']}/incidents",
            headers=self.auth(1),
            json={"incident_type": "ATE_FOREIGN_OBJECT", "occurred_at": (START + timedelta(seconds=10)).isoformat(), "latitude": 37.5, "longitude": 127.0},
        )
        self.assertEqual(r5.status_code, 201)
        self.assertEqual(r5.json()["description"], "이상한 것 주워먹음")

        with self.Session() as db:
            count = db.query(WalkIncident).filter_by(walk_id=walk["id"]).count()
            self.assertEqual(count, 5)

    # 2. 필수 유형 누락 및 미지원 유형 422
    def test_invalid_incident_type_returns_422(self):
        walk = self.begin()
        self.advance(60)

        # 유형 누락
        r1 = self.client.post(
            f"/walks/{walk['id']}/incidents",
            headers=self.auth(1),
            json={"description": "내용", "occurred_at": START.isoformat()},
        )
        self.assertEqual(r1.status_code, 422)

        # 미지원 유형
        r2 = self.incident(walk["id"], incident_type="UNSUPPORTED_TYPE")
        self.assertEqual(r2.status_code, 422)

        with self.Session() as db:
            self.assertEqual(db.query(WalkIncident).filter_by(walk_id=walk["id"]).count(), 0)

    # 3. 해당 보호소 계정의 알림 조회 및 after_id 필터
    def test_shelter_incident_alerts_polling(self):
        walk = self.begin()
        self.advance(60)

        self.incident(walk["id"], incident_type="INJURY", description="부상1", latitude=37.5, longitude=127.0)
        self.incident(walk["id"], incident_type="LOST_DOG", description="도주1", latitude=37.5, longitude=127.0)

        # User 3 (Staff of Shelter 1)
        alerts_resp = self.client.get("/shelters/me/incident-alerts", headers=self.auth(3))
        self.assertEqual(alerts_resp.status_code, 200)
        alerts = alerts_resp.json()
        self.assertEqual(len(alerts), 2)
        first_alert = alerts[0]
        self.assertEqual(first_alert["incident_type"], "INJURY")
        self.assertEqual(first_alert["description"], "부상1")
        self.assertEqual(first_alert["dog"]["id"], 1)
        self.assertTrue(bool(first_alert["dog"]["name"]))
        self.assertTrue(bool(first_alert["reporter_nickname"]))
        self.assertIsNone(first_alert["acknowledged_at"])

        # after_id 필터링
        after_resp = self.client.get(f"/shelters/me/incident-alerts?after_id={first_alert['id']}", headers=self.auth(3))
        self.assertEqual(after_resp.status_code, 200)
        after_data = after_resp.json()
        self.assertEqual(len(after_data), 1)
        self.assertEqual(after_data[0]["incident_type"], "LOST_DOG")

    # 4. 타 보호소 알림 격리 및 일반 사용자/관리자 접근 제한 (403), 전화번호 null 확인
    def test_incident_alerts_authorization(self):
        walk = self.begin()
        self.advance(60)
        self.incident(walk["id"], incident_type="INJURY", latitude=37.5, longitude=127.0)

        # 타 보호소 직원 (User 4: Shelter 2)
        r_other = self.client.get("/shelters/me/incident-alerts", headers=self.auth(4))
        self.assertEqual(r_other.status_code, 200)
        self.assertEqual(r_other.json(), [])

        # 봉사자 (User 1) -> 403
        r_volunteer = self.client.get("/shelters/me/incident-alerts", headers=self.auth(1))
        self.assertEqual(r_volunteer.status_code, 403)

        # 관리자 (User 5) -> 403
        r_admin = self.client.get("/shelters/me/incident-alerts", headers=self.auth(5))
        self.assertEqual(r_admin.status_code, 403)

        # 전화번호 null 보호소 처리
        with self.Session() as db:
            shelter = db.get(Shelter, 1)
            shelter.phone = None
            db.commit()

        r_no_phone = self.client.get("/shelters/me/incident-alerts", headers=self.auth(3))
        self.assertEqual(r_no_phone.status_code, 200)
        self.assertIsNone(r_no_phone.json()[0]["shelter_phone"])

    # 5. 알림 확인 처리 멱등성 및 이력 보존, 타 보호소 확인 불가
    def test_acknowledge_incident_alert_idempotent(self):
        walk = self.begin()
        self.advance(60)
        inc_resp = self.incident(walk["id"], incident_type="INJURY", latitude=37.5, longitude=127.0)
        inc_id = inc_resp.json()["id"]

        # 타 보호소 직원은 확인 불가 (403)
        r_other_ack = self.client.post(f"/shelters/me/incident-alerts/{inc_id}/ack", headers=self.auth(4))
        self.assertEqual(r_other_ack.status_code, 403)

        # 해당 보호소 직원 확인 처리 (200)
        r_ack = self.client.post(f"/shelters/me/incident-alerts/{inc_id}/ack", headers=self.auth(3))
        self.assertEqual(r_ack.status_code, 200)
        ack_data = r_ack.json()
        self.assertIsNotNone(ack_data["acknowledged_at"])
        first_ack_time = ack_data["acknowledged_at"]

        # 미확인 목록에서 제거됨
        alerts = self.client.get("/shelters/me/incident-alerts", headers=self.auth(3)).json()
        self.assertEqual(len(alerts), 0)

        # 반복 확인 처리 시 200 및 최초 확인 시각 보존 (멱등)
        self.advance(100)
        r_repeat_ack = self.client.post(f"/shelters/me/incident-alerts/{inc_id}/ack", headers=self.auth(3))
        self.assertEqual(r_repeat_ack.status_code, 200)
        self.assertEqual(r_repeat_ack.json()["acknowledged_at"], first_ack_time)

        # 이력 목록에는 유지됨
        history = self.client.get("/shelters/me/incidents", headers=self.auth(3)).json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], inc_id)

    # 6. 중복 신고: 3분 내 동일 유형 409 (DUPLICATE_RECENT), confirm_duplicate 재전송 시 201
    def test_duplicate_incident_window_and_confirmation(self):
        walk = self.begin()
        self.advance(60)

        # 첫 번째 신고
        r1 = self.incident(walk["id"], incident_type="INJURY", seconds=10, latitude=37.5, longitude=127.0)
        self.assertEqual(r1.status_code, 201)
        inc_id = r1.json()["id"]

        # 3분(180초) 이내 동일 유형 재전송 (seconds=50) -> 409
        r2 = self.incident(walk["id"], incident_type="INJURY", seconds=50, latitude=37.5, longitude=127.0)
        self.assertEqual(r2.status_code, 409)
        err = r2.json()["detail"]
        self.assertEqual(err["code"], "DUPLICATE_RECENT")
        self.assertEqual(err["duplicate_incident_id"], inc_id)

        # confirm_duplicate=True 재전송 -> 201
        r3 = self.incident(walk["id"], incident_type="INJURY", seconds=50, confirm_duplicate=True, latitude=37.5, longitude=127.0)
        self.assertEqual(r3.status_code, 201)

        # 다른 유형은 3분 이내라도 즉시 201
        r4 = self.incident(walk["id"], incident_type="LOST_DOG", seconds=55, latitude=37.5, longitude=127.0)
        self.assertEqual(r4.status_code, 201)

        # 3분(180초) 초과 후에는 동일 유형도 즉시 201 (seconds=250, 차이 200초 > 180초)
        self.advance(260)
        r5 = self.incident(walk["id"], incident_type="INJURY", seconds=250, latitude=37.5, longitude=127.0)
        self.assertEqual(r5.status_code, 201)

    # 7. client_report_id 멱등성: 동일 내용 200 반환, 상이 내용 409
    def test_client_report_id_idempotency(self):
        walk = self.begin()
        self.advance(60)
        client_id = "test-uuid-123456"

        payload = {
            "incident_type": "INJURY",
            "description": "발목 긁힘",
            "occurred_at": (START + timedelta(seconds=10)).isoformat(),
            "latitude": 37.5,
            "longitude": 127.0,
            "client_report_id": client_id,
        }

        # 1차 전송: 201 Created
        r1 = self.client.post(f"/walks/{walk['id']}/incidents", headers=self.auth(1), json=payload)
        self.assertEqual(r1.status_code, 201)
        first_id = r1.json()["id"]

        # 2차 전송 (동일 내용 재전송): 200 OK & 동일 레코드 반환
        r2 = self.client.post(f"/walks/{walk['id']}/incidents", headers=self.auth(1), json=payload)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["id"], first_id)

        # 3차 전송 (동일 client_report_id지만 내용 변경): 409 Conflict
        conflicting_payload = {**payload, "description": "다른 부상 내용"}
        r3 = self.client.post(f"/walks/{walk['id']}/incidents", headers=self.auth(1), json=conflicting_payload)
        self.assertEqual(r3.status_code, 409)

        # DB에는 1건만 존재
        with self.Session() as db:
            self.assertEqual(db.query(WalkIncident).filter_by(reported_by=1, client_report_id=client_id).count(), 1)

    # 8. 종료 후 늦은 신고: 24시간 이내 산책 기간 내 201, 24시간 초과/기간 외/CANCELLED 409
    def test_late_report_for_ended_walk(self):
        walk = self.begin()
        self.advance(120)
        end_resp = self.end(walk["id"])
        self.assertEqual(end_resp.status_code, 200)
        self.assertEqual(end_resp.json()["status"], "COMPLETED")

        # 종료 1시간 후, 산책 진행 중 발생 시각 (seconds=60) -> 201
        self.advance(3600)
        r_valid = self.incident(walk["id"], incident_type="INJURY", seconds=60, latitude=37.5, longitude=127.0)
        self.assertEqual(r_valid.status_code, 201)

        # 발생 시각이 산책 종료 시각(seconds=120) 이후인 경우 -> 409
        r_after_end = self.incident(walk["id"], incident_type="LOST_DOG", seconds=130, latitude=37.5, longitude=127.0)
        self.assertEqual(r_after_end.status_code, 409)

        # 24시간 초과 후 신고 -> 409
        self.advance(25 * 3600)
        r_too_late = self.incident(walk["id"], incident_type="SAFETY_THREAT", seconds=60, latitude=37.5, longitude=127.0)
        self.assertEqual(r_too_late.status_code, 409)

        # 자동 종료(AUTO_TIMEOUT)된 산책에 대한 늦은 신고 검증
        self.advance(0)
        walk2 = self.begin(dog=2)
        self.advance(3700)
        with self.Session() as db:
            close_stale_walks(db)
        # 자동 종료 후 24시간 이내 산책 시간대 발생 건 -> 201
        r_auto_ended = self.incident(walk2["id"], incident_type="INJURY", seconds=60, latitude=37.5, longitude=127.0)
        self.assertEqual(r_auto_ended.status_code, 201)

    # 9. 좌표 없는 신고는 walks.last_latitude/longitude로 저장, 마지막 위치도 없으면 NULL
    def test_fallback_to_walk_last_coordinates(self):
        walk = self.begin()
        self.advance(30)

        # 1) 마지막 위치 없는 상태에서 좌표 없이 신고 -> NULL 저장
        r1 = self.client.post(
            f"/walks/{walk['id']}/incidents",
            headers=self.auth(1),
            json={"incident_type": "INJURY", "occurred_at": START.isoformat()},
        )
        self.assertEqual(r1.status_code, 201)
        self.assertIsNone(r1.json()["latitude"])
        self.assertIsNone(r1.json()["longitude"])

        # 2) GPS 좌표 묶음 업로드하여 last_* 위치 생성
        points = [self.point(0, 10, latitude="37.555555", longitude="127.066666")]
        self.upload(walk["id"], points)

        # 좌표 없이 신고 -> walks.last_*로 대체 저장
        r2 = self.client.post(
            f"/walks/{walk['id']}/incidents",
            headers=self.auth(1),
            json={"incident_type": "LOST_DOG", "occurred_at": (START + timedelta(seconds=20)).isoformat()},
        )
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.json()["latitude"], "37.555555")
        self.assertEqual(r2.json()["longitude"], "127.066666")

    # 10. 보호소 신고 이력 목록 필터링 및 관리자 조회
    def test_shelter_incidents_history_filters(self):
        walk = self.begin()
        self.advance(60)

        # 3건의 신고 등록
        r1 = self.incident(walk["id"], incident_type="INJURY", description="부상", latitude=37.5, longitude=127.0)
        r2 = self.incident(walk["id"], incident_type="LOST_DOG", description="도주", latitude=37.5, longitude=127.0)
        r3 = self.incident(walk["id"], incident_type="SAFETY_THREAT", description="위협", latitude=37.5, longitude=127.0)

        # 1건 확인 처리
        self.client.post(f"/shelters/me/incident-alerts/{r1.json()['id']}/ack", headers=self.auth(3))

        # 전체 이력 조회
        all_hist = self.client.get("/shelters/me/incidents", headers=self.auth(3)).json()
        self.assertEqual(len(all_hist), 3)

        # 유형 필터
        inj_hist = self.client.get("/shelters/me/incidents?incident_type=INJURY", headers=self.auth(3)).json()
        self.assertEqual(len(inj_hist), 1)
        self.assertEqual(inj_hist[0]["incident_type"], "INJURY")

        # 확인 여부 필터
        ack_hist = self.client.get("/shelters/me/incidents?acknowledged=true", headers=self.auth(3)).json()
        self.assertEqual(len(ack_hist), 1)
        self.assertEqual(ack_hist[0]["id"], r1.json()["id"])

        unack_hist = self.client.get("/shelters/me/incidents?acknowledged=false", headers=self.auth(3)).json()
        self.assertEqual(len(unack_hist), 2)

        # limit / offset 페이지네이션
        page1 = self.client.get("/shelters/me/incidents?limit=1&offset=0", headers=self.auth(3)).json()
        self.assertEqual(len(page1), 1)

        # 관리자(User 5)의 shelter_id 조회
        admin_missing_shelter = self.client.get("/shelters/me/incidents", headers=self.auth(5))
        self.assertEqual(admin_missing_shelter.status_code, 422)

        admin_resp = self.client.get("/shelters/me/incidents?shelter_id=1", headers=self.auth(5))
        self.assertEqual(admin_resp.status_code, 200)
        self.assertEqual(len(admin_resp.json()), 3)

    # 11. 동일 산책 동일 유형 동시 신고 레이스 컨디션 방어
    def test_concurrent_incident_race_condition(self):
        walk = self.begin()
        self.advance(60)

        winner, loser = self.recording_race(
            lambda: self.incident(walk["id"], user=1, incident_type="INJURY", latitude=37.5, longitude=127.0),
            lambda: self.incident(walk["id"], user=3, incident_type="INJURY", latitude=37.5, longitude=127.0),
            hook="app.walk_service.locked_walk_for_incident",
        )
        self.assertEqual({winner.status_code, loser.status_code}, {201, 409})
        err_resp = winner if winner.status_code == 409 else loser
        self.assertEqual(err_resp.json()["detail"]["code"], "DUPLICATE_RECENT")

    # 12. 회귀 검증: 권한(본인/해당보호소/관리자), GET /walks/{id} incidents 조회
    def test_regression_rules(self):
        walk = self.begin()
        self.advance(60)

        # 허용된 사용자들 (본인 1, 보호소직원 3, 관리자 5)
        self.assertEqual(self.incident(walk["id"], user=1, incident_type="INJURY", latitude=37.5, longitude=127.0).status_code, 201)
        self.assertEqual(self.incident(walk["id"], user=3, incident_type="LOST_DOG", latitude=37.5, longitude=127.0).status_code, 201)
        self.assertEqual(self.incident(walk["id"], user=5, incident_type="SAFETY_THREAT", latitude=37.5, longitude=127.0).status_code, 201)

        # 타 사용자(User 2) 거부 (403)
        self.assertEqual(self.incident(walk["id"], user=2, incident_type="OTHER").status_code, 403)

        # GET /walks/{id} incidents 필드 검증
        detail = self.detail(walk["id"]).json()
        self.assertEqual(len(detail["incidents"]), 3)
        self.assertTrue(all("client_report_id" in inc for inc in detail["incidents"]))
        self.assertTrue(all("acknowledged_at" in inc for inc in detail["incidents"]))
