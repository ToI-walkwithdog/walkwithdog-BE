"""GET /users/me onboarding progress (Issue #45) on isolated SQLite with synthetic accounts.

One snapshot, concurrency and the statement count on PostgreSQL live in test_signup_postgres.py.
"""
from datetime import date, datetime, timedelta, timezone
import os
import re
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.auth import UNUSABLE_PASSWORD, hash_password, token_digest
    from app.database import Base, get_db
    from app.models import (AuthSession, User, UserPledgeAcknowledgement, UserRole, VolunteerOnboardingBasic,
                            VolunteerOnboardingExperience, VolunteerOnboardingRoutine, VolunteerRoutineRegion,
                            VolunteerRoutineTimeSlot)
    from app.onboarding_common import get_clock
    from app.region_models import LEVEL_SIDO, LEVEL_SIGUNGU, Region
    from app.routers import (age_review, auth, onboarding_basic, onboarding_experience, onboarding_routine,
                             users)
    from app.sms_provider import MockSmsProvider, get_sms_provider
    from app.phone_verification_service import SmsDispatcher, get_sms_dispatcher

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from privacy_test_support import onboarding_agreements, use_test_privacy_policy

PLEDGE_VERSION = "pledge-test-v1"  # test-only values
TEST_ENV = {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION,
            "PHONE_VERIFICATION_HMAC_KEY": "test-only-hmac-key-0123456789abcdef"}
START = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)
PASSWORD = "WalkTogether2026!"
ME = "/users/me"
BASIC_FIELDS = {"id", "login_id", "nickname", "role", "shelter_id"}
PROGRESS_FIELDS = {"account_status", "step1_completed", "step2_completed", "step3_completed", "pledge_completed",
                   "current_pledge_version", "pledge_acknowledged_at", "onboarding_completed",
                   "next_onboarding_step"}
STEP2 = {"has_experience": False}
STEP3 = {"region_codes": ["11110"], "available_time_slot_codes": ["WEEKEND_MORNING"],
         "walking_duration_code": "UP_TO_30_MIN", "comfortable_dog_size_code": "SMALL",
         "agreements": onboarding_agreements()}
