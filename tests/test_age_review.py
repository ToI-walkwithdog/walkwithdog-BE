"""Issue #41 on isolated SQLite: birth date correction and sign-up exit of AGE_REVIEW_REQUIRED
accounts. Injected clocks; no real waiting. Foreign keys are enforced like PostgreSQL."""
from datetime import date, datetime, timedelta, timezone
import io
import logging
import os
import unittest
from unittest.mock import patch
import uuid

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import age_review_service
    from app.auth import UNUSABLE_PASSWORD, hash_password, token_digest
    from app.database import Base, get_db
    from app.models import (AuthSession, ChatSession, Dog, MatchingResult, PhoneVerification,
                            PhoneVerificationDailyCount, SessionToken, Shelter, Survey, User, UserConsent,
                            UserPledgeAcknowledgement, UserRole, VolunteerExperienceChallenge, VolunteerOnboardingBasic,
                            VolunteerOnboardingExperience, VolunteerOnboardingRoutine, VolunteerRoutineRegion,
                            VolunteerRoutineTimeSlot, VolunteerWalkPreference)
    from app.onboarding_common import get_clock
    from app.region_models import LEVEL_SIDO, LEVEL_SIGUNGU, Region
    from app.rate_limit import FailureLimiter
    from app.routers import age_review, auth, onboarding_basic, users
    from app.sms_provider import MockSmsProvider, get_sms_provider
    from app.walk_models import VolunteerProfile, Walk, WalkIncident, WalkRequest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

PASSWORD = "CorrectCurrentPassword1!"
PLEDGE_VERSION = "pledge-test-v1"  # test-only value
START = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)  # 2026-11-23 10:00 in Asia/Seoul
BASE_ENV = {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION,
            "PHONE_VERIFICATION_HMAC_KEY": "test-only-hmac-key-0123456789abcdef"}
# Record deletion is only reachable in local, development and test environments.
DELETE_RECORDS = {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "test"}
CORRECT, EXIT = "/users/me/age-review/birth-date", "/users/me"


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class MonotonicClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class AgeReviewTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(PASSWORD)

    def setUp(self):
        env = patch.dict(os.environ, BASE_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("ACCOUNT_EXIT_RECORD_POLICY", "APP_ENV"):
            os.environ.pop(name, None)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.clock = FakeClock(START)
        self.monotonic = MonotonicClock()
        limiter = patch.object(age_review, "PASSWORD_FAILURES", FailureLimiter(5, 900, self.monotonic))
        limiter.start()
        self.addCleanup(limiter.stop)
        app = FastAPI()
        for router in (auth.router, users.router, onboarding_basic.router, age_review.router):
            app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_clock] = lambda: self.clock
        app.dependency_overrides[get_sms_provider] = lambda: MockSmsProvider()
        for limiter_dependency in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            app.dependency_overrides[limiter_dependency] = lambda: None
        self.client = TestClient(app)
        with self.Session.begin() as db:
            db.add_all([
                User(id=1, login_id="minor01", nickname="연령확인", password=self.password_hash,
                     account_status="AGE_REVIEW_REQUIRED", age_review_required_at=START - timedelta(hours=1)),
                User(id=2, login_id="walker02", nickname="활성", password=self.password_hash),
                User(id=3, login_id="shelter01", nickname="보호소", password=self.password_hash, role=UserRole.SHELTER),
                User(id=4, login_id="admin01", nickname="관리자", password=self.password_hash, role=UserRole.ADMIN),
            ])
            db.flush()
            for user_id in (1, 2, 3, 4):
                db.add(AuthSession(token_hash=token_digest(f"token-{user_id}"), user_id=user_id,
                                   expires_at=START + timedelta(days=30)))
                db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION,
                                                 acknowledged_at=START - timedelta(days=1)))
                db.add(UserConsent(user_id=user_id, consent_type="PRIVACY_COLLECTION_USE", document_version="privacy-test-v1",
                                   action="AGREE", recorded_at=START - timedelta(days=1)))

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def call(self, method, path, user_id=1, token=None, **kwargs):
        token = token or (f"token-{user_id}" if user_id else None)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.request(method, path, headers=headers, **kwargs)

    def correct(self, birth_date="2006-05-17", user_id=1):
        return self.call("PUT", CORRECT, user_id, json={"birth_date": birth_date})

    def exit(self, password=PASSWORD, user_id=1, token=None):
        return self.call("DELETE", EXIT, user_id, token=token, json={"password": password})

    def assert_error(self, response, status, code, data=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["code"], code, response.text)
        if data is not None:
            self.assertEqual(response.json()["data"], data)

    def user(self, user_id=1):
        with self.Session() as db:
            return db.get(User, user_id)

    def count(self, model, **filters):
        with self.Session() as db:
            return db.query(model).filter_by(**filters).count()


