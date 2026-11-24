"""New-activity permission and existing safety operations through real API routes."""
from datetime import timedelta
import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from walk_record_test_support import START, WalkFixture
from app.models import (User, VolunteerOnboardingBasic, VolunteerOnboardingExperience,
                        VolunteerOnboardingRoutine, VolunteerProfile, Walk, WalkRequest)
from app.routers import users


class OnboardingAccessGateTests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(users.router)

    def apply_for_future_walk(self, user=1):
        start = START + timedelta(days=7)
        return self.post(user=user, start=start, end=start + timedelta(hours=1))

    def assert_onboarding_denied(self, response):
        self.assertEqual((response.status_code, response.json()),
                         (403, {"detail": "온보딩을 먼저 완료해 주세요."}))

    def me(self):
        response = self.client.get("/users/me", headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_completed_user_can_apply_and_start(self):
        self.assertTrue(self.me()["onboarding_completed"])
        response = self.apply_for_future_walk()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "PENDING")
        walk = self.begin()
        self.assertEqual(walk["status"], "IN_PROGRESS")

    def test_each_missing_step_blocks_both_new_writes_without_side_effects(self):
        request_id = self.seed_request(status="APPROVED")
        before = self.rows(WalkRequest), self.rows(VolunteerProfile)
        for model, step in ((VolunteerOnboardingBasic, "BASIC_INFORMATION"),
                            (VolunteerOnboardingExperience, "PET_EXPERIENCE"),
                            (VolunteerOnboardingRoutine, "WALKING_ROUTINE")):
            with self.subTest(step=step):
                with self.Session.begin() as db:
                    record = db.get(model, 1)
                    values = {column.name: getattr(record, column.name) for column in model.__table__.columns}
                    db.delete(record)
                self.assertEqual(self.me()["next_onboarding_step"], step)
                self.assert_onboarding_denied(self.apply_for_future_walk())
                self.assert_onboarding_denied(self.start(request_id))
                self.assertEqual((self.rows(WalkRequest), self.rows(VolunteerProfile)), before)
                self.assertEqual(self.rows(Walk), [])
                with self.Session.begin() as db:
                    db.add(model(**values))

    def test_rotated_pledge_blocks_new_writes_until_onboarding_write_succeeds(self):
        request_id = self.seed_request(status="APPROVED")
        before = self.rows(WalkRequest), self.rows(VolunteerProfile)
        version = "walk-fixture-pledge-v2"
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": version}):
            self.assertEqual(self.me()["next_onboarding_step"], "PLEDGE")
            self.assert_onboarding_denied(self.apply_for_future_walk())
            self.assert_onboarding_denied(self.start(request_id))
            self.assertEqual((self.rows(WalkRequest), self.rows(VolunteerProfile)), before)
            self.assertEqual(self.rows(Walk), [])

            response = self.client.put("/users/me/pledge", headers=self.auth(), json={"pledge_version": version})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(self.me()["onboarding_completed"])
            self.assertEqual(self.apply_for_future_walk().status_code, 201)
            self.assertEqual(self.start(request_id).status_code, 201)

    def test_pledge_change_keeps_existing_walk_and_reservation_actions_available(self):
        walk = self.begin()
        walk_id = walk["id"]
        future = START + timedelta(days=7)
        reservation_id = self.seed_request(dog=9, start=future, end=future + timedelta(hours=1))
        self.advance(60)
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "walk-fixture-pledge-v2"}):
            self.assertFalse(self.me()["onboarding_completed"])
            active = self.client.get("/walks/active", headers=self.auth())
            self.assertEqual(active.status_code, 200, active.text)
            self.assertEqual(self.upload(walk_id, [self.point()]).status_code, 200)
            self.assertEqual(self.incident(walk_id, seconds=10).status_code, 201)
            self.assertEqual(self.detail(walk_id).status_code, 200)
            self.assertEqual(self.end(walk_id).status_code, 200)

            detail = self.client.get(f"/walk-requests/{reservation_id}", headers=self.auth())
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(self.act(reservation_id, "CANCEL", user=1).status_code, 200)

    def test_missing_pledge_configuration_fails_closed_for_new_writes(self):
        request_id = self.seed_request(status="APPROVED")
        before = self.rows(WalkRequest), self.rows(VolunteerProfile)
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": ""}):
            for response in (self.apply_for_future_walk(), self.start(request_id)):
                self.assertEqual((response.status_code, response.json()),
                                 (500, {"detail": "일시적인 오류가 발생했습니다."}))
        self.assertEqual((self.rows(WalkRequest), self.rows(VolunteerProfile)), before)
        self.assertEqual(self.rows(Walk), [])

    def test_existing_auth_role_and_account_status_take_precedence(self):
        request_id = self.seed_request(status="APPROVED")
        with self.Session.begin() as db:
            db.get(User, 1).account_status = "AGE_REVIEW_REQUIRED"
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": ""}):
            for actor, status, detail in ((None, 401, "로그인이 필요하거나 세션이 만료되었습니다."),
                                          (3, 403, "권한이 없습니다."), (5, 403, "권한이 없습니다."),
                                          (1, 403, "연령 확인 정보를 먼저 수정해 주세요.")):
                with self.subTest(actor=actor):
                    for response in (self.apply_for_future_walk(actor), self.start(request_id, actor)):
                        self.assertEqual((response.status_code, response.json()), (status, {"detail": detail}))
        self.assertEqual(self.rows(Walk), [])