USER, PLEDGED, SHELTER, ADMIN = 1, 2, 4, 5


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class UsersMeOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(PASSWORD)

    def setUp(self):
        use_test_privacy_policy(self)
        env = patch.dict(os.environ, TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.clock = FakeClock(START)
        self.sms = MockSmsProvider()
        app = FastAPI()
        for router in (auth.router, users.router, onboarding_basic.router, onboarding_experience.router,
                       onboarding_routine.router, age_review.router):
            app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_clock] = lambda: self.clock
        app.dependency_overrides[get_sms_provider] = lambda: self.sms
        self.dispatcher = SmsDispatcher(inline=True)  # delivery runs inside the request (#110)
        app.dependency_overrides[get_sms_dispatcher] = lambda: self.dispatcher
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            app.dependency_overrides[limiter] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.engine.dispose)
        with self.Session.begin() as db:
            db.add(Region(region_code="11", region_name="서울특별시", region_level=LEVEL_SIDO, is_active=True))
            db.flush()
            db.add(Region(region_code="11110", region_name="종로구", region_level=LEVEL_SIGUNGU,
                          parent_region_code="11", is_active=True))
            for user_id, role in ((USER, UserRole.USER), (PLEDGED, UserRole.USER), (SHELTER, UserRole.SHELTER),
                                  (ADMIN, UserRole.ADMIN)):
                db.add(User(id=user_id, login_id=f"user{user_id:02d}", nickname=f"사용자{user_id}",
                            password=self.password_hash, role=role, phone="010-0000-0000"))
            db.flush()
            for user_id in (USER, PLEDGED, SHELTER, ADMIN):
                db.add(AuthSession(token_hash=token_digest(f"token-{user_id}"), user_id=user_id,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            db.add(UserPledgeAcknowledgement(user_id=PLEDGED, pledge_version=PLEDGE_VERSION, acknowledged_at=START))

    def call(self, method, path, user_id=USER, **kwargs):
        headers = {"Authorization": f"Bearer token-{user_id}"} if user_id else {}
        return self.client.request(method, path, headers=headers, **kwargs)

    def me(self, user_id=USER):
        response = self.call("GET", ME, user_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(set(response.json()["data"]), BASIC_FIELDS | PROGRESS_FIELDS)
        return response.json()["data"]

    def progress(self, user_id=USER):
        return {key: value for key, value in self.me(user_id).items() if key in PROGRESS_FIELDS}

    def expected(self, **values):
        base = {"account_status": "ACTIVE", "step1_completed": False, "step2_completed": False,
                "step3_completed": False, "pledge_completed": False, "current_pledge_version": PLEDGE_VERSION,
                "pledge_acknowledged_at": None, "onboarding_completed": False, "next_onboarding_step": "PLEDGE"}
        return {**base, **values}

    def add_records(self, user_id, *, step1=False, step2=False, step3=False):
        """Rows stored directly, including combinations the APIs would never produce."""
        with self.Session.begin() as db:
            if step1:
                db.add(VolunteerOnboardingBasic(user_id=user_id, full_name="김민지", birth_date=date(2002, 12, 10),
                                                phone_e164=f"+8210111{user_id:05d}", phone_verified_at=START,
                                                completed_at=START, updated_at=START))
            if step2:
                db.add(VolunteerOnboardingExperience(user_id=user_id, has_experience=False, completed_at=START,
                                                     updated_at=START))
            if step3:
                db.add(VolunteerOnboardingRoutine(user_id=user_id, walking_duration_code="UP_TO_30_MIN",
                                                  comfortable_dog_size_code="SMALL", completed_at=START,
                                                  updated_at=START))
                db.flush()
                db.add(VolunteerRoutineRegion(user_id=user_id, region_code="11110"))
                db.add(VolunteerRoutineTimeSlot(user_id=user_id, time_slot_code="WEEKDAY_MORNING"))

    def save_step1(self, user_id=USER):
        sent = self.call("POST", "/users/me/phone-verifications", user_id, json={"phone_number": "010-1234-5678"})
        self.assertEqual(sent.status_code, 200, sent.text)
        verification_id = sent.json()["data"]["verification_id"]
        code = re.search(r"\d{6}", self.sms.outbox[-1]["text"]).group()
        confirmed = self.call("POST", "/users/me/phone-verifications/confirm", user_id,
                              json={"verification_id": verification_id, "code": code})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        saved = self.call("PUT", "/users/me/onboarding/basic-info", user_id,
                          json={"full_name": "김민지", "birth_date": "2002-12-10",
                                "phone_verification_id": verification_id})
        self.assertEqual(saved.status_code, 200, saved.text)
        return saved.json()["data"]

    # The normal flow, step by step

    def test_each_step_moves_to_the_next_and_matches_the_step_api_answers(self):
        self.assertEqual(self.progress(), self.expected())
        pledged = self.call("PUT", "/users/me/pledge", json={"pledge_version": PLEDGE_VERSION})
        self.assertEqual(pledged.status_code, 200, pledged.text)
        acknowledged_at = pledged.json()["data"]["acknowledged_at"]
        self.assertEqual(self.progress(), self.expected(pledge_completed=True, pledge_acknowledged_at=acknowledged_at,
                                                        next_onboarding_step="BASIC_INFORMATION"))
        step1 = self.save_step1()
        self.assertEqual(step1["next_step"], "PET_EXPERIENCE")
        self.assertEqual(self.me()["next_onboarding_step"], step1["next_step"])
        step2 = self.call("PUT", "/users/me/onboarding/dog-experience", json=STEP2).json()["data"]
        self.assertEqual(step2["next_onboarding_step"], "WALKING_ROUTINE")
        self.assertEqual(self.me()["next_onboarding_step"], step2["next_onboarding_step"])
        # Issue #44 test moved here: the Step 3 save answers null and the account is complete.
        step3 = self.call("PUT", "/users/me/onboarding/walking-routine", json=STEP3).json()["data"]
        self.assertIsNone(step3["next_onboarding_step"])
        self.assertEqual(self.progress(), self.expected(
            step1_completed=True, step2_completed=True, step3_completed=True, pledge_completed=True,
            pledge_acknowledged_at=acknowledged_at, onboarding_completed=True, next_onboarding_step=None))
        # The earlier step APIs now give the same shared answer instead of a fixed next step.
        for path, field in (("/users/me/onboarding/basic-info", "next_step"),
                            ("/users/me/onboarding/dog-experience", "next_onboarding_step"),
                            ("/users/me/onboarding/walking-routine", "next_onboarding_step")):
            with self.subTest(path=path):
                self.assertIsNone(self.call("GET", path).json()["data"][field])
        replayed = self.call("PUT", "/users/me/onboarding/dog-experience", json=STEP2).json()["data"]
        self.assertIsNone(replayed["next_onboarding_step"])

    def test_only_a_pledge_or_records_out_of_order_are_not_complete(self):
        self.assertEqual(self.progress(PLEDGED), self.expected(
            pledge_completed=True, pledge_acknowledged_at=START.isoformat(), next_onboarding_step="BASIC_INFORMATION"))
        # Later steps without Step 1 (never produced by the APIs) still report Step 1 first.
        self.add_records(PLEDGED, step2=True, step3=True)
        self.assertEqual(self.progress(PLEDGED), self.expected(
            step2_completed=True, step3_completed=True, pledge_completed=True,
            pledge_acknowledged_at=START.isoformat(), next_onboarding_step="BASIC_INFORMATION"))
        # Every step stored but no pledge record: the pledge comes first.
        self.add_records(USER, step1=True, step2=True, step3=True)
        self.assertEqual(self.progress(USER), self.expected(step1_completed=True, step2_completed=True,
                                                            step3_completed=True))

    def test_a_new_pledge_version_asks_for_the_pledge_again_and_keeps_history(self):
        self.add_records(PLEDGED, step1=True, step2=True, step3=True)
        self.assertTrue(self.me(PLEDGED)["onboarding_completed"])
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "pledge-test-v2"}):
            self.assertEqual(self.progress(PLEDGED), self.expected(
                step1_completed=True, step2_completed=True, step3_completed=True,
                current_pledge_version="pledge-test-v2"))
        with self.Session() as db:
            self.assertEqual(db.query(UserPledgeAcknowledgement).filter_by(user_id=PLEDGED).count(), 1)
        self.assertTrue(self.me(PLEDGED)["onboarding_completed"])  # the kept record counts again for v1

    def test_age_review_comes_first_and_a_correction_returns_to_the_first_step(self):
        self.add_records(PLEDGED, step1=True, step2=True, step3=True)
        with self.Session.begin() as db:
            for user_id in (PLEDGED, USER):
                db.get(User, user_id).account_status = "AGE_REVIEW_REQUIRED"
                db.get(User, user_id).age_review_required_at = START
        self.assertEqual(self.progress(PLEDGED), self.expected(
            account_status="AGE_REVIEW_REQUIRED", step1_completed=True, step2_completed=True, step3_completed=True,
            pledge_completed=True, pledge_acknowledged_at=START.isoformat(), next_onboarding_step="AGE_REVIEW"))
        # A corrected account without Step 1 is sent back to its first incomplete step.
        with self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=USER, pledge_version=PLEDGE_VERSION, acknowledged_at=START))
        corrected = self.call("PUT", "/users/me/age-review/birth-date", json={"birth_date": "2006-05-17"})
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertEqual(corrected.json()["data"]["next_onboarding_step"], "BASIC_INFORMATION")
        self.assertEqual(self.progress(), self.expected(pledge_completed=True, pledge_acknowledged_at=START.isoformat(),
                                                        next_onboarding_step="BASIC_INFORMATION"))

    def test_shelter_and_admin_get_null_onboarding_fields_and_keep_the_pledge_fields(self):
        with self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=ADMIN, pledge_version=PLEDGE_VERSION, acknowledged_at=START))
        nulls = {"step1_completed": None, "step2_completed": None, "step3_completed": None,
                 "onboarding_completed": None, "next_onboarding_step": None}
        self.assertEqual(self.progress(SHELTER), self.expected(**nulls))
        self.assertEqual(self.progress(ADMIN), self.expected(**nulls, pledge_completed=True,
                                                             pledge_acknowledged_at=START.isoformat()))
        self.assertEqual((self.me(SHELTER)["role"], self.me(ADMIN)["role"]), ("shelter", "admin"))

    # Reading rules

    def test_reading_never_writes_and_uses_three_statements_in_every_state(self):
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        self.add_records(PLEDGED, step1=True, step2=True, step3=True)
        event.listen(self.engine, "before_cursor_execute", record)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record)
        for user_id in (USER, PLEDGED, SHELTER):
            with self.subTest(user=user_id):
                statements.clear()
                self.me(user_id)
                # Session lookup, account row, then one progress snapshot.
                self.assertEqual(statements, ["SELECT"] * 3)

    def test_no_sensitive_values_are_returned(self):
        self.add_records(PLEDGED, step1=True)
        body = self.call("GET", ME, PLEDGED).text
        for secret in (PASSWORD, self.password_hash, "token-2", "+82101110000", "2002-12-10", "010-0000-0000",
                       "김민지", "email", "password", "birth_date", "phone"):
            self.assertNotIn(secret, body)

    def test_a_missing_pledge_version_fails_closed_without_details(self):
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": ""}), self.assertLogs("app.api_contract", "ERROR") as logs:
            for user_id in (USER, SHELTER):
                response = self.call("GET", ME, user_id)
                self.assertEqual(response.status_code, 500, response.text)
                self.assertEqual(response.json(), {"code": "INTERNAL_SERVER_ERROR", "data": {},
                                                   "message": response.json()["message"]})
        self.assertTrue(all("PledgeVersionNotConfigured" in line for line in logs.output))
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": ""}):
            self.assertEqual(self.call("GET", ME, None).json()["code"], "AUTHENTICATION_REQUIRED")  # 401 first

    def test_401_codes_keep_no_store_and_an_exited_account_gets_401(self):
        missing = self.call("GET", ME, None)
        self.assertEqual((missing.status_code, missing.json()["code"]), (401, "AUTHENTICATION_REQUIRED"))
        self.assertEqual(missing.headers["cache-control"], "no-store")
        with self.Session.begin() as db:
            db.add(AuthSession(token_hash=token_digest("orphan"), user_id=999,
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            db.get(AuthSession, token_digest("token-4")).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        for headers, code in (({"Authorization": "Bearer orphan"}, "USER_NOT_FOUND"),
                              ({"Authorization": "Bearer token-4"}, "SESSION_EXPIRED")):
            response = self.client.get(ME, headers=headers)
            self.assertEqual((response.status_code, response.json()["code"]), (401, code))
            self.assertEqual(response.headers["cache-control"], "no-store")
        # A real sign-up exit of an age review account: its tokens get 401 afterwards.
        with self.Session.begin() as db:
            db.get(User, USER).account_status = "AGE_REVIEW_REQUIRED"
            db.get(User, USER).age_review_required_at = START
        self.assertEqual(self.me()["next_onboarding_step"], "AGE_REVIEW")
        exited = self.call("DELETE", ME, json={"password": PASSWORD})
        self.assertEqual(exited.status_code, 200, exited.text)
        after = self.call("GET", ME)
        self.assertEqual((after.status_code, after.json()["code"]), (401, "INVALID_ACCESS_TOKEN"))
        self.assertEqual(after.headers["cache-control"], "no-store")

    def test_a_birth_date_over_120_years_keeps_the_account_at_step1(self):
        sent = self.call("POST", "/users/me/phone-verifications", PLEDGED, json={"phone_number": "010-1234-5678"})
        verification_id = sent.json()["data"]["verification_id"]
        code = re.search(r"\d{6}", self.sms.outbox[-1]["text"]).group()
        self.call("POST", "/users/me/phone-verifications/confirm", PLEDGED,
                  json={"verification_id": verification_id, "code": code})
        refused = self.call("PUT", "/users/me/onboarding/basic-info", PLEDGED,
                            json={"full_name": "김민지", "birth_date": "1900-01-01",
                                  "phone_verification_id": verification_id})
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "AGE_ABOVE_MAXIMUM"))
        self.assertEqual(self.progress(PLEDGED), self.expected(
            pledge_completed=True, pledge_acknowledged_at=START.isoformat(), next_onboarding_step="BASIC_INFORMATION"))

    def test_logging_in_again_restores_the_same_server_state(self):
        self.add_records(PLEDGED, step1=True)
        before = self.progress(PLEDGED)
        for _ in range(2):
            login = self.client.post("/auth/login", json={"identifier": "user02", "password": PASSWORD})
            self.assertEqual(login.status_code, 200, login.text)
            headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            data = self.client.get(ME, headers=headers).json()["data"]
            self.assertEqual({key: data[key] for key in PROGRESS_FIELDS}, before)
            self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 200)


if __name__ == "__main__":
    unittest.main()
