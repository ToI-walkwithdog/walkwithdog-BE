"""Onboarding integration on real PostgreSQL (Issue #34): sign-up to home through the real API routers.

The main flows run on a schema built only by the checked-in SQL files, in an order that satisfies
every manifest `requires`, without create_all(): the base tables of a dev database before 016
(005, 006, 007, 014, 015), then 002, 016_onboarding_signup, 017_remove_user_email, 018_user_consents,
019, 023, 024, 025, 022, 026, 027, 035, 036 and 037. Account exit reads activity tables (chat, matching) that those files do not create, so
its scenario runs on the model schema instead.

Synthetic accounts and regions only. SMS goes to the in-memory MockSmsProvider; nothing is sent.
SIGNUP_TEST_DATABASE_URL must name an EMPTY local toi_walk_test_* database. Each test works in its
own su_<uuid> schema and drops only that schema afterwards.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from uuid import uuid4

# app.database builds its engine from DATABASE_URL at import time; keep it away from any real DB.
with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.auth import hash_password
    from app.database import Base, get_db
    from app.models import (AuthSession, PhoneVerification, PhoneVerificationDailyCount, Survey, User,
                            UserConsent, UserPledgeAcknowledgement, UserRole, VolunteerExperienceChallenge,
                            VolunteerOnboardingBasic, VolunteerOnboardingExperience, VolunteerOnboardingRoutine,
                            VolunteerRoutineRegion, VolunteerRoutineTimeSlot)
    from app.onboarding_common import get_clock
    from app.region_models import LEVEL_SIDO, LEVEL_SIGUNGU, LEVEL_SPECIAL_SELECTABLE, Region
    from app.routers import (age_review, auth, onboarding_basic, onboarding_experience, onboarding_routine,
                             regions, users)
    from app.sms_provider import MockSmsProvider, get_sms_provider
    from app.phone_verification_service import SmsDispatcher, get_sms_dispatcher

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as OrmSession, sessionmaker

from privacy_test_support import TEST_DOCUMENT_VERSION, TEST_TERMS_VERSION, onboarding_agreements, privacy, use_test_privacy_policy

from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
SCHEMA_NAME = re.compile(r"su_[0-9a-f]{32}")
ROOT = Path(__file__).resolve().parents[1]
PLEDGE_VERSION = "pledge-test-v1"  # test-only values
TEST_ENV = {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION,
            "PHONE_VERIFICATION_HMAC_KEY": "test-only-hmac-key-0123456789abcdef",
            "SMS_MOCK_FIXED_CODE": ""}  # SMS_MOCK_FIXED_CODE: a shell or root .env value must not reach these tests (#128)
START = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)  # 10:00 in Asia/Seoul
PASSWORD = "WalkTogether2026!"
# Base tables of a dev database before 016 come from these files (as in test_signup_postgres).
DEV_CHAIN_BEFORE_016 = ("005_user_roles.sql", "006_user_shelters.sql", "007_walk_domain.sql",
                        "014_f08_live_walk.sql", "015_f09_walk_review.sql")
# The onboarding chain in dependency order; 022 has no prerequisite and must precede 026.
ONBOARDING_CHAIN = ("002_auth_sessions.sql", "016_onboarding_signup.sql", "017_remove_user_email.sql",
                    "018_user_consents.sql", "019_first_walk_guide.sql", "023_user_pledge_acknowledgements.sql",
                    "024_onboarding_basic_info.sql", "025_onboarding_dog_experience.sql", "022_regions.sql",
                    "026_onboarding_walking_routine.sql", "027_onboarding_agreements.sql",
                    "035_profile_phone_change.sql",  # Issue #103: requires 024 only
                    "036_walk_preferences.sql",  # Issue #105: base users only; sign-up exit deletes it
                    "037_phone_verification_suppressed.sql")  # Issue #110: requires 024
REGIONS = (("11", "서울특별시", LEVEL_SIDO, None), ("11110", "종로구", LEVEL_SIGUNGU, "11"),
           ("11680", "강남구", LEVEL_SIGUNGU, "11"), ("26", "부산광역시", LEVEL_SIDO, None),
           ("26350", "해운대구", LEVEL_SIGUNGU, "26"), ("36", "세종특별자치시", LEVEL_SIDO, None),
           ("36_SPECIAL_ALL", "세종특별자치시 전체", LEVEL_SPECIAL_SELECTABLE, "36"))
NO_EXPERIENCE = {"has_experience": False}
FULL_EXPERIENCE = {"has_experience": True, "experienced_breeds_text": "믹스견", "dog_size_code": "MEDIUM",
                   "walking_frequency": "WEEKLY_3_4", "perceived_difficulty": "NORMAL",
                   "challenge_codes": ["LEASH_PULLING", "OTHER_DOG_REACTION"]}
ROUTINE = {"region_codes": ["11110"], "available_time_slot_codes": ["WEEKDAY_MORNING", "WEEKEND_AFTERNOON"],
           "walking_duration_code": "UP_TO_30_MIN", "comfortable_dog_size_code": "SMALL",
           "agreements": onboarding_agreements()}


@unittest.skipUnless(TEST_URL, "SIGNUP_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다")
class OnboardingIntegrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(TEST_URL)
        validate_local_engine(cls.admin)
        if not cls.admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        cls.assert_empty_database()
        cls.created_schemas = set()
        cls.password_hash = hash_password(PASSWORD)

    @classmethod
    def assert_empty_database(cls):
        with cls.admin.connect() as connection:
            inspector = inspect(connection)
            custom = [s for s in inspector.get_schema_names()
                      if s not in ("public", "information_schema") and not s.startswith("pg_")]
            if inspector.get_table_names() or custom:
                raise PreparationError("새로운 빈 테스트 DB만 허용합니다.")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.assert_empty_database()  # every schema this run created is gone
        finally:
            cls.admin.dispose()

    def setUp(self):
        use_test_privacy_policy(self)
        env = patch.dict(os.environ, TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.schema = "su_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.created_schemas.add(self.schema)
        self.addCleanup(self.drop_own_schema, self.schema)
        self.use_session_time_zone(None)
        self.now = START
        self.sms = MockSmsProvider()

    def use_session_time_zone(self, zone):
        """Connect to this test's schema; `zone` sets the PostgreSQL session TimeZone (None: server default)."""
        options = f"-csearch_path={self.schema}" + (f" -ctimezone={zone}" if zone else "")
        self.engine = create_engine(TEST_URL, connect_args={"options": options})
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)  # the app's get_db reads this on every request

    def drop_own_schema(self, schema):
        """Drop only a schema this run created, owned by the test role, in the test DB."""
        if (schema not in self.created_schemas or SCHEMA_NAME.fullmatch(schema) is None
                or not self.admin.url.database.startswith("toi_walk_test_")):
            raise PreparationError(f"이 실행이 만든 테스트 스키마가 아니므로 삭제하지 않습니다: {schema}")
        with self.admin.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            owned = connection.execute(text(
                "SELECT n.nspowner = r.oid FROM pg_namespace n, pg_roles r"
                " WHERE n.nspname = :schema AND r.rolname = current_user"), {"schema": schema}).scalar_one_or_none()
            if owned is not True:
                raise PreparationError(f"현재 테스트 계정 소유 스키마가 아니므로 삭제하지 않습니다: {schema}")
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        self.created_schemas.discard(schema)

    # Schema and application

    def run_sql_file(self, name):
        raw = self.engine.raw_connection()
        driver = raw.driver_connection
        try:
            driver.autocommit = True  # each file manages its own BEGIN/COMMIT, like psql
            with driver.cursor() as cursor:
                try:
                    cursor.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
        finally:
            driver.autocommit = False
            raw.close()

    def migrate(self):
        """The real SQL files only: no create_all(). Checks manifest prerequisites on the way."""
        manifest = json.loads((ROOT / "migrations" / "manifest.json").read_text(encoding="utf-8"))
        requires = {item["file"]: item["requires"] for item in manifest["migrations"]}
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE shelters (id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL)")
            connection.exec_driver_sql(
                "CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL UNIQUE,"
                " nickname VARCHAR(50) NOT NULL, password VARCHAR(255), created_at TIMESTAMPTZ DEFAULT now())")
            connection.exec_driver_sql("CREATE TABLE dogs (id SERIAL PRIMARY KEY, name VARCHAR(100))")
            connection.exec_driver_sql(
                "CREATE TABLE surveys (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id))")
        applied = set()
        for name in DEV_CHAIN_BEFORE_016 + ONBOARDING_CHAIN:
            self.assertLessEqual(set(requires[name]), applied, name)  # every prerequisite already applied
            self.run_sql_file(name)
            applied.add(name)
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            self.assertEqual({c["name"] for c in inspector.get_columns("users")}, set(User.__table__.columns.keys()))
            for model in (UserConsent, UserPledgeAcknowledgement, AuthSession, PhoneVerification,
                          PhoneVerificationDailyCount, VolunteerOnboardingBasic, VolunteerOnboardingExperience,
                          VolunteerExperienceChallenge, VolunteerOnboardingRoutine, VolunteerRoutineRegion,
                          VolunteerRoutineTimeSlot, Region):
                self.assertEqual({c["name"] for c in inspector.get_columns(model.__tablename__)},
                                 set(model.__table__.columns.keys()), model.__tablename__)

    def start(self, *, migrated=True, load_regions=True):
        if migrated:
            self.migrate()
        else:
            Base.metadata.create_all(self.engine)
        app = FastAPI()
        # Same routers and order as app.main for the onboarding and account APIs.
        for router in (auth.router, users.router, onboarding_basic.router, onboarding_experience.router,
                       onboarding_routine.router, age_review.router, regions.router):
            app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_clock] = lambda: (lambda: self.now)
        app.dependency_overrides[get_sms_provider] = lambda: self.sms
        self.dispatcher = SmsDispatcher(inline=True)  # delivery runs inside the request (#110)
        app.dependency_overrides[get_sms_dispatcher] = lambda: self.dispatcher
        # Per-IP limits are verified in test_rate_limit; one flow here signs up several times.
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            app.dependency_overrides[limiter] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        if load_regions:
            self.load_regions()

    def load_regions(self):
        with self.Session.begin() as db:
            for code, name, level, parent in REGIONS:
                db.add(Region(region_code=code, region_name=name, region_level=level, parent_region_code=parent,
                              is_active=True))
                db.flush()

    # API steps

    def call(self, method, path, token=None, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.request(method, path, headers=headers, **kwargs)

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["code"], "OK")
        return response.json()["data"]

    def assert_error(self, response, status, code, data=None):
        self.assertEqual((response.status_code, response.json()["code"]), (status, code), response.text)
        if data is not None:
            self.assertEqual(response.json()["data"], data)

    def sign_up(self, login_id="walker01", nickname="산책봉사"):
        self.assertTrue(self.ok(self.call("GET", "/users/nickname-availability", params={"nickname": nickname}))
                        ["available"])
        self.assertTrue(self.ok(self.call("GET", "/users/id-availability", params={"login_id": login_id}))
                        ["available"])
        return self.ok(self.call("POST", "/users/", json={"login_id": login_id, "nickname": nickname,
                                                          "password": PASSWORD, "privacy": privacy()}))

    def log_in(self, login_id="walker01"):
        data = self.ok(self.call("POST", "/auth/login", json={"identifier": login_id, "password": PASSWORD}))
        return data["access_token"]

    def log_out(self, token):
        self.ok(self.call("POST", "/auth/logout", token))

    def me(self, token):
        response = self.call("GET", "/users/me", token)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return self.ok(response)

    def pledge(self, token, version=PLEDGE_VERSION):
        return self.ok(self.call("PUT", "/users/me/pledge", token, json={"pledge_version": version}))

    def send_code(self, token, phone="010-1234-5678"):
        return self.call("POST", "/users/me/phone-verifications", token, json={"phone_number": phone})

    def last_code(self):
        return re.search(r"\d{6}", self.sms.outbox[-1]["text"]).group()

    def verified(self, token, phone="010-1234-5678"):
        verification_id = self.ok(self.send_code(token, phone))["verification_id"]
        self.ok(self.call("POST", "/users/me/phone-verifications/confirm", token,
                          json={"verification_id": verification_id, "code": self.last_code()}))
        return verification_id

    def save_basic(self, token, verification_id, birth_date="2002-12-10", full_name="김민지"):
        return self.call("PUT", "/users/me/onboarding/basic-info", token,
                         json={"full_name": full_name, "birth_date": birth_date,
                               "phone_verification_id": verification_id})

    def save_experience(self, token, body=NO_EXPERIENCE):
        return self.call("PUT", "/users/me/onboarding/dog-experience", token, json=body)

    def save_routine(self, token, body=ROUTINE):
        return self.call("PUT", "/users/me/onboarding/walking-routine", token, json=body)

    def rows(self, model, **filters):
        with self.Session() as db:
            return db.query(model).filter_by(**filters).count()

    # Full flows

    def complete_flow(self, experience, routine):
        """Sign-up to home in the confirmed order; returns the next steps seen along the way."""
        signed_up = self.sign_up()
        token = self.log_in()
        seen = []

        def next_step(answer_step):
            current = self.me(token)
            self.assertEqual(current["next_onboarding_step"], answer_step)  # step API and /users/me agree
            seen.append(current["next_onboarding_step"])
            return current

        first = self.me(token)
        self.assertEqual({key: first[key] for key in ("step1_completed", "pledge_completed", "onboarding_completed",
                                                      "next_onboarding_step")},
                         {"step1_completed": False, "pledge_completed": False, "onboarding_completed": False,
                          "next_onboarding_step": "PLEDGE"})
        self.assertNotIn("email", first)
        pledged = self.pledge(token)
        next_step("BASIC_INFORMATION")
        basic = self.ok(self.save_basic(token, self.verified(token)))
        next_step(basic["next_step"])
        step2 = self.ok(self.save_experience(token, experience))
        next_step(step2["next_onboarding_step"])
        sido = self.ok(self.call("GET", "/regions/sido", token))["regions"]
        self.assertIn({"region_code": "11", "region_name": "서울특별시"}, sido)
        sigungu = self.ok(self.call("GET", "/regions/sigungu", token, params={"sido_code": "11"}))["regions"]
        self.assertIn("11110", [row["region_code"] for row in sigungu])
        step3 = self.ok(self.save_routine(token, routine))
        done = next_step(step3["next_onboarding_step"])
        self.assertEqual(seen, ["BASIC_INFORMATION", "PET_EXPERIENCE", "WALKING_ROUTINE", None])
        self.assertNotIn("PLEDGE", seen)  # never sent back to the pledge before completion
        self.assertEqual((done["onboarding_completed"], done["pledge_acknowledged_at"]),
                         (True, pledged["acknowledged_at"]))
        # Log out: the old token is refused. Log in again: the same completed state comes back.
        self.log_out(token)
        self.assert_error(self.call("GET", "/users/me", token), 401, "INVALID_ACCESS_TOKEN")
        again = self.log_in()
        self.assertEqual({k: v for k, v in self.me(again).items()}, done)
        return signed_up["id"], again

    def test_full_onboarding_without_dog_experience_on_the_migrated_schema(self):
        self.start()
        user_id, _ = self.complete_flow(NO_EXPERIENCE, ROUTINE)
        self.assertEqual((self.rows(User), self.rows(UserPledgeAcknowledgement, user_id=user_id),
                          self.rows(VolunteerOnboardingBasic), self.rows(VolunteerOnboardingExperience),
                          self.rows(VolunteerExperienceChallenge), self.rows(VolunteerOnboardingRoutine),
                          self.rows(VolunteerRoutineRegion), self.rows(VolunteerRoutineTimeSlot),
                          self.rows(AuthSession)), (1, 1, 1, 1, 0, 1, 1, 2, 1))
        with self.Session() as db:
            consents = db.query(UserConsent).filter_by(user_id=user_id).all()
            self.assertEqual(len(consents), 2)
            self.assertEqual({(row.consent_type, row.document_version, row.action) for row in consents},
                             {("PRIVACY_COLLECTION_USE", TEST_DOCUMENT_VERSION, "AGREE"),
                              ("TERMS_OF_SERVICE", TEST_TERMS_VERSION, "AGREE")})
            (verification,) = db.query(PhoneVerification).all()
            self.assertIsNotNone(verification.consumed_at)
            self.assertFalse(db.get(VolunteerOnboardingExperience, user_id).has_experience)

    def test_full_onboarding_with_dog_experience_on_the_migrated_schema(self):
        self.start()
        routine = {**ROUTINE, "region_codes": ["11680", "26350", "36_SPECIAL_ALL"],
                   "available_time_slot_codes": ["WEEKDAY_MORNING", "WEEKDAY_AFTERNOON", "WEEKEND_MORNING",
                                                 "WEEKEND_AFTERNOON"],
                   "walking_duration_code": "BETWEEN_30_AND_60_MIN", "comfortable_dog_size_code": "LARGE"}
        user_id, token = self.complete_flow(FULL_EXPERIENCE, routine)
        stored = self.ok(self.call("GET", "/users/me/onboarding/dog-experience", token))["experience"]
        self.assertEqual((stored["dog_size_code"], stored["challenge_codes"]),
                         ("MEDIUM", ["LEASH_PULLING", "OTHER_DOG_REACTION"]))
        stored = self.ok(self.call("GET", "/users/me/onboarding/walking-routine", token))["routine"]
        self.assertEqual((stored["region_codes"], len(stored["available_time_slot_codes"]),
                          stored["walking_duration_code"]), (sorted(routine["region_codes"]), 4,
                                                             "BETWEEN_30_AND_60_MIN"))
        self.assertEqual((self.rows(VolunteerExperienceChallenge, user_id=user_id),
                          self.rows(VolunteerRoutineRegion, user_id=user_id)), (2, 3))

    # Recovery and bypass

    def test_logging_in_again_after_each_step_restores_the_next_step(self):
        self.start()
        self.sign_up()
        token = self.log_in()

        def relogin_and_expect(step):
            nonlocal token
            self.log_out(token)  # the app was closed; the next start logs in again
            token = self.log_in()
            self.assertEqual(self.me(token)["next_onboarding_step"], step)

        relogin_and_expect("PLEDGE")
        self.pledge(token)
        relogin_and_expect("BASIC_INFORMATION")
        self.ok(self.save_basic(token, self.verified(token)))
        relogin_and_expect("PET_EXPERIENCE")
        self.ok(self.save_experience(token))
        relogin_and_expect("WALKING_ROUTINE")
        self.ok(self.save_routine(token))
        relogin_and_expect(None)
        self.assertEqual(self.rows(AuthSession), 1)  # every logout revoked its session

    def test_skipping_steps_gets_the_first_incomplete_step_and_stores_nothing(self):
        self.start()
        self.sign_up()
        token = self.log_in()

        def refused(response, step):
            self.assert_error(response, 409, "PREVIOUS_STEP_NOT_COMPLETED", {"required_step": step})
            self.assertEqual(self.me(token)["next_onboarding_step"], step)

        for response in (self.send_code(token), self.call("GET", "/users/me/onboarding/basic-info", token),
                         self.save_experience(token), self.save_routine(token)):
            refused(response, "PLEDGE")
        self.assertEqual((self.sms.outbox, self.rows(PhoneVerification)), ([], 0))  # no SMS before the pledge
        self.pledge(token)
        for response in (self.save_experience(token), self.save_routine(token)):
            refused(response, "BASIC_INFORMATION")
        self.ok(self.save_basic(token, self.verified(token)))
        refused(self.save_routine(token), "PET_EXPERIENCE")
        self.assertEqual((self.rows(VolunteerOnboardingExperience), self.rows(VolunteerOnboardingRoutine)), (0, 0))

    def test_lost_responses_are_retried_and_different_answers_get_409(self):
        self.start()
        self.sign_up()
        token = self.log_in()
        first = self.pledge(token)
        self.now += timedelta(minutes=1)
        self.assertEqual(self.pledge(token)["acknowledged_at"], first["acknowledged_at"])
        verification_id = self.verified(token)
        saved = self.ok(self.save_basic(token, verification_id))
        self.now += timedelta(minutes=1)
        self.assertEqual(self.ok(self.save_basic(token, verification_id))["completed_at"], saved["completed_at"])
        self.assert_error(self.save_basic(token, verification_id, full_name="이서연"), 409,
                          "BASIC_INFO_ALREADY_COMPLETED")
        saved = self.ok(self.save_experience(token, FULL_EXPERIENCE))
        self.assertEqual(self.ok(self.save_experience(token, FULL_EXPERIENCE))["completed_at"], saved["completed_at"])
        self.assert_error(self.save_experience(token, NO_EXPERIENCE), 409, "DOG_EXPERIENCE_ALREADY_COMPLETED")
        saved = self.ok(self.save_routine(token))
        retried = self.ok(self.save_routine(token, {**ROUTINE, "available_time_slot_codes":
                                                    list(reversed(ROUTINE["available_time_slot_codes"]))}))
        self.assertEqual(retried["completed_at"], saved["completed_at"])
        self.assert_error(self.save_routine(token, {**ROUTINE, "walking_duration_code": "OVER_60_MIN"}), 409,
                          "WALKING_ROUTINE_ALREADY_COMPLETED")
        self.assertEqual((self.rows(UserPledgeAcknowledgement), self.rows(VolunteerOnboardingBasic),
                          self.rows(VolunteerOnboardingExperience), self.rows(VolunteerExperienceChallenge),
                          self.rows(VolunteerOnboardingRoutine), self.rows(VolunteerRoutineTimeSlot)),
                         (1, 1, 1, 2, 1, 2))
        self.assertTrue(self.me(token)["onboarding_completed"])

    def test_a_step1_answer_that_fails_after_its_commit_is_recovered_by_the_retry(self):
        """Step 1 works out its next step after the commit (#45). If that fails, the record is kept
        and the client's retry of the same request gets the first result."""
        self.start()
        self.sign_up()
        token = self.log_in()
        self.pledge(token)
        verification_id = self.verified(token)
        calls = []

        def fail_once(*args, **kwargs):
            calls.append("next_step")
            raise RuntimeError("answer lost after the commit")

        with patch("app.basic_info_service.next_step", side_effect=fail_once), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.save_basic(token, verification_id), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(calls, ["next_step"])  # failed once, after the commit; the retry runs unpatched
        state = self.me(token)
        self.assertEqual((state["step1_completed"], state["next_onboarding_step"]), (True, "PET_EXPERIENCE"))
        retried = self.ok(self.save_basic(token, verification_id))
        self.assertEqual(retried["next_step"], "PET_EXPERIENCE")
        with self.Session() as db:
            (basic,) = db.query(VolunteerOnboardingBasic).all()
            (verification,) = db.query(PhoneVerification).all()
            self.assertEqual(retried["completed_at"], basic.completed_at.astimezone(timezone.utc).isoformat())
            self.assertEqual(verification.consumed_at, basic.completed_at)

    def test_a_step1_failure_after_the_insert_rolls_back_on_postgres(self):
        self.start()
        self.sign_up()
        token = self.log_in()
        self.pledge(token)
        verification_id = self.verified(token)
        original, written = OrmSession.commit, {}

        def fail_after_writing(session):
            if not written and any(isinstance(row, VolunteerOnboardingBasic) for row in session.new):
                session.flush()  # the INSERT and the consumed_at UPDATE reach PostgreSQL first
                written["basic_rows"] = session.execute(text(
                    "SELECT count(*) FROM volunteer_onboarding_basics")).scalar_one()
                written["consumed"] = session.execute(text(
                    "SELECT count(*) FROM phone_verifications WHERE consumed_at IS NOT NULL")).scalar_one()
                raise OperationalError("COMMIT", {}, Exception("connection lost at commit"))
            return original(session)

        with patch.object(OrmSession, "commit", fail_after_writing), \
                self.assertLogs("app.api_contract", "ERROR") as logs:
            self.assert_error(self.save_basic(token, verification_id), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(written, {"basic_rows": 1, "consumed": 1})  # both writes happened before the failure
        self.assertIn("OperationalError", logs.output[0])
        with self.Session() as db:
            self.assertEqual(db.query(VolunteerOnboardingBasic).count(), 0)
            self.assertIsNone(db.query(PhoneVerification).one().consumed_at)  # consumption rolled back
        self.assertEqual(self.ok(self.save_basic(token, verification_id))["next_step"], "PET_EXPERIENCE")

    def test_pledge_time_is_the_same_utc_string_whatever_the_session_time_zone(self):
        """TIMESTAMPTZ comes back in the session time zone; both APIs must still answer in UTC (#45)."""
        self.start()
        self.sign_up()
        token = self.log_in()
        first = None
        for zone in ("Asia/Seoul", "UTC"):
            with self.subTest(zone=zone):
                self.use_session_time_zone(zone)
                with self.engine.connect() as connection:
                    self.assertEqual(connection.execute(text("SHOW TimeZone")).scalar_one(), zone)
                pledged = self.pledge(token)  # the first call stores the record, the next replays it
                first = first or pledged["acknowledged_at"]
                self.assertEqual(pledged["acknowledged_at"], first)
                self.assertTrue(first.endswith("+00:00"), first)
                self.assertEqual(self.me(token)["pledge_acknowledged_at"], first)
        with self.Session() as db:
            stored = db.query(UserPledgeAcknowledgement).one().acknowledged_at
        self.assertEqual(stored.astimezone(timezone.utc).isoformat(), first)  # the stored instant is unchanged

    def test_a_new_pledge_version_mid_onboarding_is_asked_before_the_next_step(self):
        self.start()
        self.sign_up()
        token = self.log_in()
        self.pledge(token)
        self.ok(self.save_basic(token, self.verified(token)))
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "pledge-test-v2"}):
            state = self.me(token)
            self.assertEqual((state["pledge_completed"], state["pledge_acknowledged_at"], state["step1_completed"],
                              state["next_onboarding_step"]), (False, None, True, "PLEDGE"))
            for response in (self.save_experience(token), self.call("GET", "/users/me/onboarding/basic-info", token)):
                self.assert_error(response, 409, "PREVIOUS_STEP_NOT_COMPLETED", {"required_step": "PLEDGE"})
            self.pledge(token, "pledge-test-v2")
            self.assertEqual(self.me(token)["next_onboarding_step"], "PET_EXPERIENCE")
            self.ok(self.save_experience(token))
            self.ok(self.save_routine(token))
            self.assertTrue(self.me(token)["onboarding_completed"])
        self.assertEqual(self.rows(UserPledgeAcknowledgement), 2)  # the first version's record is kept

    def test_a_failed_login_after_sign_up_keeps_the_account_and_needs_no_new_sign_up(self):
        self.start()
        self.sign_up()
        consents = self.rows(UserConsent)
        failed = self.call("POST", "/auth/login", json={"identifier": "walker01", "password": "Wrong-Pass-2026!"})
        self.assert_error(failed, 401, "INVALID_CREDENTIALS")
        self.assertEqual(self.rows(AuthSession), 0)
        again = self.call("POST", "/users/", json={"login_id": "walker01", "nickname": "다른닉네임",
                                                   "password": PASSWORD, "privacy": privacy()})
        self.assert_error(again, 409, "LOGIN_ID_ALREADY_EXISTS")
        self.assertEqual((self.rows(User), self.rows(UserConsent)), (1, consents))
        self.assertEqual(self.me(self.log_in())["next_onboarding_step"], "PLEDGE")

    # External services and master data

    def test_an_sms_failure_is_recovered_by_a_new_send(self):
        self.start()
        signed_up = self.sign_up()
        token = self.log_in()
        self.pledge(token)
        self.sms.next_outcome = ["failed"]
        # Issue #110: the send is accepted (200); the provider's refusal stays internal, and
        # confirming the failed request answers like a wrong code.
        self.assertEqual(self.send_code(token).status_code, 200)
        with self.Session() as db:
            failed = db.query(PhoneVerification).one()
            self.assertEqual(failed.send_status, "FAILED")
        confirm = self.call("POST", "/users/me/phone-verifications/confirm", token,
                            json={"verification_id": str(failed.id), "code": "123456"})
        self.assert_error(confirm, 422, "VERIFICATION_CODE_INVALID")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.ok(self.save_basic(token, self.verified(token)))["next_step"], "PET_EXPERIENCE")
        with self.Session() as db:
            count = db.query(PhoneVerificationDailyCount).filter_by(scope="USER", subject=str(signed_up["id"])).one()
            self.assertEqual(count.send_count, 2)  # the failed send counts toward the daily limit

    def test_an_empty_region_master_blocks_the_choice_until_regions_are_loaded(self):
        self.start(load_regions=False)
        self.sign_up()
        token = self.log_in()
        self.pledge(token)
        self.ok(self.save_basic(token, self.verified(token)))
        self.ok(self.save_experience(token))
        self.assert_error(self.call("GET", "/regions/sido", token), 503, "REGION_DATA_UNAVAILABLE")
        self.assert_error(self.save_routine(token), 422, "INVALID_REGION_CODE")
        self.assertEqual(self.rows(VolunteerOnboardingRoutine), 0)
        self.load_regions()
        self.assertIsNone(self.ok(self.save_routine(token))["next_onboarding_step"])

    # Age restriction and account exit

    def test_an_under_14_birth_date_blocks_onboarding_until_the_correction(self):
        self.start()
        self.sign_up()
        token = self.log_in()
        self.pledge(token)
        refused = self.save_basic(token, self.verified(token), birth_date="2015-01-01")
        self.assert_error(refused, 422, "AGE_BELOW_MINIMUM")
        state = self.me(token)
        self.assertEqual((state["account_status"], state["onboarding_completed"], state["next_onboarding_step"]),
                         ("AGE_REVIEW_REQUIRED", False, "AGE_REVIEW"))
        self.assertEqual(self.rows(VolunteerOnboardingBasic), 0)  # neither the profile nor the birth date
        for response in (self.pledge_call(token), self.send_code(token), self.save_experience(token)):
            self.assert_error(response, 403, "AGE_REVIEW_REQUIRED")
        self.assert_error(self.save_routine(token), 403, "ONBOARDING_NOT_ALLOWED")  # Step 3 spec code
        corrected = self.ok(self.call("PUT", "/users/me/age-review/birth-date", token,
                                      json={"birth_date": "2006-05-17"}))
        self.assertEqual(corrected["next_onboarding_step"], "BASIC_INFORMATION")
        state = self.me(token)
        self.assertEqual((state["account_status"], state["pledge_completed"], state["step1_completed"]),
                         ("ACTIVE", True, False))
        with self.Session() as db:
            self.assertIsNone(db.query(User).one().age_review_required_at)
        self.now += timedelta(seconds=61)  # the earlier code request is still recent
        self.ok(self.save_basic(token, self.verified(token), birth_date="2006-05-17"))
        self.ok(self.save_experience(token))
        self.ok(self.save_routine(token))
        self.assertTrue(self.me(token)["onboarding_completed"])

    def pledge_call(self, token):
        return self.call("PUT", "/users/me/pledge", token, json={"pledge_version": PLEDGE_VERSION})

    def test_an_exit_of_an_age_review_account_revokes_every_device(self):
        exit_policy = patch.dict(os.environ, {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "test"})
        exit_policy.start()
        self.addCleanup(exit_policy.stop)
        self.start(migrated=False)  # the exit also reads chat and matching tables
        signed_up = self.sign_up()
        phone, laptop = self.log_in(), self.log_in()
        self.pledge(phone)
        self.assert_error(self.save_basic(phone, self.verified(phone), birth_date="2015-01-01"), 422,
                          "AGE_BELOW_MINIMUM")
        exited = self.call("DELETE", "/users/me", laptop, json={"password": PASSWORD})
        self.assertEqual(self.ok(exited), {"deleted": True})
        for token in (phone, laptop):
            self.assert_error(self.call("GET", "/users/me", token), 401, "INVALID_ACCESS_TOKEN")
        failed = self.call("POST", "/auth/login", json={"identifier": "walker01", "password": PASSWORD})
        self.assert_error(failed, 401, "INVALID_CREDENTIALS")
        user_id = signed_up["id"]
        self.assertEqual((self.rows(User), self.rows(AuthSession), self.rows(UserConsent, user_id=user_id),
                          self.rows(UserPledgeAcknowledgement, user_id=user_id),
                          self.rows(PhoneVerification, user_id=user_id)), (0, 0, 0, 0, 0))

    def test_an_exit_is_refused_and_keeps_everything_when_activity_must_be_retained(self):
        # The record policy would delete consents and pledges here; the activity alone must block it.
        exit_policy = patch.dict(os.environ, {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "test"})
        exit_policy.start()
        self.addCleanup(exit_policy.stop)
        self.start(migrated=False)
        user_id = self.sign_up()["id"]
        token = self.log_in()
        self.pledge(token)
        self.assert_error(self.save_basic(token, self.verified(token), birth_date="2015-01-01"), 422,
                          "AGE_BELOW_MINIMUM")
        with self.Session.begin() as db:
            db.add(Survey(user_id=user_id))  # retained activity (Issue #41 list)

        def kept():
            return (self.rows(User), self.rows(AuthSession), self.rows(UserConsent, user_id=user_id),
                    self.rows(UserPledgeAcknowledgement, user_id=user_id),
                    self.rows(PhoneVerification, user_id=user_id), self.rows(Survey, user_id=user_id))

        before = kept()
        self.assertEqual((before[0], before[-1]), (1, 1))
        refused = self.call("DELETE", "/users/me", token, json={"password": PASSWORD})
        self.assert_error(refused, 409, "ACCOUNT_HAS_RETAINED_ACTIVITY")
        self.assertEqual(kept(), before)  # nothing was deleted
        self.assertEqual(self.me(token)["next_onboarding_step"], "AGE_REVIEW")  # the session still works

    # Roles

    def test_shelter_and_admin_log_in_by_id_and_are_not_onboarding_targets(self):
        self.start()
        with self.Session.begin() as db:
            db.execute(text("INSERT INTO shelters (id, name) VALUES (1, '보호소')"))
            db.add_all([User(login_id="shelter01", nickname="보호소담당", password=self.password_hash,
                             role=UserRole.SHELTER, shelter_id=1),
                        User(login_id="admin01", nickname="관리자", password=self.password_hash,
                             role=UserRole.ADMIN)])
        for login_id, role in (("shelter01", "shelter"), ("admin01", "admin")):
            with self.subTest(role=role):
                token = self.log_in(login_id)
                data = self.me(token)
                self.assertNotIn("email", data)
                self.assertEqual((data["role"], data["account_status"], data["pledge_completed"],
                                  data["current_pledge_version"]), (role, "ACTIVE", False, PLEDGE_VERSION))
                for field in ("step1_completed", "step2_completed", "step3_completed", "onboarding_completed",
                              "next_onboarding_step"):
                    self.assertIsNone(data[field], field)
                self.assert_error(self.pledge_call(token), 403, "PLEDGE_NOT_ALLOWED")
                for response in (self.send_code(token), self.save_experience(token), self.save_routine(token)):
                    self.assert_error(response, 403, "ONBOARDING_NOT_ALLOWED")
        self.assertEqual((self.rows(UserPledgeAcknowledgement), self.rows(PhoneVerification)), (0, 0))


if __name__ == "__main__":
    unittest.main()