class CorrectionTests(AgeReviewTestCase):
    def test_correction_lifts_the_restriction_without_storing_anything(self):
        response = self.correct()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"code": "OK", "message": "연령 확인 정보를 정정했습니다.", "data": {
            "account_status": "ACTIVE", "age_review_required": False, "step1_completed": False,
            "next_onboarding_step": "BASIC_INFORMATION"}})
        self.assertNotIn("2006", response.text)
        user = self.user()
        self.assertEqual((user.account_status, user.age_review_required_at), ("ACTIVE", None))
        self.assertEqual((self.count(VolunteerOnboardingBasic), self.count(PhoneVerification)), (0, 0))
        self.assertEqual(self.count(UserPledgeAcknowledgement, user_id=1), 1)  # pledge kept
        self.assertEqual(self.count(UserConsent, user_id=1), 1)
        # The new state is visible to other APIs at once: Step 1 is reachable, the pledge too.
        self.assertEqual(self.call("GET", "/users/me/onboarding/basic-info").status_code, 200)
        self.assertEqual(self.call("PUT", "/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}).status_code, 200)
        self.assertEqual(self.call("GET", "/users/me").json()["data"]["id"], 1)

    def test_changed_pledge_version_asks_for_the_pledge_first(self):
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "pledge-test-v2"}):
            data = self.correct().json()["data"]
        self.assertEqual(data["next_onboarding_step"], "PLEDGE")

    def test_age_boundaries_keep_the_restriction_when_out_of_range(self):
        cases = [("2012-11-24", 422, "AGE_BELOW_MINIMUM", {"field": "birth_date", "minimum_age": 14}),
                 ("1905-11-23", 422, "AGE_ABOVE_MAXIMUM", {"field": "birth_date", "maximum_age": 120})]
        for birth_date, status, code, data in cases:
            with self.subTest(birth_date=birth_date):
                self.assert_error(self.correct(birth_date), status, code, data)
                self.assertEqual(self.user().account_status, "AGE_REVIEW_REQUIRED")
                self.assertIsNotNone(self.user().age_review_required_at)
        self.assertEqual(self.correct("2012-11-23").status_code, 200)  # 14 today
        with self.Session.begin() as db:
            db.get(User, 1).account_status = "AGE_REVIEW_REQUIRED"
        self.assertEqual(self.correct("1905-11-24").status_code, 200)  # still 120

    def test_29_february_birthday_in_a_common_year(self):
        self.clock.now = datetime(2022, 2, 28, 1, 0, tzinfo=timezone.utc)  # 2022-02-28 KST
        self.assert_error(self.correct("2008-02-29"), 422, "AGE_BELOW_MINIMUM")
        self.clock.now = datetime(2022, 2, 28, 15, 0, tzinfo=timezone.utc)  # 2022-03-01 00:00 KST
        self.assertEqual(self.correct("2008-02-29").status_code, 200)

    def test_invalid_dates_keep_the_restriction(self):
        log = io.StringIO()
        handler = logging.StreamHandler(log)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        cases = [(None, "BIRTH_DATE_REQUIRED"), ("", "BIRTH_DATE_REQUIRED"), ("2006.05.17", "INVALID_BIRTH_DATE"),
                 ("2006-02-30", "INVALID_BIRTH_DATE"), ("2026-11-24", "INVALID_BIRTH_DATE"), (20060517, "BIRTH_DATE_REQUIRED")]
        for value, code in cases:
            with self.subTest(value=value):
                body = {} if value is None else {"birth_date": value}
                self.assert_error(self.call("PUT", CORRECT, json=body), 422, code, {"field": "birth_date"})
        self.assertEqual(self.user().account_status, "AGE_REVIEW_REQUIRED")
        self.assertNotIn("2006", log.getvalue())

    def test_access_rules(self):
        self.assert_error(self.correct(user_id=2), 409, "AGE_REVIEW_NOT_REQUIRED", {})
        self.assert_error(self.call("PUT", CORRECT, 2, json={}), 409, "AGE_REVIEW_NOT_REQUIRED")  # before the body
        for user_id in (3, 4):
            self.assert_error(self.correct(user_id=user_id), 403, "AGE_REVIEW_NOT_ALLOWED", {})
        self.assert_error(self.correct(user_id=None), 401, "AUTHENTICATION_REQUIRED")

    def test_repeat_after_success_is_409_for_now(self):
        # Provisional: Notion recommends a repeat 200 but its error table says 409 (Issue #41 open item).
        self.assertEqual(self.correct().status_code, 200)
        self.assert_error(self.correct(), 409, "AGE_REVIEW_NOT_REQUIRED")

    def test_state_changed_between_the_route_check_and_the_lock(self):
        original = age_review_service.lock_user

        def corrected_meanwhile(db, user_id, **kwargs):
            with self.Session.begin() as other:  # a concurrent correction committed first
                other.get(User, user_id).account_status = "ACTIVE"
            return original(db, user_id, **kwargs)

        with patch("app.age_review_service.lock_user", side_effect=corrected_meanwhile):
            self.assert_error(self.correct(), 409, "AGE_REVIEW_NOT_REQUIRED")

    def test_failed_commit_keeps_the_restriction(self):
        with patch.object(Session, "commit", side_effect=OperationalError("COMMIT", {}, Exception("down"))), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.correct(), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.user().account_status, "AGE_REVIEW_REQUIRED")

    def test_restricted_account_access_list(self):
        allowed = [self.call("GET", "/users/me"), self.call("POST", "/auth/login",
                                                            json={"identifier": "minor01", "password": PASSWORD})]
        self.assertEqual([r.status_code for r in allowed], [200, 200])
        blocked = [self.call("PUT", "/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}),
                   self.call("GET", "/users/me/onboarding/basic-info"),
                   self.call("POST", "/users/me/phone-verifications", json={"phone_number": "010-1234-5678"}),
                   self.call("POST", "/users/me/phone-verifications/confirm",
                             json={"verification_id": "4cbfb8b0-5320-4c19-87c1-091338d1df7b", "code": "123456"}),
                   self.call("PUT", "/users/me/onboarding/basic-info", json={})]
        for response in blocked:
            self.assert_error(response, 403, "AGE_REVIEW_REQUIRED")
        self.assertEqual(self.call("POST", "/auth/logout").status_code, 200)


class ExitTests(AgeReviewTestCase):
    def add_exit_records(self):
        """Onboarding rows of user 1 that an exit removes, plus rows of user 2 that must stay."""
        with self.Session.begin() as db:
            db.add(AuthSession(token_hash=token_digest("token-1-phone"), user_id=1, expires_at=START + timedelta(days=1)))
            db.add(SessionToken(user_id=1, token="legacy", expires_at=START + timedelta(days=1)))
            for user_id in (1, 2):
                db.add(PhoneVerification(
                    id=uuid.uuid4(), user_id=user_id, phone_e164=f"+82100000000{user_id}", code_digest="0" * 64,
                    send_status="SENT", provider_request_key=uuid.uuid4(),
                    expires_at=START + timedelta(minutes=5), created_at=START))
                db.add(PhoneVerificationDailyCount(scope="USER", subject=str(user_id), kst_date=date(2026, 11, 23),
                                                   send_count=1))
            db.add(PhoneVerificationDailyCount(scope="PHONE", subject="h" * 64, kst_date=date(2026, 11, 23), send_count=1))

    def test_exit_deletes_the_account_and_every_session(self):
        self.add_exit_records()
        with patch.dict(os.environ, DELETE_RECORDS):
            response = self.exit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"code": "OK", "data": {"deleted": True}, "message": "가입이 종료되었습니다."})
        self.assertIsNone(self.user(1))
        for model in (AuthSession, SessionToken, PhoneVerification, UserPledgeAcknowledgement, UserConsent):
            with self.subTest(model=model.__tablename__):
                self.assertEqual(self.count(model, user_id=1), 0)
        self.assertEqual(self.count(PhoneVerificationDailyCount, scope="USER", subject="1"), 0)
        self.assertEqual(self.count(PhoneVerificationDailyCount, scope="PHONE"), 1)  # per-number limit is kept
        # Other accounts are untouched.
        self.assertIsNotNone(self.user(2))
        for model in (AuthSession, PhoneVerification, UserPledgeAcknowledgement, UserConsent):
            self.assertEqual(self.count(model, user_id=2), 1)
        # Old tokens (every device) and the login are refused afterwards.
        for token in ("token-1", "token-1-phone"):
            self.assert_error(self.call("GET", "/users/me", token=token), 401, "INVALID_ACCESS_TOKEN")
        self.assert_error(self.exit(), 401, "INVALID_ACCESS_TOKEN")  # a retry after a lost response
        login = self.call("POST", "/auth/login", None, json={"identifier": "minor01", "password": PASSWORD})
        self.assert_error(login, 401, "INVALID_CREDENTIALS")

    def test_unexpected_step1_row_is_removed_in_fk_order(self):
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingBasic(user_id=1, full_name="김민지", birth_date=date(2002, 12, 10),
                                            phone_e164="+821012345678", phone_verified_at=START, completed_at=START,
                                            updated_at=START))
        with patch.dict(os.environ, DELETE_RECORDS):
            self.assertEqual(self.exit().status_code, 200)
        self.assertEqual(self.count(VolunteerOnboardingBasic), 0)

    def test_step2_rows_are_removed_in_fk_order_and_kept_when_the_exit_is_refused(self):
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingExperience(user_id=1, has_experience=True, experienced_breeds_text="푸들",
                                                 dog_size_code="SMALL", walking_frequency="DAILY",
                                                 perceived_difficulty="EASY", completed_at=START, updated_at=START))
            db.flush()
            db.add_all([VolunteerExperienceChallenge(user_id=1, challenge_code="LEASH_PULLING"),
                        VolunteerExperienceChallenge(user_id=1, challenge_code="OTHER_DOG_REACTION")])
        with patch.dict(os.environ, {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "production"}):
            self.assert_error(self.exit(), 409, "ACCOUNT_HAS_RETAINED_ACTIVITY")  # production keeps everything
        self.assertEqual((self.count(VolunteerOnboardingExperience), self.count(VolunteerExperienceChallenge)), (1, 2))
        with patch.dict(os.environ, DELETE_RECORDS):
            self.assertEqual(self.exit().status_code, 200)
        self.assertEqual((self.count(VolunteerOnboardingExperience), self.count(VolunteerExperienceChallenge)), (0, 0))
        self.assertIsNone(self.user(1))

    def step3_rows(self):
        return tuple(self.count(model) for model in (VolunteerOnboardingRoutine, VolunteerRoutineRegion,
                                                     VolunteerRoutineTimeSlot))

    def test_step3_rows_are_removed_in_fk_order_and_kept_when_the_exit_is_refused(self):
        with self.Session.begin() as db:
            db.add(Region(region_code="11", region_name="서울특별시", region_level=LEVEL_SIDO, is_active=True))
            db.flush()
            db.add(Region(region_code="11110", region_name="종로구", region_level=LEVEL_SIGUNGU,
                          parent_region_code="11", is_active=True))
            db.add(VolunteerOnboardingRoutine(user_id=1, walking_duration_code="UP_TO_30_MIN",
                                              comfortable_dog_size_code="SMALL", completed_at=START, updated_at=START))
            db.flush()
            db.add(VolunteerRoutineRegion(user_id=1, region_code="11110"))
            db.add_all([VolunteerRoutineTimeSlot(user_id=1, time_slot_code="WEEKDAY_MORNING"),
                        VolunteerRoutineTimeSlot(user_id=1, time_slot_code="WEEKEND_AFTERNOON")])
        with patch.dict(os.environ, {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "production"}):
            self.assert_error(self.exit(), 409, "ACCOUNT_HAS_RETAINED_ACTIVITY")  # production keeps everything
        self.assertEqual(self.step3_rows(), (1, 1, 2))
        with patch.dict(os.environ, DELETE_RECORDS):
            self.assertEqual(self.exit().status_code, 200)
        self.assertEqual(self.step3_rows(), (0, 0, 0))
        self.assertIsNone(self.user(1))
        self.assertEqual(self.count(Region), 2)  # the shared region master stays

    def add_walk_preferences(self):
        """Issue #105: saved on my page before Step 1 turned the account into AGE_REVIEW_REQUIRED."""
        with self.Session.begin() as db:
            db.add(VolunteerWalkPreference(user_id=1, duration_code="OVER_60_MIN", size_code="LARGE", version=2,
                                           updated_at=START - timedelta(hours=2)))

    def test_walk_preferences_are_removed_before_the_user_and_kept_when_the_exit_is_refused(self):
        self.add_walk_preferences()
        with patch.dict(os.environ, {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "production"}):
            self.assert_error(self.exit(), 409, "ACCOUNT_HAS_RETAINED_ACTIVITY")  # policy: production keeps everything
        with self.Session.begin() as db:
            survey = Survey(user_id=1)
            db.add(survey)
        with patch.dict(os.environ, DELETE_RECORDS):
            self.assert_error(self.exit(), 409, "ACCOUNT_HAS_RETAINED_ACTIVITY")  # an activity record
        self.assertEqual((self.count(VolunteerWalkPreference), self.count(User, id=1)), (1, 1))
        with self.Session.begin() as db:
            db.query(Survey).filter_by(user_id=1).delete()
        with patch.dict(os.environ, DELETE_RECORDS):
            self.assertEqual(self.exit().status_code, 200)  # the users FK no longer blocks it (foreign_keys=ON)
        self.assertEqual(self.count(VolunteerWalkPreference), 0)
        self.assertIsNone(self.user(1))

    def test_a_failed_exit_rolls_back_and_keeps_the_walk_preferences(self):
        self.add_walk_preferences()
        with patch.dict(os.environ, DELETE_RECORDS), \
                patch.object(Session, "commit", side_effect=OperationalError("COMMIT", {}, Exception("down"))), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.exit(), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual((self.count(User, id=1), self.count(VolunteerWalkPreference, user_id=1),
                          self.count(AuthSession, user_id=1)), (1, 1, 1))

    def test_consent_and_pledge_records_are_kept_until_the_retention_policy_is_decided(self):
        settings = [{}, {"ACCOUNT_EXIT_RECORD_POLICY": "delete"},
                    {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "production"},
                    {"ACCOUNT_EXIT_RECORD_POLICY": "keep", "APP_ENV": "test"}]
        for env in settings:
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.assert_error(self.exit(), 409, "ACCOUNT_HAS_RETAINED_ACTIVITY", {})
        self.assertEqual((self.count(User, id=1), self.count(UserConsent, user_id=1),
                          self.count(UserPledgeAcknowledgement, user_id=1), self.count(AuthSession, user_id=1)),
                         (1, 1, 1, 1))

    def test_account_without_policy_pending_records_can_exit_in_any_environment(self):
        with self.Session.begin() as db:
            db.query(UserConsent).filter_by(user_id=1).delete()
            db.query(UserPledgeAcknowledgement).filter_by(user_id=1).delete()
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            self.assertEqual(self.exit().status_code, 200)
        self.assertIsNone(self.user(1))

    def test_activity_records_block_the_exit_and_stay(self):
        with self.Session.begin() as db:
            db.add(Shelter(id=1, name="보호소"))
            db.flush()
            db.add(Dog(id=1, name="강아지", shelter_id=1))
            db.flush()
        start = START + timedelta(days=1)

        def walk_request(user_id, **extra):
            return WalkRequest(user_id=user_id, dog_id=1, shelter_id=1, requested_start_at=start,
                               requested_end_at=start + timedelta(hours=1), **extra)

        def survey_and_chat(db):
            survey = Survey(user_id=1)
            db.add(survey)
            db.flush()
            return ChatSession(user_id=1, dog_id=1, survey_id=survey.id, context_snapshot={}, consent_to_external_ai=False)

        def incident(db):
            request = walk_request(2)
            db.add(request)
            db.flush()
            walk = Walk(walk_request_id=request.id, started_at=start, status="IN_PROGRESS")
            db.add(walk)
            db.flush()
            return WalkIncident(walk_id=walk.id, reported_by=1, incident_type="OTHER", description="기록",
                                occurred_at=start)

        builders = {"walk_requests.user_id": lambda db: walk_request(1),
                    "walk_requests.reviewed_by": lambda db: walk_request(2, reviewed_by=1, reviewed_at=start, status="APPROVED"),
                    "walk_incidents.reported_by": incident,
                    "surveys": lambda db: Survey(user_id=1),
                    "chat_sessions": survey_and_chat,
                    "matching_results": lambda db: MatchingResult(user_id=1, dog_id=1, score=50),
                    "volunteer_profiles": lambda db: VolunteerProfile(user_id=1)}
        for name, build in builders.items():
            with self.subTest(table=name):
                with self.Session.begin() as db:
                    db.add(build(db))
                with patch.dict(os.environ, DELETE_RECORDS):
                    self.assert_error(self.exit(), 409, "ACCOUNT_HAS_RETAINED_ACTIVITY", {})
                self.assertIsNotNone(self.user(1))
                self.assertEqual(self.count(AuthSession, user_id=1), 1)
                with self.Session.begin() as db:  # clean up for the next case, children first
                    for model in (WalkIncident, Walk, WalkRequest, ChatSession, MatchingResult, Survey, VolunteerProfile):
                        db.query(model).delete()

    def test_password_rules_and_repeated_failures(self):
        self.assert_error(self.call("DELETE", EXIT, json={}), 422, "PASSWORD_REQUIRED", {"field": "password"})
        self.assert_error(self.exit("   "), 422, "PASSWORD_REQUIRED")
        for _ in range(5):
            self.assert_error(self.exit("wrong-password"), 403, "PASSWORD_CONFIRMATION_FAILED", {"field": "password"})
        blocked = self.exit()  # even the right password while blocked
        self.assert_error(blocked, 429, "TOO_MANY_PASSWORD_ATTEMPTS", {})
        self.assertEqual(blocked.headers["Retry-After"], "900")
        self.assertIsNotNone(self.user(1))
        self.monotonic.value += 900
        with patch.dict(os.environ, DELETE_RECORDS):
            self.assertEqual(self.exit().status_code, 200)

    def test_account_without_a_usable_password_cannot_exit(self):
        with self.Session.begin() as db:
            db.get(User, 1).password = UNUSABLE_PASSWORD
        self.assert_error(self.exit(), 403, "PASSWORD_CONFIRMATION_FAILED")
        self.assertIsNotNone(self.user(1))

    def test_access_rules(self):
        response = self.exit(user_id=2)
        self.assert_error(response, 409, "AGE_REVIEW_NOT_REQUIRED", {})
        self.assertEqual(response.json()["message"], "현재 가입 종료 절차의 대상이 아닙니다.")
        for user_id in (3, 4):
            self.assert_error(self.exit(user_id=user_id), 403, "ACCOUNT_DELETION_NOT_ALLOWED", {})
        self.assert_error(self.exit(user_id=None), 401, "AUTHENTICATION_REQUIRED")
        self.assertEqual(self.count(User), 4)

    def test_database_error_and_fk_error_roll_everything_back(self):
        self.add_exit_records()
        with patch.dict(os.environ, DELETE_RECORDS), \
                patch.object(Session, "commit", side_effect=OperationalError("COMMIT", {}, Exception("down"))), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.exit(), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual((self.count(User, id=1), self.count(AuthSession, user_id=1), self.count(PhoneVerification, user_id=1),
                          self.count(UserConsent, user_id=1)), (1, 2, 1, 1))
        # A referencing row the checks missed: the users DELETE fails on the FK and nothing is lost.
        with self.Session.begin() as db:
            db.add(Survey(user_id=1))
        with patch.dict(os.environ, DELETE_RECORDS), \
                patch("app.age_review_service.has_retained_activity", return_value=False), \
                self.assertLogs("app.api_contract", "ERROR") as logs:
            self.assert_error(self.exit(), 500, "INTERNAL_SERVER_ERROR")
        self.assertIn("IntegrityError", "".join(logs.output))
        self.assertNotIn(PASSWORD, "".join(logs.output))
        self.assertEqual((self.count(User, id=1), self.count(AuthSession, user_id=1), self.count(UserPledgeAcknowledgement, user_id=1)),
                         (1, 2, 1))
        self.assertEqual(self.call("GET", "/users/me").status_code, 200)  # the session still works

    def test_corrected_account_cannot_exit_and_exited_account_cannot_correct(self):
        self.assertEqual(self.correct().status_code, 200)
        self.assert_error(self.exit(), 409, "AGE_REVIEW_NOT_REQUIRED")
        with self.Session.begin() as db:
            db.get(User, 1).account_status = "AGE_REVIEW_REQUIRED"
        with patch.dict(os.environ, DELETE_RECORDS):
            self.assertEqual(self.exit().status_code, 200)
        self.assert_error(self.correct(), 401, "INVALID_ACCESS_TOKEN")


if __name__ == "__main__":
    unittest.main()
