"""Acceptance tests for F-11 Walk Overtime Warning (SC-15)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import timedelta
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.models import Walk
from app.routers import shelter_incidents, shelter_live_walks
from app.walk_models import DogWalkSetting
from walk_record_test_support import START, WalkFixture
from walk_request_test_support import END


class WalkOvertimeF11Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(shelter_incidents.router)
        self.app.include_router(shelter_live_walks.router)

    def _set_recommended_duration(self, dog_id: int, minutes: int | None):
        with self.Session.begin() as db:
            setting = db.query(DogWalkSetting).filter(DogWalkSetting.dog_id == dog_id).first()
            if setting is None:
                setting = DogWalkSetting(dog_id=dog_id, walkable=True, recommended_duration_minutes=minutes)
                db.add(setting)
            else:
                setting.recommended_duration_minutes = minutes

    # 1. 산책 시작 시 강아지 권장 산책 시간 기준으로 예정 종료 시각 계산
    def test_expected_end_at_calculated_from_dog_walk_setting(self):
        self._set_recommended_duration(dog_id=1, minutes=30)

        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        expected_end = (START + timedelta(minutes=30)).isoformat()

        self.assertEqual(walk["planned_duration_minutes"], 30)
        self.assertEqual(walk["expected_end_at"].replace("Z", "+00:00"), expected_end)
        self.assertFalse(walk["is_overdue"])
        self.assertEqual(walk["overdue_minutes"], 0)

        # 보호소 실시간 카드 확인
        r_live = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(r_live.status_code, 200)
        card = r_live.json()[0]
        self.assertEqual(card["expected_end_at"].replace("Z", "+00:00"), expected_end)
        self.assertFalse(card["is_overdue"])
        self.assertEqual(card["overdue_minutes"], 0)

        # 사용자 상세/활성 산책 조회 확인
        r_detail = self.client.get(f"/walks/{walk_id}", headers=self.auth(1))
        self.assertEqual(r_detail.status_code, 200)
        self.assertFalse(r_detail.json()["walk"]["is_overdue"])
        self.assertEqual(r_detail.json()["walk"]["overdue_minutes"], 0)

        r_active = self.client.get("/walks/active", headers=self.auth(1))
        self.assertEqual(r_active.status_code, 200)
        self.assertEqual(r_active.json()["planned_duration_minutes"], 30)
        self.assertFalse(r_active.json()["is_overdue"])

    # 2. 권장 산책 시간이 미설정(NULL)이면 승인된 예약 소요시간을 기본값으로 사용
    def test_expected_end_at_falls_back_to_request_duration_when_setting_missing(self):
        self._set_recommended_duration(dog_id=1, minutes=None)

        walk = self.begin(user=1, dog=1)
        expected_minutes = int((END - START).total_seconds() // 60)
        expected_end = (START + timedelta(minutes=expected_minutes)).isoformat()

        self.assertEqual(walk["planned_duration_minutes"], expected_minutes)
        self.assertEqual(walk["expected_end_at"].replace("Z", "+00:00"), expected_end)
        self.assertFalse(walk["is_overdue"])

    # 3. 예정 시간 초과 시 보호소 카드·상세·증분 및 사용자 화면에 경고 표시
    def test_overdue_warning_appears_on_shelter_and_user_views_after_expected_end(self):
        self._set_recommended_duration(dog_id=1, minutes=30)
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 정확히 30분 경과 시점: 아직 초과 아님
        self.advance(30 * 60)
        card_at_limit = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
        self.assertFalse(card_at_limit["is_overdue"])
        self.assertEqual(card_at_limit["overdue_minutes"], 0)

        # 30분 1초 경과 시점: 시간 초과 진입
        self.advance(30 * 60 + 1)
        card_just_over = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
        self.assertTrue(card_just_over["is_overdue"])
        self.assertEqual(card_just_over["overdue_minutes"], 0)

        # 35분 30초 경과 시점: 5분 초과
        self.advance(35 * 60 + 30)
        card_over = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
        self.assertTrue(card_over["is_overdue"])
        self.assertEqual(card_over["overdue_minutes"], 5)

        detail_over = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3)).json()
        self.assertTrue(detail_over["is_overdue"])
        self.assertEqual(detail_over["overdue_minutes"], 5)

        updates_over = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(3)).json()
        self.assertTrue(updates_over["is_overdue"])
        self.assertEqual(updates_over["overdue_minutes"], 5)

        user_detail = self.client.get(f"/walks/{walk_id}", headers=self.auth(1)).json()
        self.assertTrue(user_detail["walk"]["is_overdue"])
        self.assertEqual(user_detail["walk"]["overdue_minutes"], 5)

        user_active = self.client.get("/walks/active", headers=self.auth(1)).json()
        self.assertTrue(user_active["is_overdue"])
        self.assertEqual(user_active["overdue_minutes"], 5)

    # 4. 유예 시간(WALK_OVERDUE_GRACE_MINUTES) 환경변수 적용 검증
    def test_grace_minutes_environment_variable_delays_overdue(self):
        self._set_recommended_duration(dog_id=1, minutes=30)
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        with patch.dict(os.environ, {"WALK_OVERDUE_GRACE_MINUTES": "5"}):
            # 34분 경과: 예정 시간(30분)은 지났지만 유예 5분 이내 -> 초과 아님
            self.advance(34 * 60)
            card_within_grace = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
            self.assertFalse(card_within_grace["is_overdue"])
            self.assertEqual(card_within_grace["overdue_minutes"], 0)

            # 35분 경과: 유예 경계 -> 초과 아님
            self.advance(35 * 60)
            card_at_grace = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
            self.assertFalse(card_at_grace["is_overdue"])

            # 37분 경과: 유예 5분 후 2분 초과 -> is_overdue=True, overdue_minutes=2
            self.advance(37 * 60)
            card_past_grace = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
            self.assertTrue(card_past_grace["is_overdue"])
            self.assertEqual(card_past_grace["overdue_minutes"], 2)

            user_walk = self.client.get(f"/walks/{walk_id}", headers=self.auth(1)).json()["walk"]
            self.assertTrue(user_walk["is_overdue"])
            self.assertEqual(user_walk["overdue_minutes"], 2)

        # 잘못된 환경변수 값은 안전하게 0분으로 폴백
        with patch.dict(os.environ, {"WALK_OVERDUE_GRACE_MINUTES": "invalid"}):
            self.advance(31 * 60)
            card_fallback = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
            self.assertTrue(card_fallback["is_overdue"])
            self.assertEqual(card_fallback["overdue_minutes"], 1)

    # 5. 예외 처리: 산책이 종료되면 경고 자동 해제
    def test_overdue_warning_clears_automatically_when_walk_ends(self):
        self._set_recommended_duration(dog_id=1, minutes=30)
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 40분 경과 (10분 초과 상태)
        self.advance(40 * 60)
        card_before_end = self.client.get("/shelters/me/live-walks", headers=self.auth(3)).json()[0]
        self.assertTrue(card_before_end["is_overdue"])
        self.assertEqual(card_before_end["overdue_minutes"], 10)

        # 산책 종료
        r_end = self.end(walk_id, user=1)
        self.assertEqual(r_end.status_code, 200)
        ended_walk = r_end.json()
        self.assertEqual(ended_walk["status"], "COMPLETED")
        self.assertFalse(ended_walk["is_overdue"])
        self.assertEqual(ended_walk["overdue_minutes"], 0)

        # 산책 상세 조회에서도 경고 자동 해제 확인
        r_detail = self.client.get(f"/walks/{walk_id}", headers=self.auth(1))
        self.assertEqual(r_detail.status_code, 200)
        self.assertFalse(r_detail.json()["walk"]["is_overdue"])
        self.assertEqual(r_detail.json()["walk"]["overdue_minutes"], 0)

        # 보호소 실시간 목록에서 제외됨
        r_live = self.client.get("/shelters/me/live-walks", headers=self.auth(3))
        self.assertEqual(r_live.status_code, 200)
        self.assertEqual(r_live.json(), [])

    # 6. DB 모델 제약조건 검증
    def test_walk_model_constraints_reject_invalid_planned_duration_or_expected_end(self):
        with self.Session() as db:
            walk = db.query(Walk).first()
            if walk is None:
                started = self.begin(user=1, dog=1)
                walk = db.get(Walk, started["id"])

            with self.assertRaises(IntegrityError):
                with db.begin_nested():
                    walk.planned_duration_minutes = 0
                    db.flush()

            with self.assertRaises(IntegrityError):
                with db.begin_nested():
                    walk.expected_end_at = walk.started_at
                    db.flush()


if __name__ == "__main__":
    unittest.main()
