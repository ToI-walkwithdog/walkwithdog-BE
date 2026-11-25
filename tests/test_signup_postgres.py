"""Real PostgreSQL tests for sign-up, consent, pledges, concurrency and migrations.

Each migration test checks the DDL and preserved data at its own stage before
applying any remaining files needed by the current API, without create_all().

SIGNUP_TEST_DATABASE_URL must name an EMPTY local toi_walk_test_* database.
Each test works in its own new su_<uuid> schema and drops only that schema
afterwards, so the same test DB can be reused; no other schema is touched.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import re as regex
import sys
from threading import Barrier, Event, Thread, local
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

# app.database builds its engine from DATABASE_URL at import time. Pin it to an
# in-memory SQLite URL so this file imports on its own, never touches the dev DB
# named in DATABASE_URL or .env, and does not depend on another test importing app
# first. Every test overrides get_db with SIGNUP_TEST_DATABASE_URL.
with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import age_review_service, basic_info_service, pledge_service, signup_service
    from app.account_status import change_to_age_review
    from app.auth import UNUSABLE_PASSWORD, hash_password, token_digest
    from app.database import Base, get_db
    from app.models import (AuthSession, PhoneVerification, PhoneVerificationDailyCount, Survey, User, UserConsent,
                            UserPledgeAcknowledgement, UserRole, VolunteerOnboardingBasic)
    from app.onboarding_common import get_clock
    from app.phone_verification_service import phone_subject
    from app.phone_verification_service import SmsDispatcher, get_sms_dispatcher
    from app.routers import (age_review, auth, chat, matching, onboarding_basic, onboarding_experience,
                             onboarding_routine, surveys, users, walk_requests, walks)
    from app.models import VolunteerExperienceChallenge, VolunteerOnboardingExperience
    from app.models import (DOG_SIZE_CODES, TIME_SLOT_CODES, WALKING_DURATION_CODES, VolunteerOnboardingRoutine,
                            VolunteerRoutineRegion, VolunteerRoutineTimeSlot)
    from app.region_models import LEVEL_SIDO, LEVEL_SIGUNGU, LEVEL_SPECIAL_SELECTABLE, Region
    from app.models import ChatMessage, ChatSession, Dog, DogWalkSetting, DogWalkSlot, MatchingResult, Shelter
    from app.walk_models import VolunteerProfile, Walk, WalkRequest
    from app.sms_provider import MockSmsProvider, get_sms_provider

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from privacy_test_support import TEST_DOCUMENT_VERSION, TEST_TERMS_VERSION, onboarding_agreements, privacy, use_test_privacy_policy
from walk_request_test_support import seed_completed_onboarding

from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
SCHEMA_NAME = re.compile(r"su_[0-9a-f]{32}")
ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "migrations" / "016_onboarding_signup.sql").read_text(encoding="utf-8")
EMAIL_REMOVAL = (ROOT / "migrations" / "017_remove_user_email.sql").read_text(encoding="utf-8")
CONSENT_MIGRATION = (ROOT / "migrations" / "018_user_consents.sql").read_text(encoding="utf-8")
GUIDE_MIGRATION = "019_first_walk_guide.sql"
PLEDGE_MIGRATION = "023_user_pledge_acknowledgements.sql"
STEP1_MIGRATION = "024_onboarding_basic_info.sql"
STEP2_MIGRATION = "025_onboarding_dog_experience.sql"
REGION_MIGRATION = "022_regions.sql"
STEP3_MIGRATION = "026_onboarding_walking_routine.sql"
AGREEMENTS_MIGRATION = "027_onboarding_agreements.sql"
PROFILE_MIGRATION = "035_profile_phone_change.sql"  # Issue #103: users.profile_version, SMS purpose
PREFERENCE_MIGRATION = "036_walk_preferences.sql"  # Issue #105: deleted by sign-up exit
SUPPRESSED_MIGRATION = "037_phone_verification_suppressed.sql"  # Issue #110: SUPPRESSED send status
USER_COLUMNS_AFTER_018 = {
    "first_walk_guide_completed_at": GUIDE_MIGRATION,
    "account_status": STEP1_MIGRATION,
    "age_review_required_at": STEP1_MIGRATION,
    "profile_version": PROFILE_MIGRATION,
}
# The current API reads the Step 3 table (next onboarding step), and 026 references regions
# from 022. 022 depends on nothing, so it runs right before 026: every stage that stops at
# 019 to 025 still gets it.
API_MIGRATIONS_AFTER_018 = (GUIDE_MIGRATION, PLEDGE_MIGRATION, STEP1_MIGRATION, STEP2_MIGRATION, REGION_MIGRATION,
                            STEP3_MIGRATION, AGREEMENTS_MIGRATION, PROFILE_MIGRATION, PREFERENCE_MIGRATION,
                            SUPPRESSED_MIGRATION)
STEP3_FULL = {"region_codes": ["11680", "26350"], "available_time_slot_codes": ["WEEKDAY_MORNING", "WEEKEND_MORNING"],
              "walking_duration_code": "UP_TO_30_MIN", "comfortable_dog_size_code": "SMALL",
              "agreements": onboarding_agreements()}
# Synthetic region master rows for Step 3 tests.
STEP3_REGIONS = (("11", "서울특별시", LEVEL_SIDO, None), ("11680", "강남구", LEVEL_SIGUNGU, "11"),
                 ("26", "부산광역시", LEVEL_SIDO, None), ("26350", "해운대구", LEVEL_SIGUNGU, "26"),
                 ("36", "세종특별자치시", LEVEL_SIDO, None),
                 ("36_SPECIAL_ALL", "세종특별자치시 전체", LEVEL_SPECIAL_SELECTABLE, "36"))
GUIDE_COLUMN = ("first_walk_guide_completed_at", "TIMESTAMP", True)
PLEDGE_VERSION = "pledge-test-v1"  # test-only value; the real version is an undecided server setting
STEP2_FULL = {"has_experience": True, "experienced_breeds_text": "믹스견", "dog_size_code": "MEDIUM",
              "walking_frequency": "WEEKLY_3_4", "perceived_difficulty": "NORMAL", "challenge_codes": ["LEASH_PULLING"]}
HMAC_KEY = "test-only-hmac-key-0123456789abcdef"  # test-only secret
STEP1_START = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)
# Real files a dev DB has applied to users and walk tables before 016, in order.
# 014 (F-08) adds users.phone; 015 (F-09) needs walk_reviews from 007.
DEV_CHAIN_BEFORE_016 = ("005_user_roles.sql", "006_user_shelters.sql", "007_walk_domain.sql",
                        "014_f08_live_walk.sql", "015_f09_walk_review.sql")
USER_REFERENCES = {("surveys", "user_id"), ("volunteer_profiles", "user_id"), ("walk_requests", "user_id"),
                   ("walk_requests", "reviewed_by"), ("walk_incidents", "reported_by")}
PASSWORD = "WalkTogether2026!"
INDEXES = {"uq_users_login_id_lower": "lower((login_id)::text)",
           "uq_users_nickname_lower": "lower((nickname)::text)"}
# 016 also creates the email index; 017 removes it together with the column.
INDEXES_AFTER_016 = {**INDEXES, "uq_users_email_lower": "lower((email)::text)"}


@unittest.skipUnless(TEST_URL, "SIGNUP_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다")
class SignupPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(TEST_URL)
        validate_local_engine(cls.admin)
        if not cls.admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        with cls.admin.connect() as connection:
            inspector = inspect(connection)
            custom = [s for s in inspector.get_schema_names()
                      if s not in ("public", "information_schema") and not s.startswith("pg_")]
            if inspector.get_table_names() or custom:
                raise PreparationError("새로운 빈 테스트 DB만 허용합니다.")
        cls.created_schemas = set()

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.admin.connect() as connection:
                left = sorted(set(inspect(connection).get_schema_names()) & cls.created_schemas)
            if left:
                raise PreparationError(f"정리되지 않은 테스트 스키마: {', '.join(left)}")
        finally:
            cls.admin.dispose()

    def setUp(self):
        use_test_privacy_policy(self)
        pledge_env = patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION,
                                             "PHONE_VERIFICATION_HMAC_KEY": HMAC_KEY})
        pledge_env.start()
        self.addCleanup(pledge_env.stop)
        self.schema = "su_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.created_schemas.add(self.schema)
        # Cleanups run last-in first-out: client and engine close before the drop.
        self.addCleanup(self.drop_own_schema, self.schema)
        self.engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={self.schema}"})
        self.addCleanup(self.engine.dispose)

    def drop_own_schema(self, schema):
        """Drop only a schema this run created, owned by the test role, in the test DB.

        Runs on success and failure (addCleanup). Any check that fails leaves the schema
        in place and raises, so the test reports the leftover instead of guessing.
        """
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
            # CASCADE removes only objects inside this schema; the DB was verified empty
            # in setUpClass, so nothing outside it can depend on them.
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        self.created_schemas.discard(schema)

    def start_api(self, create_schema=True):
        # create_schema=False runs the API on tables built only by migration files.
        if create_schema:
            Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(users.router)
        app.include_router(onboarding_basic.router)
        app.include_router(age_review.router)
        self.now = STEP1_START
        self.sms = MockSmsProvider()
        app.dependency_overrides[get_clock] = lambda: (lambda: self.now)
        app.dependency_overrides[get_sms_provider] = lambda: self.sms
        self.dispatcher = SmsDispatcher(inline=True)  # delivery runs inside the request (#110)
        app.dependency_overrides[get_sms_dispatcher] = lambda: self.dispatcher

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        # Concurrency tests send more sign-ups than the per-IP limit; limits are tested separately.
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            app.dependency_overrides[limiter] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def body(self, suffix, **changes):
        return {"login_id": "user" + suffix.replace("_", ""), "nickname": f"nick_{suffix}",
                "password": PASSWORD, "privacy": privacy(), **changes}

    def count(self):
        with self.Session() as db:
            return db.query(User).count()

    def run_sql_file(self, sql):
        raw = self.engine.raw_connection()
        driver = raw.driver_connection
        try:
            driver.autocommit = True  # the file manages its own BEGIN/COMMIT, like psql
            with driver.cursor() as cursor:
                try:
                    cursor.execute(sql)
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
        finally:
            driver.autocommit = False
            raw.close()

    def create_pre_016_tables(self):
        """Users as created before migration 016, plus a referencing table."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE shelters (id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL)")
            connection.exec_driver_sql(
                "CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL UNIQUE,"
                " nickname VARCHAR(50) NOT NULL, password VARCHAR(255),"
                " shelter_id INTEGER CONSTRAINT fk_users_shelter_id REFERENCES shelters(id),"
                " created_at TIMESTAMPTZ DEFAULT now())")
            connection.exec_driver_sql(
                "CREATE TABLE surveys (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id))")

    def create_dev_schema_before_016(self):
        """Legacy users plus minimal dogs/surveys, then the real migrations dev ran before 016."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE shelters (id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL)")
            connection.exec_driver_sql(
                "CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL UNIQUE,"
                " nickname VARCHAR(50) NOT NULL, password VARCHAR(255), created_at TIMESTAMPTZ DEFAULT now())")
            connection.exec_driver_sql("CREATE TABLE dogs (id SERIAL PRIMARY KEY, name VARCHAR(100))")
            connection.exec_driver_sql(
                "CREATE TABLE surveys (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id))")
        for name in DEV_CHAIN_BEFORE_016:
            self.run_sql_file((ROOT / "migrations" / name).read_text(encoding="utf-8"))

    def schema_snapshot(self):
        """Columns, indexes and constraints of every table in this test's schema."""
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            tables = {name: sorted((c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(name))
                      for name in inspector.get_table_names()}
            indexes = sorted(tuple(row) for row in connection.execute(text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")))
            constraints = sorted(tuple(row) for row in connection.execute(text(
                "SELECT t.relname, c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c"
                " JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE n.nspname = current_schema()")))
        return tables, indexes, constraints

    def users_structure(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            columns = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns("users")}
            indexes = {row.indexname: row.indexdef for row in connection.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"
                " AND tablename = 'users'"))}
            survey_fk = [(f["constrained_columns"], f["referred_table"]) for f in inspector.get_foreign_keys("surveys")]
            user_fk = [(f["constrained_columns"], f["referred_table"]) for f in inspector.get_foreign_keys("users")]
        return columns, indexes, survey_fk, user_fk

    def run_migration(self, name):
        self.run_sql_file((ROOT / "migrations" / name).read_text(encoding="utf-8"))

    def migrate_users_for_current_models(self, *, after=None):
        """Finish the SQL chain after the stage under test, before calling the current API."""
        migrations = API_MIGRATIONS_AFTER_018
        if after is not None:
            migrations = migrations[migrations.index(after) + 1:]
        for name in migrations:
            self.run_migration(name)
        with self.engine.connect() as connection:
            columns = inspect(connection).get_columns("users")
        self.assertEqual({column["name"] for column in columns}, set(User.__table__.columns.keys()))

    # Schema creation and constraints

    def test_model_schema_has_case_insensitive_unique_indexes(self):
        self.start_api()
        with self.engine.connect() as connection:
            rows = {row.indexname: row.indexdef for row in connection.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'users'"))}
        for name, expression in INDEXES.items():
            with self.subTest(index=name):
                self.assertIn("CREATE UNIQUE INDEX", rows[name])
                self.assertIn(f"({expression})", rows[name])
        with self.Session.begin() as db:
            db.add(User(login_id="walker04", nickname="Walker", password=UNUSABLE_PASSWORD))
        for changes in ({"login_id": "WALKER04"}, {"nickname": "wALKER"}):
            with self.subTest(changes=changes):
                values = {"login_id": "other", "nickname": "other", **changes}
                with self.assertRaises(IntegrityError), self.Session.begin() as db:
                    db.add(User(**values, password=UNUSABLE_PASSWORD))
        self.assertEqual(self.count(), 1)

    def test_constraint_violation_maps_to_the_right_field(self):
        self.start_api()
        self.assertEqual(self.client.post("/users/", json=self.body("a", nickname="Walker_King")).status_code, 200)
        cases = (({"login_id": "USERA"}, "LOGIN_ID_ALREADY_EXISTS", "login_id"),
                 ({"nickname": "walker_king"}, "NICKNAME_ALREADY_EXISTS", "nickname"))
        with patch("app.signup_service.first_taken_field", return_value=None):
            for index, (changes, code, field) in enumerate(cases):
                with self.subTest(code=code):
                    response = self.client.post("/users/", json=self.body(f"b{index}", **changes))
                    self.assertEqual((response.status_code, response.json()["code"], response.json()["data"]),
                                     (409, code, {"field": field}))
        self.assertEqual(self.count(), 1)

    # Concurrency

    def test_signup_after_another_user_passes_the_pre_check_gets_409_and_rolls_back(self):
        self.start_api()
        for field, code in (("login_id", "LOGIN_ID_ALREADY_EXISTS"), ("nickname", "NICKNAME_ALREADY_EXISTS")):
            with self.subTest(field=field):
                shared = self.body(f"shared_{field}")[field]
                first = self.body(f"first_{field}", **{field: shared})
                second = self.body(f"second_{field}", **{field: shared.upper()})
                original = signup_service.first_taken_field
                passed, release = Event(), Event()

                def gated(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if not passed.is_set():
                        passed.set()
                        if not release.wait(8):
                            raise AssertionError("barrier timed out")
                    return result

                before = self.count()
                with patch("app.signup_service.first_taken_field", side_effect=gated), \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(self.client.post, "/users/", json=first)
                    try:
                        self.assertTrue(passed.wait(8), "first request did not reach the pre-check")
                        winner = self.client.post("/users/", json=second)
                    finally:
                        release.set()
                    loser = pending.result(timeout=15)
                self.assertEqual(winner.status_code, 200, winner.text)
                self.assertEqual((loser.status_code, loser.json()["code"], loser.json()["data"]),
                                 (409, code, {"field": field}))
                self.assertEqual(self.count(), before + 1)
                with self.Session() as db:
                    record = db.query(UserConsent).filter_by(user_id=winner.json()["data"]["id"]).one()
                    self.assertEqual(record.consent_type, "PRIVACY_COLLECTION_USE")
                    self.assertEqual(db.query(UserConsent).count(), before + 1)

    def test_parallel_identical_signups_store_exactly_one_row(self):
        self.start_api()
        workers = 6
        barrier = Barrier(workers)
        original = signup_service.first_taken_field

        def synchronized(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(10)  # every request passes the pre-check before any insert
            return result

        bodies = [self.body(f"p{index}", login_id="samelogin") for index in range(workers)]
        with patch("app.signup_service.first_taken_field", side_effect=synchronized), \
                ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(lambda body: self.client.post("/users/", json=body), bodies))
        statuses = sorted(response.status_code for response in responses)
        self.assertEqual(statuses, [200] + [409] * (workers - 1))
        self.assertEqual({r.json()["code"] for r in responses if r.status_code == 409}, {"LOGIN_ID_ALREADY_EXISTS"})
        self.assertEqual(self.count(), 1)
        winning_id = next(response.json()["data"]["id"] for response in responses if response.status_code == 200)
        with self.Session() as db:
            consent = db.query(UserConsent).one()
            self.assertEqual((consent.user_id, consent.consent_type, consent.document_version),
                             (winning_id, "PRIVACY_COLLECTION_USE", TEST_DOCUMENT_VERSION))

    def test_consent_insert_failure_rolls_back_account_and_all_consents(self):
        self.start_api()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE user_consents ADD CONSTRAINT test_reject_one_consent "
                "CHECK (consent_type <> 'PRIVACY_COLLECTION_USE')"
            )
        body = self.body("atomic", privacy=privacy(ai_overseas_transfer_agreed=True))
        response = self.client.post("/users/", json=body)
        self.assertEqual((response.status_code, response.json()["code"], response.json()["data"]),
                         (500, "INTERNAL_SERVER_ERROR", {}))
        with self.Session() as db:
            self.assertEqual(db.query(User).count(), 0)
            self.assertEqual(db.query(UserConsent).count(), 0)

        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE user_consents DROP CONSTRAINT test_reject_one_consent")
        retry = self.client.post("/users/", json=body)
        self.assertEqual(retry.status_code, 200, retry.text)
        with self.Session() as db:
            account = db.get(User, retry.json()["data"]["id"])
            self.assertIsNotNone(account.age_14_plus_confirmed_at.utcoffset())
            records = db.query(UserConsent).all()
            self.assertEqual({rec.consent_type for rec in records}, {"PRIVACY_COLLECTION_USE", "AI_OVERSEAS_TRANSFER"})
            self.assertEqual(len(records), 2)
            for record in records:
                self.assertEqual((record.user_id, record.document_version, record.action, record.recorded_at),
                                 (account.id, TEST_DOCUMENT_VERSION, "AGREE", account.age_14_plus_confirmed_at))

    # Migration 016

    def test_migration_016_on_empty_users_matches_the_model(self):
        self.create_pre_016_tables()
        self.run_sql_file(MIGRATION)
        columns, indexes, survey_fk, user_fk = self.users_structure()
        self.assertEqual(columns["login_id"], ("VARCHAR(20)", False))
        self.assertEqual(columns["nickname"], ("VARCHAR(20)", False))
        self.assertEqual(columns["email"], ("VARCHAR(100)", False))
        self.assertEqual(columns["password"], ("VARCHAR(255)", False))
        for name, expression in INDEXES_AFTER_016.items():
            with self.subTest(index=name):
                self.assertIn(f"({expression})", indexes[name])
        self.assertIn("users_email_key", indexes)
        self.assertEqual(survey_fk, [(["user_id"], "users")])
        self.assertEqual(user_fk, [(["shelter_id"], "shelters")])
        # Re-running on the migrated empty table is a no-op.
        self.run_sql_file(MIGRATION)

    def test_migration_016_aborts_without_changes_when_users_has_rows(self):
        self.create_pre_016_tables()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO users (id, email, nickname) VALUES (1, 'legacy@example.com', '기존 사용자')")
            connection.exec_driver_sql("INSERT INTO surveys (user_id) VALUES (1)")
        before = self.users_structure()
        with self.assertRaisesRegex(Exception, "users has existing rows"):
            self.run_sql_file(MIGRATION)
        self.assertEqual(self.users_structure(), before)
        self.assertNotIn("login_id", before[0])
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT email, nickname, password FROM users")).one()
            self.assertEqual(tuple(row), ("legacy@example.com", "기존 사용자", None))
            self.assertEqual(connection.execute(text("SELECT count(*) FROM surveys")).scalar_one(), 1)

    # Migration 016 after F-08 (014) and F-09 (015)

    def test_migration_016_after_f08_f09_keeps_their_schema_and_signup_works_without_create_all(self):
        self.create_dev_schema_before_016()
        before_tables, before_indexes, before_constraints = self.schema_snapshot()
        self.assertEqual(dict(before_tables)["users"].count(("phone", "VARCHAR(50)", True)), 1)
        self.run_sql_file(MIGRATION)
        tables, indexes, constraints = self.schema_snapshot()
        columns, user_indexes, _, user_fk = self.users_structure()
        self.assertEqual(columns["phone"], ("VARCHAR(50)", True))  # F-08 column kept as is
        self.assertEqual(columns["login_id"], ("VARCHAR(20)", False))
        self.assertEqual(columns["nickname"], ("VARCHAR(20)", False))
        self.assertEqual(columns["email"], ("VARCHAR(100)", False))
        self.assertEqual(columns["password"], ("VARCHAR(255)", False))
        with self.engine.connect() as connection:  # str() of a reflected ENUM is generic VARCHAR; ask the catalog
            role_type = connection.execute(text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
                " WHERE attrelid = 'users'::regclass AND attname = 'role'")).scalar_one()
        self.assertEqual((role_type, columns["role"][1]), ("userrole", False))
        for name, expression in INDEXES_AFTER_016.items():
            with self.subTest(index=name):
                self.assertIn("CREATE UNIQUE INDEX", user_indexes[name])
                self.assertIn(f"({expression})", user_indexes[name])
        self.assertEqual(user_fk, [(["shelter_id"], "shelters")])
        # 016 changes users only: every other table, index and constraint is identical.
        self.assertEqual({k: v for k, v in tables.items() if k != "users"},
                         {k: v for k, v in before_tables.items() if k != "users"})
        self.assertEqual([i for i in indexes if i[0] != "users"], [i for i in before_indexes if i[0] != "users"])
        self.assertEqual([c for c in constraints if c[0] != "users"],
                         [c for c in before_constraints if c[0] != "users"])
        # F-09 (015) objects are present and every walk-domain reference to users survives.
        self.assertIn("walk_review_tags", tables)
        self.assertLessEqual({("rating", "INTEGER", True), ("comment", "VARCHAR(100)", True)},
                             set(tables["walk_reviews"]))
        self.assertLessEqual({"ck_walk_review_rating", "ck_review_observation"},
                             {c[1] for c in constraints if c[0] == "walk_reviews"})
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            references = {(table, fk["constrained_columns"][0]) for table in inspector.get_table_names()
                          for fk in inspector.get_foreign_keys(table) if fk["referred_table"] == "users"}
        self.assertLessEqual(USER_REFERENCES, references)
        self.run_sql_file(MIGRATION)  # re-running on the still-empty migrated table is a no-op
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        # The current code no longer writes email, so it ships with 017 (same deployment).
        # The current API also needs the consent, guide and onboarding schema.
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
        self.migrate_users_for_current_models()
        snapshot = self.schema_snapshot()
        # The ORM and sign-up API work on the migrated tables with no create_all().
        self.start_api(create_schema=False)
        response = self.client.post("/users/", json=self.body("chain", login_id="ChainUser1"))
        self.assertEqual(response.status_code, 200, response.text)
        duplicate = self.client.post("/users/", json=self.body("other", login_id="CHAINUSER1"))
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]), (409, "LOGIN_ID_ALREADY_EXISTS"))
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT login_id, role::text, phone FROM users")).one()
        self.assertEqual(tuple(row), ("chainuser1", "USER", None))
        # Once an account exists, re-applying 016 is refused and changes nothing.
        with self.assertRaisesRegex(Exception, "016: users has existing rows"):
            self.run_sql_file(MIGRATION)
        self.assertEqual(self.schema_snapshot(), snapshot)

    def test_migration_016_after_f08_aborts_and_preserves_rows_including_phone(self):
        self.create_dev_schema_before_016()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO users (id, email, nickname, phone)"
                                       " VALUES (1, 'legacy@example.com', '기존 사용자', '010-0000-0000')")
            connection.exec_driver_sql("INSERT INTO surveys (user_id) VALUES (1)")
            connection.exec_driver_sql("INSERT INTO volunteer_profiles (user_id) VALUES (1)")
        before = self.schema_snapshot()
        with self.assertRaisesRegex(Exception, "016: users has existing rows"):
            self.run_sql_file(MIGRATION)
        self.assertEqual(self.schema_snapshot(), before)  # the whole transaction rolled back
        self.assertNotIn("login_id", dict((c[0], c) for c in before[0]["users"]))
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT email, nickname, password, phone, role::text FROM users")).one()
            self.assertEqual(tuple(row), ("legacy@example.com", "기존 사용자", None, "010-0000-0000", "USER"))
            for table in ("surveys", "volunteer_profiles"):
                self.assertEqual(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one(), 1)


    # Migration 017 (email removal)

    def migrate_dev_chain_through_016_with_accounts(self):
        """016 on the real dev chain, then accounts of every role as they would exist before 017."""
        self.create_dev_schema_before_016()
        # Login sessions (002) exist in every dev DB; the minimal chain above does not need them.
        self.run_sql_file((ROOT / "migrations" / "002_auth_sessions.sql").read_text(encoding="utf-8"))
        self.run_sql_file(MIGRATION)
        self.password_hash = hash_password(PASSWORD)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO shelters (id, name) VALUES (1, '보호소')")
            # login_id values cover the pre-2026-11-23 rule (underscore) and the current one.
            connection.execute(text(
                "INSERT INTO users (id, login_id, email, nickname, password, role, shelter_id, phone) VALUES"
                " (1, 'walker_one', 'one@example.com', '봉사자', :hash, 'USER', NULL, '010-0000-0000'),"
                " (2, 'shelter01', 'shelter@example.com', '보호소담당', :hash, 'SHELTER', 1, NULL),"
                " (3, 'admin01', 'admin@example.com', '관리자', :hash, 'ADMIN', NULL, NULL)"),
                {"hash": self.password_hash})
            connection.exec_driver_sql("SELECT setval(pg_get_serial_sequence('users', 'id'), 3)")
            connection.exec_driver_sql("INSERT INTO surveys (user_id) VALUES (1)")

    def user_rows(self):
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(
                "SELECT id, login_id, nickname, password, role::text, shelter_id, phone, created_at"
                " FROM users ORDER BY id"))]

    def test_migration_017_drops_only_email_and_every_role_logs_in_by_id(self):
        self.migrate_dev_chain_through_016_with_accounts()
        before_rows = self.user_rows()
        before_tables, before_indexes, before_constraints = self.schema_snapshot()
        self.run_sql_file(EMAIL_REMOVAL)
        tables, indexes, constraints = self.schema_snapshot()
        columns, user_indexes, survey_fk, user_fk = self.users_structure()
        self.assertNotIn("email", columns)
        self.assertNotIn("uq_users_email_lower", user_indexes)
        self.assertNotIn("users_email_key", user_indexes)
        self.assertNotIn("users_email_key", {c[1] for c in constraints})
        for name, expression in INDEXES.items():
            with self.subTest(index=name):
                self.assertIn("CREATE UNIQUE INDEX", user_indexes[name])
                self.assertIn(f"({expression})", user_indexes[name])
        self.assertEqual(columns["login_id"], ("VARCHAR(20)", False))
        self.assertEqual((survey_fk, user_fk), ([(["user_id"], "users")], [(["shelter_id"], "shelters")]))
        # Only the email column, its index and its constraint changed. Rows are intact.
        self.assertEqual(sorted(tables["users"]),
                         sorted(c for c in before_tables["users"] if c[0] != "email"))
        self.assertEqual({k: v for k, v in tables.items() if k != "users"},
                         {k: v for k, v in before_tables.items() if k != "users"})
        self.assertEqual(indexes, [i for i in before_indexes if i[1] not in ("uq_users_email_lower", "users_email_key")])
        self.assertEqual(constraints, [c for c in before_constraints if c[1] != "users_email_key"])
        self.assertEqual(self.user_rows(), before_rows)
        self.run_sql_file(EMAIL_REMOVAL)  # re-running is a no-op
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        # Finish the schema required by the current user lookup and onboarding code.
        self.run_sql_file(CONSENT_MIGRATION)
        self.migrate_users_for_current_models()
        self.start_api(create_schema=False)
        for identifier, user_id, role in (("walker_one", 1, "user"), ("Shelter01", 2, "shelter"),
                                          (" ADMIN01 ", 3, "admin")):
            with self.subTest(role=role):
                login = self.client.post("/auth/login", json={"identifier": identifier, "password": PASSWORD})
                self.assertEqual(login.status_code, 200, login.text)
                headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
                me = self.client.get("/users/me", headers=headers).json()["data"]
                self.assertEqual((me["id"], me["role"]), (user_id, role))
                self.assertNotIn("email", me)
        email_login = self.client.post("/auth/login", json={"identifier": "one@example.com", "password": PASSWORD})
        self.assertEqual((email_login.status_code, email_login.json()["code"]), (401, "INVALID_CREDENTIALS"))
        # Existing and new accounts share the same case-insensitive ID space.
        self.assertEqual(self.client.post("/users/", json=self.body("new", login_id="NewWalker")).status_code, 200)
        clash = self.client.post("/users/", json=self.body("clash", login_id="SHELTER01"))
        self.assertEqual((clash.status_code, clash.json()["code"]), (409, "LOGIN_ID_ALREADY_EXISTS"))
        new_login = self.client.post("/auth/login", json={"identifier": "newwalker", "password": PASSWORD})
        self.assertEqual(new_login.status_code, 200, new_login.text)

    def test_migration_018_preserves_existing_accounts_and_adds_consent_history(self):
        self.migrate_dev_chain_through_016_with_accounts()
        self.run_sql_file(EMAIL_REMOVAL)
        original_accounts = self.user_rows()
        before = self.schema_snapshot()
        self.run_sql_file(CONSENT_MIGRATION)
        self.assertEqual(self.user_rows(), original_accounts)
        tables, indexes, constraints = self.schema_snapshot()
        self.assertEqual(tables["users"], sorted(before[0]["users"] + [
            ("age_14_plus_confirmed_at", "TIMESTAMP", True),
        ]))
        self.assertEqual({k: v for k, v in tables.items() if k not in ("users", "user_consents")},
                         {k: v for k, v in before[0].items() if k != "users"})
        self.assertEqual([i for i in indexes if i[0] != "user_consents"], before[1])
        self.assertEqual([c for c in constraints if c[0] != "user_consents"], before[2])
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            self.assertTrue(next(col["type"].timezone for col in inspector.get_columns("users")
                                 if col["name"] == "age_14_plus_confirmed_at"))
            self.assertEqual(inspector.get_pk_constraint("user_consents")["constrained_columns"], ["id"])
            fk, = inspector.get_foreign_keys("user_consents")
            self.assertEqual((fk["constrained_columns"], fk["referred_table"]), (["user_id"], "users"))
            idx, = inspector.get_indexes("user_consents")
            self.assertEqual(idx["column_names"], ["user_id", "consent_type", "recorded_at"])
            self.assertEqual(connection.execute(text(
                "SELECT count(*) FROM users WHERE age_14_plus_confirmed_at IS NULL")).scalar_one(), 3)
            self.assertEqual(connection.execute(text("SELECT count(*) FROM user_consents")).scalar_one(), 0)

        self.run_sql_file(CONSENT_MIGRATION)  # re-run before any later files
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))

        # The current API also needs 019, which adds one users column and nothing else.
        self.run_migration(GUIDE_MIGRATION)
        with_guide = self.schema_snapshot()
        self.assertEqual(with_guide[0]["users"], sorted(tables["users"] + [GUIDE_COLUMN]))
        self.assertEqual({k: v for k, v in with_guide[0].items() if k != "users"},
                         {k: v for k, v in tables.items() if k != "users"})
        self.assertEqual(with_guide[1:], (indexes, constraints))
        self.migrate_users_for_current_models(after=GUIDE_MIGRATION)
        current_schema = self.schema_snapshot()
        self.start_api(create_schema=False)
        response = self.client.post("/users/", json=self.body("consent"))
        self.assertEqual(response.status_code, 200, response.text)
        self.run_sql_file(CONSENT_MIGRATION)  # re-running 018 with consent rows present changes nothing
        self.assertEqual(self.schema_snapshot(), current_schema)
        with self.Session() as db:
            record = db.query(UserConsent).one()
            self.assertEqual(record.user_id, response.json()["data"]["id"])
            self.assertEqual(record.document_version, TEST_DOCUMENT_VERSION)

    # Migration 019 (F-14 first walk guide) and the current User model (Issue #65)

    def test_migration_019_adds_only_the_guide_column_and_matches_the_current_user_model(self):
        self.migrate_dev_chain_through_016_with_accounts()
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
        accounts = self.user_rows()
        before_tables, before_indexes, before_constraints = self.schema_snapshot()
        model_columns = set(User.__table__.columns.keys())
        # At the 018 stage, the only mapped users columns missing are the ones later files add.
        self.assertEqual(model_columns - {c[0] for c in before_tables["users"]}, set(USER_COLUMNS_AFTER_018))
        self.run_migration(GUIDE_MIGRATION)
        tables, indexes, constraints = self.schema_snapshot()
        self.assertEqual({c[0] for c in tables["users"]} - {c[0] for c in before_tables["users"]},
                         {column for column, migration in USER_COLUMNS_AFTER_018.items()
                          if migration == GUIDE_MIGRATION})
        self.assertEqual(tables["users"], sorted(before_tables["users"] + [GUIDE_COLUMN]))
        self.assertEqual({k: v for k, v in tables.items() if k != "users"},
                         {k: v for k, v in before_tables.items() if k != "users"})
        self.assertEqual((indexes, constraints), (before_indexes, before_constraints))
        self.assertEqual(self.user_rows(), accounts)  # existing accounts are untouched
        with self.engine.connect() as connection:
            self.assertTrue(next(col["type"].timezone for col in inspect(connection).get_columns("users")
                                 if col["name"] == "first_walk_guide_completed_at"))
            self.assertEqual(connection.execute(text(
                "SELECT count(*) FROM users WHERE first_walk_guide_completed_at IS NULL")).scalar_one(), 3)
        self.run_migration(GUIDE_MIGRATION)  # re-running is a no-op
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        self.migrate_users_for_current_models(after=GUIDE_MIGRATION)
        self.start_api(create_schema=False)
        login = self.client.post("/auth/login", json={"identifier": "walker_one", "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        self.assertEqual(self.client.get("/users/me", headers=headers).json()["data"]["id"], 1)
        response = self.client.post("/users/", json=self.body("guide"))
        self.assertEqual(response.status_code, 200, response.text)
        with self.Session() as db:
            created = db.get(User, response.json()["data"]["id"])
            self.assertIsNone(created.first_walk_guide_completed_at)

    def test_migration_017_aborts_without_changes_before_016(self):
        self.create_dev_schema_before_016()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO users (id, email, nickname) VALUES (1, 'legacy@example.com', '기존 사용자')")
        before = self.schema_snapshot()
        with self.assertRaisesRegex(Exception, "017: users.login_id is missing or nullable"):
            self.run_sql_file(EMAIL_REMOVAL)
        self.assertEqual(self.schema_snapshot(), before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT email FROM users")).scalar_one(), "legacy@example.com")

    def test_migration_017_aborts_without_changes_on_a_blank_login_id(self):
        self.migrate_dev_chain_through_016_with_accounts()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE users SET login_id = '  ' WHERE id = 2")
        before, before_rows = self.schema_snapshot(), self.user_rows()
        with self.assertRaisesRegex(Exception, "017: users has accounts with a blank login_id"):
            self.run_sql_file(EMAIL_REMOVAL)
        self.assertEqual((self.schema_snapshot(), self.user_rows()), (before, before_rows))

    def test_migration_017_aborts_without_changes_when_another_object_uses_email(self):
        self.migrate_dev_chain_through_016_with_accounts()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE VIEW user_emails AS SELECT id, email FROM users")
        before, before_rows = self.schema_snapshot(), self.user_rows()
        with self.assertRaises(Exception) as caught:
            self.run_sql_file(EMAIL_REMOVAL)
        # SQLSTATE, not message text: the server may answer in a localized language.
        self.assertEqual(getattr(caught.exception, "pgcode", None), "2BP01")  # dependent_objects_still_exist
        # The index drop before the failing DROP COLUMN was rolled back too.
        self.assertEqual((self.schema_snapshot(), self.user_rows()), (before, before_rows))
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM user_emails")).scalar_one(), 3)


    # Pledge (Issue #33) and migration 019

    def add_session(self, user_id, token):
        with self.Session.begin() as db:
            db.add(AuthSession(token_hash=token_digest(token), user_id=user_id,
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        return {"Authorization": f"Bearer {token}"}

    def pledge_rows(self):
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(
                "SELECT user_id, pledge_version, acknowledged_at FROM user_pledge_acknowledgements ORDER BY id"))]

    def test_migration_023_adds_only_the_pledge_table_and_pledges_work_without_create_all(self):
        self.migrate_dev_chain_through_016_with_accounts()
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
        self.run_migration(GUIDE_MIGRATION)
        accounts = self.user_rows()
        before_tables, before_indexes, before_constraints = self.schema_snapshot()
        self.run_migration(PLEDGE_MIGRATION)
        tables, indexes, constraints = self.schema_snapshot()
        self.assertEqual(self.user_rows(), accounts)
        self.assertEqual({k: v for k, v in tables.items() if k != "user_pledge_acknowledgements"}, before_tables)
        self.assertEqual([i for i in indexes if i[0] != "user_pledge_acknowledgements"], before_indexes)
        self.assertEqual([c for c in constraints if c[0] != "user_pledge_acknowledgements"], before_constraints)
        self.assertEqual(sorted(tables["user_pledge_acknowledgements"]), sorted([
            ("id", "BIGINT", False), ("user_id", "INTEGER", False),
            ("pledge_version", "VARCHAR(32)", False), ("acknowledged_at", "TIMESTAMP", False)]))
        new_constraints = {c[1]: c[2] for c in constraints if c[0] == "user_pledge_acknowledgements"}
        self.assertEqual(new_constraints["uq_user_pledge_ack_user_version"], "UNIQUE (user_id, pledge_version)")
        self.assertEqual(new_constraints["fk_user_pledge_ack_user_id"], "FOREIGN KEY (user_id) REFERENCES users(id)")
        self.run_migration(PLEDGE_MIGRATION)  # re-running is a no-op
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        # The API runs on the migrated tables with no create_all(): an existing user pledges,
        # shelter and admin accounts are refused.
        self.migrate_users_for_current_models(after=PLEDGE_MIGRATION)
        self.start_api(create_schema=False)
        login = self.client.post("/auth/login", json={"identifier": "walker_one", "password": PASSWORD})
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        first = self.client.put("/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        again = self.client.put("/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}, headers=headers)
        self.assertEqual(again.json()["data"], first.json()["data"])
        for identifier in ("shelter01", "admin01"):
            with self.subTest(identifier=identifier):
                token = self.client.post("/auth/login", json={"identifier": identifier, "password": PASSWORD})
                response = self.client.put("/users/me/pledge", json={"pledge_version": PLEDGE_VERSION},
                                           headers={"Authorization": f"Bearer {token.json()['data']['access_token']}"})
                self.assertEqual((response.status_code, response.json()["code"]), (403, "PLEDGE_NOT_ALLOWED"))
        self.assertEqual([row[:2] for row in self.pledge_rows()], [(1, PLEDGE_VERSION)])
        self.assertEqual(self.pledge_rows()[0][2], datetime.fromisoformat(first.json()["data"]["acknowledged_at"]))

    def test_unique_constraint_rejects_a_second_row_for_the_same_user_and_version(self):
        self.start_api()
        with self.Session.begin() as db:
            db.add(User(id=1, login_id="walker01", nickname="봉사자", password=UNUSABLE_PASSWORD))
            db.flush()
            db.add(UserPledgeAcknowledgement(user_id=1, pledge_version=PLEDGE_VERSION,
                                             acknowledged_at=datetime.now(timezone.utc)))
        with self.assertRaises(IntegrityError), self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=1, pledge_version=PLEDGE_VERSION,
                                             acknowledged_at=datetime.now(timezone.utc)))
        with self.Session.begin() as db:  # another version for the same user is allowed
            db.add(UserPledgeAcknowledgement(user_id=1, pledge_version="pledge-test-v0",
                                             acknowledged_at=datetime.now(timezone.utc)))
        self.assertEqual(len(self.pledge_rows()), 2)

    def test_parallel_same_version_pledges_store_one_row_and_return_the_same_time(self):
        self.start_api()
        with self.Session.begin() as db:
            db.add(User(id=1, login_id="walker01", nickname="봉사자", password=UNUSABLE_PASSWORD))
        headers = self.add_session(1, "pledge-token")
        workers = 6
        barrier = Barrier(workers)
        original = pledge_service._insert_if_absent

        def synchronized(*args, **kwargs):
            barrier.wait(10)  # every request saw "no record yet" before any of them inserts
            return original(*args, **kwargs)

        with patch("app.pledge_service._insert_if_absent", side_effect=synchronized), \
                ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(
                lambda _: self.client.put("/users/me/pledge", json={"pledge_version": PLEDGE_VERSION},
                                          headers=headers), range(workers)))
        self.assertEqual([r.status_code for r in responses], [200] * workers, [r.text for r in responses])
        self.assertEqual(len({r.json()["data"]["acknowledged_at"] for r in responses}), 1)
        rows = self.pledge_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], datetime.fromisoformat(responses[0].json()["data"]["acknowledged_at"]))

    def test_pledge_failure_after_insert_rolls_back(self):
        self.start_api()
        with self.Session.begin() as db:
            db.add(User(id=1, login_id="walker01", nickname="봉사자", password=UNUSABLE_PASSWORD))
        headers = self.add_session(1, "pledge-token")
        original = pledge_service._insert_if_absent

        def insert_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated failure after the insert")

        with patch("app.pledge_service._insert_if_absent", side_effect=insert_then_fail), \
                self.assertLogs("app.api_contract", "ERROR"):
            response = self.client.put("/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}, headers=headers)
        self.assertEqual((response.status_code, response.json()["code"]), (500, "INTERNAL_SERVER_ERROR"))
        self.assertEqual(self.pledge_rows(), [])
        retry = self.client.put("/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}, headers=headers)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(self.pledge_rows()), 1)


    # Onboarding Step 1 (Issue #40) and migration 024

    def step1_user(self, user_id, *, pledged=True):
        with self.Session.begin() as db:
            db.add(User(id=user_id, login_id=f"walker{user_id:02d}", nickname=f"봉사자{user_id}",
                        password=UNUSABLE_PASSWORD))
            db.flush()
            if pledged:
                db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION,
                                                 acknowledged_at=STEP1_START - timedelta(minutes=5)))
        return self.add_session(user_id, f"step1-token-{user_id}")

    def send_and_confirm(self, headers, phone="010-1234-5678"):
        sent = self.client.post("/users/me/phone-verifications", json={"phone_number": phone}, headers=headers)
        self.assertEqual(sent.status_code, 200, sent.text)
        verification_id = sent.json()["data"]["verification_id"]
        code = re.search(r"\d{6}", self.sms.outbox[-1]["text"]).group()
        confirmed = self.client.post("/users/me/phone-verifications/confirm",
                                     json={"verification_id": verification_id, "code": code}, headers=headers)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        return verification_id

    def basic_info(self, headers, verification_id, birth_date="2002-12-10"):
        return self.client.put("/users/me/onboarding/basic-info", headers=headers,
                               json={"full_name": "김민지", "birth_date": birth_date,
                                     "phone_verification_id": verification_id})

    def account_status(self, user_id):
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT account_status FROM users WHERE id = :id"),
                                      {"id": user_id}).scalar_one()

    def test_migration_024_keeps_accounts_and_step1_works_without_create_all(self):
        self.migrate_dev_chain_through_016_with_accounts()
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
        self.run_migration(GUIDE_MIGRATION)
        self.run_migration(PLEDGE_MIGRATION)
        accounts = self.user_rows()
        before_tables, before_indexes, before_constraints = self.schema_snapshot()
        self.run_migration(STEP1_MIGRATION)
        tables, indexes, constraints = self.schema_snapshot()
        self.assertEqual(self.user_rows(), accounts)  # includes users.phone (#28) unchanged
        with self.engine.connect() as connection:
            statuses = connection.execute(text(
                "SELECT account_status, age_review_required_at FROM users ORDER BY id")).all()
        self.assertEqual([tuple(row) for row in statuses], [("ACTIVE", None)] * 3)
        new_tables = {"volunteer_onboarding_basics", "phone_verifications", "phone_verification_daily_counts"}
        self.assertEqual(set(tables) - set(before_tables), new_tables)
        self.assertEqual({k: v for k, v in tables.items() if k not in new_tables | {"users"}},
                         {k: v for k, v in before_tables.items() if k != "users"})
        added_columns = set(tables["users"]) - set(before_tables["users"])
        self.assertEqual(added_columns,
                         {("account_status", "VARCHAR(24)", False), ("age_review_required_at", "TIMESTAMP", True)})
        self.assertEqual({column[0] for column in added_columns},
                         {column for column, migration in USER_COLUMNS_AFTER_018.items()
                          if migration == STEP1_MIGRATION})
        with self.engine.connect() as connection:
            user_columns = {column["name"]: column for column in inspect(connection).get_columns("users")}
        self.assertTrue(user_columns["age_review_required_at"]["type"].timezone)
        self.assertEqual([i for i in indexes if i[0] not in new_tables], before_indexes)
        self.assertEqual([c for c in constraints if c[0] not in new_tables and c[1] != "ck_users_account_status"],
                         before_constraints)
        self.assertIn(("volunteer_onboarding_basics", "volunteer_onboarding_basics_phone_e164_key",
                       "UNIQUE (phone_e164)"), constraints)
        self.assertIn(("users", "ck_users_account_status",
                       "CHECK (((account_status)::text = ANY ((ARRAY['ACTIVE'::character varying, "
                       "'AGE_REVIEW_REQUIRED'::character varying])::text[])))"), constraints)
        self.run_migration(STEP1_MIGRATION)  # re-running is a no-op
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        # The API runs on the migrated tables: login, pledge, then Step 1.
        self.migrate_users_for_current_models(after=STEP1_MIGRATION)
        self.start_api(create_schema=False)
        login = self.client.post("/auth/login", json={"identifier": "walker_one", "password": PASSWORD})
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        blocked = self.client.post("/users/me/phone-verifications", json={"phone_number": "010-1234-5678"},
                                   headers=headers)
        self.assertEqual((blocked.status_code, blocked.json()["data"]), (409, {"required_step": "PLEDGE"}))
        self.assertEqual(self.client.put("/users/me/pledge", json={"pledge_version": PLEDGE_VERSION},
                                         headers=headers).status_code, 200)
        saved = self.basic_info(headers, self.send_and_confirm(headers))
        self.assertEqual(saved.status_code, 200, saved.text)
        with self.engine.connect() as connection:
            phone = connection.execute(text("SELECT phone FROM users WHERE id = 1")).scalar_one()
        self.assertEqual(phone, "010-0000-0000")  # the #28 contact field is not overwritten

    def test_parallel_sends_of_one_account_send_once(self):
        self.start_api()
        headers = self.step1_user(1)
        workers = 6
        barrier = Barrier(workers)

        def send(_):
            barrier.wait(10)
            return self.client.post("/users/me/phone-verifications", json={"phone_number": "010-1234-5678"},
                                    headers=headers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = sorted(r.status_code for r in pool.map(send, range(workers)))
        self.assertEqual(statuses, [200] + [429] * (workers - 1))
        with self.Session() as db:
            self.assertEqual(db.query(PhoneVerification).count(), 1)
            self.assertEqual({row.send_count for row in db.query(PhoneVerificationDailyCount)}, {1})
        self.assertEqual(len(self.sms.outbox), 1)

    def test_parallel_sends_to_one_number_never_pass_the_daily_number_limit(self):
        self.start_api()
        headers = [self.step1_user(user_id) for user_id in range(1, 7)]
        with self.Session.begin() as db:
            db.add(PhoneVerificationDailyCount(scope="PHONE", subject=phone_subject(HMAC_KEY.encode(), "+821012345678"),
                                               kst_date=(STEP1_START + timedelta(hours=9)).date(), send_count=4))
        barrier = Barrier(len(headers))

        def send(header):
            barrier.wait(10)
            return self.client.post("/users/me/phone-verifications", json={"phone_number": "010-1234-5678"},
                                    headers=header)

        with ThreadPoolExecutor(max_workers=len(headers)) as pool:
            statuses = sorted(r.status_code for r in pool.map(send, headers))
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual(statuses.count(429), len(headers) - 1)
        with self.Session() as db:
            phone_counter = db.query(PhoneVerificationDailyCount).filter_by(scope="PHONE").one()
            self.assertEqual(phone_counter.send_count, 5)
            self.assertEqual(db.query(PhoneVerification).count(), 1)

    def test_two_accounts_saving_the_same_number_one_wins(self):
        self.start_api()
        first, second = self.step1_user(1), self.step1_user(2)
        ids = [self.send_and_confirm(first)]
        self.now += timedelta(seconds=61)  # the number's own resend wait
        ids.append(self.send_and_confirm(second))
        barrier = Barrier(2)
        original = basic_info_service.registered_to_other_user
        waited = set()

        def checked_then_wait(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2] not in waited:  # only the pre-check waits, not the conflict handling
                waited.add(args[2])
                barrier.wait(10)  # both pass the pre-check; only the UNIQUE constraint decides
            return result

        with patch("app.basic_info_service.registered_to_other_user", side_effect=checked_then_wait), \
                ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda pair: self.basic_info(*pair), zip((first, second), ids)))
        outcomes = sorted((r.status_code, r.json()["code"]) for r in responses)
        self.assertEqual(outcomes, [(200, "OK"), (409, "PHONE_ALREADY_REGISTERED")])
        with self.Session() as db:
            self.assertEqual(db.query(VolunteerOnboardingBasic).count(), 1)
            consumed = [row.consumed_at is not None for row in db.query(PhoneVerification)]
        self.assertEqual(sorted(consumed), [False, True])

    def test_parallel_saves_of_one_account_keep_one_row_and_one_time(self):
        self.start_api()
        headers = self.step1_user(1)
        verification_id = self.send_and_confirm(headers)
        workers = 4
        barrier = Barrier(workers)
        original = basic_info_service.age_years

        def synchronized(*args, **kwargs):
            barrier.wait(10)  # every request saw "not completed yet"
            return original(*args, **kwargs)

        with patch("app.basic_info_service.age_years", side_effect=synchronized), \
                ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(lambda _: self.basic_info(headers, verification_id), range(workers)))
        self.assertEqual([r.status_code for r in responses], [200] * workers, [r.text for r in responses])
        self.assertEqual(len({r.json()["data"]["completed_at"] for r in responses}), 1)
        with self.Session() as db:
            self.assertEqual(db.query(VolunteerOnboardingBasic).count(), 1)

    def test_status_change_waits_for_a_pledge_that_holds_the_share_lock(self):
        self.start_api()
        headers = self.step1_user(1)  # pledged: Step 1 is reachable
        locked, release = Event(), Event()
        original = pledge_service.find_acknowledgement

        def hold(*args, **kwargs):
            if not locked.is_set():
                locked.set()  # the pledge transaction now holds FOR SHARE on users row 1
                release.wait(10)
            return original(*args, **kwargs)

        results = {}
        with patch("app.pledge_service.find_acknowledgement", side_effect=hold):
            pledge = Thread(target=lambda: results.setdefault("pledge", self.client.put(
                "/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}, headers=headers)))
            pledge.start()
            self.assertTrue(locked.wait(10))
            save = Thread(target=lambda: results.setdefault("save", self.basic_info(
                headers, str(uuid4()), birth_date="2020-01-01")))
            save.start()
            time.sleep(0.5)
            # The under-14 status change is blocked by the pledge's share lock.
            self.assertTrue(save.is_alive())
            self.assertEqual(self.account_status(1), "ACTIVE")
            release.set()
            pledge.join(10)
            save.join(10)
        self.assertEqual(results["pledge"].status_code, 200, results["pledge"].text)
        self.assertEqual((results["save"].status_code, results["save"].json()["code"]), (422, "AGE_BELOW_MINIMUM"))
        self.assertEqual(self.account_status(1), "AGE_REVIEW_REQUIRED")
        self.assertEqual(len(self.pledge_rows()), 1)  # the history is kept

    def test_pledge_that_passed_the_route_check_is_refused_after_a_status_change(self):
        self.start_api()
        headers = self.step1_user(1, pledged=False)
        checked, release = Event(), Event()
        original = pledge_service.lock_user

        def wait_then_lock(*args, **kwargs):
            checked.set()  # the route already saw ACTIVE
            release.wait(10)
            return original(*args, **kwargs)

        results = {}
        with patch("app.pledge_service.lock_user", side_effect=wait_then_lock):
            pledge = Thread(target=lambda: results.setdefault("pledge", self.client.put(
                "/users/me/pledge", json={"pledge_version": PLEDGE_VERSION}, headers=headers)))
            pledge.start()
            self.assertTrue(checked.wait(10))
            with self.Session() as other:
                change_to_age_review(other, 1, self.now)
            release.set()
            pledge.join(10)
        self.assertEqual((results["pledge"].status_code, results["pledge"].json()["code"]), (403, "AGE_REVIEW_REQUIRED"))
        self.assertEqual(self.pledge_rows(), [])


    # Age review correction and sign-up exit (Issue #41)

    def age_review_user(self, user_id=1):
        """An AGE_REVIEW_REQUIRED account with a pledge and a consent, two sessions."""
        with self.Session.begin() as db:
            db.add(User(id=user_id, login_id=f"minor{user_id:02d}", nickname=f"연령{user_id}",
                        password=self.exit_hash, account_status="AGE_REVIEW_REQUIRED",
                        age_review_required_at=STEP1_START))
            db.flush()
            db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION, acknowledged_at=STEP1_START))
            db.add(UserConsent(user_id=user_id, consent_type="PRIVACY_COLLECTION_USE", document_version="privacy-test-v1",
                               action="AGREE", recorded_at=STEP1_START))
        self.add_session(user_id, f"second-device-{user_id}")
        return self.add_session(user_id, f"age-token-{user_id}")

    def setup_age_review(self):
        if not hasattr(type(self), "_exit_hash"):
            type(self)._exit_hash = hash_password(PASSWORD)
        self.exit_hash = type(self)._exit_hash
        records = patch.dict(os.environ, {"ACCOUNT_EXIT_RECORD_POLICY": "delete", "APP_ENV": "test"})
        records.start()
        self.addCleanup(records.stop)
        self.start_api()
        return self.age_review_user()

    def correct(self, headers):
        return self.client.put("/users/me/age-review/birth-date", json={"birth_date": "2006-05-17"}, headers=headers)

    def exit_account(self, headers):
        return self.client.request("DELETE", "/users/me", json={"password": PASSWORD}, headers=headers)

    def remaining(self, user_id=1):
        with self.engine.connect() as connection:
            return {table: connection.execute(text(f"SELECT count(*) FROM {table} WHERE {column} = :id"),
                                              {"id": user_id}).scalar_one()
                    for table, column in (("users", "id"), ("auth_sessions", "user_id"),
                                          ("user_consents", "user_id"), ("user_pledge_acknowledgements", "user_id"))}

    def hold(self, target):
        """Patch `target` so the first call blocks until released; returns (entered, release)."""
        entered, release = Event(), Event()
        module, name = target.rsplit(".", 1)
        original = getattr(sys.modules[module], name)

        def held(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                release.wait(10)
            return original(*args, **kwargs)

        patcher = patch(target, side_effect=held)
        patcher.start()
        self.addCleanup(patcher.stop)
        return entered, release

    def run_in_thread(self, results, key, call):
        thread = Thread(target=lambda: results.setdefault(key, call()))
        thread.start()
        return thread

    def test_correction_holding_the_lock_makes_a_concurrent_exit_see_active(self):
        headers = self.setup_age_review()
        entered, release = self.hold("app.age_review_service.step1_completed")  # runs under the FOR UPDATE lock
        results = {}
        correction = self.run_in_thread(results, "correction", lambda: self.correct(headers))
        self.assertTrue(entered.wait(10))
        exit_thread = self.run_in_thread(results, "exit", lambda: self.exit_account(headers))
        time.sleep(0.5)
        self.assertTrue(exit_thread.is_alive())  # waits for the correction's row lock
        release.set()
        correction.join(10)
        exit_thread.join(10)
        self.assertEqual(results["correction"].status_code, 200, results["correction"].text)
        self.assertEqual((results["exit"].status_code, results["exit"].json()["code"]), (409, "AGE_REVIEW_NOT_REQUIRED"))
        self.assertEqual(self.remaining(), {"users": 1, "auth_sessions": 2, "user_consents": 1,
                                            "user_pledge_acknowledgements": 1})
        self.assertEqual(self.account_status(1), "ACTIVE")

    def test_exit_holding_the_lock_makes_a_concurrent_correction_fail(self):
        headers = self.setup_age_review()
        entered, release = self.hold("app.age_review_service.has_retained_activity")
        results = {}
        exit_thread = self.run_in_thread(results, "exit", lambda: self.exit_account(headers))
        self.assertTrue(entered.wait(10))
        correction = self.run_in_thread(results, "correction", lambda: self.correct(headers))
        time.sleep(0.5)
        self.assertTrue(correction.is_alive())
        release.set()
        exit_thread.join(10)
        correction.join(10)
        self.assertEqual(results["exit"].status_code, 200, results["exit"].text)
        self.assertEqual((results["correction"].status_code, results["correction"].json()["code"]),
                         (401, "USER_NOT_FOUND"))
        self.assertEqual(self.remaining(), {"users": 0, "auth_sessions": 0, "user_consents": 0,
                                            "user_pledge_acknowledgements": 0})

    def test_parallel_identical_exits_and_corrections(self):
        headers = self.setup_age_review()
        workers = 4
        barrier = Barrier(workers)

        def corrected(_):
            barrier.wait(10)
            return self.correct(headers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = sorted((r.status_code, r.json()["code"]) for r in pool.map(corrected, range(workers)))
        self.assertEqual(outcomes, [(200, "OK")] + [(409, "AGE_REVIEW_NOT_REQUIRED")] * (workers - 1))
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE users SET account_status = 'AGE_REVIEW_REQUIRED' WHERE id = 1")
        barrier = Barrier(workers)

        def exited(_):
            barrier.wait(10)
            return self.exit_account(headers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(exited, range(workers)))
        codes = sorted((r.status_code, r.json()["code"]) for r in responses)
        self.assertEqual(codes[0], (200, "OK"), codes)
        self.assertTrue(all(status == 401 for status, _ in codes[1:]), codes)
        self.assertEqual(self.remaining(), {"users": 0, "auth_sessions": 0, "user_consents": 0,
                                            "user_pledge_acknowledgements": 0})

    def test_login_racing_an_exit_leaves_no_session(self):
        headers = self.setup_age_review()
        entered, release = self.hold("app.age_review_service.has_retained_activity")
        results = {}
        exit_thread = self.run_in_thread(results, "exit", lambda: self.exit_account(headers))
        self.assertTrue(entered.wait(10))
        login = self.run_in_thread(results, "login", lambda: self.client.post(
            "/auth/login", json={"identifier": "minor01", "password": PASSWORD}))
        time.sleep(1.0)  # the password check passes; the session insert waits on the users row lock
        release.set()
        exit_thread.join(10)
        login.join(10)
        self.assertEqual(results["exit"].status_code, 200, results["exit"].text)
        self.assertEqual((results["login"].status_code, results["login"].json()["code"]), (401, "INVALID_CREDENTIALS"))
        self.assertEqual(self.remaining()["auth_sessions"], 0)
        self.assertEqual(self.client.get("/users/me", headers=headers).status_code, 401)

    def test_fk_error_during_exit_rolls_back_on_postgres(self):
        headers = self.setup_age_review()
        with self.Session.begin() as db:
            db.add(Survey(user_id=1))
        with patch("app.age_review_service.has_retained_activity", return_value=False), \
                self.assertLogs("app.api_contract", "ERROR"):
            response = self.exit_account(headers)
        self.assertEqual((response.status_code, response.json()["code"]), (500, "INTERNAL_SERVER_ERROR"))
        self.assertEqual(self.remaining(), {"users": 1, "auth_sessions": 2, "user_consents": 1,
                                            "user_pledge_acknowledgements": 1})
        self.assertEqual(self.client.get("/users/me", headers=headers).status_code, 200)


    # Walk, chat, survey and matching writes racing the age review status change (Issue #41)

    def start_walk_api(self):
        """Real routers on the isolated schema: a general user (1), a shelter (3), a walkable dog."""
        from datetime import time as clock_time
        self.start_api()
        for router in (walk_requests.router, walks.router, surveys.router, matching.router, chat.router):
            self.client.app.include_router(router)
        self.walk_start = datetime(2030, 1, 7, tzinfo=timezone.utc)  # Monday 09:00 Asia/Seoul
        for target, value in (("app.walk_request_service.utc_now", datetime(2030, 1, 6, tzinfo=timezone.utc)),
                              ("app.walk_service.utc_now", self.walk_start + timedelta(minutes=5)),
                              ("app.routers.chat.configuration", None)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.Session.begin() as db:
            db.add(Shelter(id=1, name="보호소"))
            db.flush()
            db.add_all([User(id=1, login_id="walker01", nickname="봉사자", password=UNUSABLE_PASSWORD),
                        User(id=3, login_id="shelter01", nickname="보호소", password=UNUSABLE_PASSWORD,
                             role=UserRole.SHELTER, shelter_id=1),
                        Dog(id=1, name="강아지", shelter_id=1, energy_level=2)])
            db.flush()
            setting = DogWalkSetting(dog_id=1, walkable=True, difficulty="BEGINNER", recommended_duration_minutes=30)
            db.add(setting)
            db.flush()
            db.add(DogWalkSlot(walk_setting_id=setting.id, day_of_week=0, start_time=clock_time(9),
                               end_time=clock_time(12)))
        self.add_session(3, "shelter-token")
        return self.add_session(1, "walker-token")

    def wait_for_lock_waiter(self):
        """Block until a backend of this test DB waits on a lock (a condition, not a fixed delay)."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.admin.connect() as connection:
                waiting = connection.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock'")).scalar_one()
            if waiting:
                return
            time.sleep(0.02)
        self.fail("no backend started waiting for the row lock")

    def change_status_while(self, request):
        """Case B: the status change holds the users row FOR UPDATE (not committed yet) while
        `request` runs in a thread; it commits once the request waits on the row lock."""
        results = {}
        with self.engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SELECT id FROM users WHERE id = 1 FOR UPDATE"))
            connection.execute(text("UPDATE users SET account_status = 'AGE_REVIEW_REQUIRED',"
                                    " age_review_required_at = now() WHERE id = 1"))
            thread = Thread(target=lambda: results.setdefault("response", request()))
            thread.start()
            self.wait_for_lock_waiter()
            transaction.commit()
        thread.join(10)
        return results["response"]

    def table_count(self, model):
        with self.Session() as db:
            return db.query(model).count()

    def test_walk_request_that_locked_first_commits_before_the_status_change(self):
        headers = self.start_walk_api()
        with self.Session.begin() as db:
            seed_completed_onboarding(db, 1, pledge_version=PLEDGE_VERSION)
        entered, release = self.hold("app.walk_request_service.lock_resources")  # after the share lock
        results = {}
        body = {"dog_id": 1, "requested_start_at": self.walk_start.isoformat(),
                "requested_end_at": (self.walk_start + timedelta(hours=1)).isoformat()}
        request = self.run_in_thread(results, "request", lambda: self.client.post(
            "/walk-requests", json=body, headers=headers))
        self.assertTrue(entered.wait(10))

        def change():
            with self.Session() as db:
                change_to_age_review(db, 1, self.now)
            return "done"

        status_change = self.run_in_thread(results, "change", change)
        self.wait_for_lock_waiter()  # the status change waits for the request's share lock
        self.assertEqual(self.account_status(1), "ACTIVE")
        release.set()
        request.join(10)
        status_change.join(10)
        self.assertEqual(results["request"].status_code, 201, results["request"].text)
        self.assertEqual(results["change"], "done")
        self.assertEqual(self.account_status(1), "AGE_REVIEW_REQUIRED")
        self.assertEqual(self.table_count(WalkRequest), 1)  # recorded while the account was ACTIVE

    def test_walk_request_after_the_status_change_is_refused_without_records(self):
        headers = self.start_walk_api()
        body = {"dog_id": 1, "requested_start_at": self.walk_start.isoformat(),
                "requested_end_at": (self.walk_start + timedelta(hours=1)).isoformat()}
        response = self.change_status_while(lambda: self.client.post("/walk-requests", json=body, headers=headers))
        self.assertEqual((response.status_code, response.json()),
                         (403, {"detail": "연령 확인 정보를 먼저 수정해 주세요."}))
        self.assertEqual((self.table_count(WalkRequest), self.table_count(VolunteerProfile)), (0, 0))

    def test_walk_start_after_the_status_change_is_refused(self):
        headers = self.start_walk_api()
        with self.Session.begin() as db:
            db.add(WalkRequest(id=1, user_id=1, dog_id=1, shelter_id=1, requested_start_at=self.walk_start,
                               requested_end_at=self.walk_start + timedelta(hours=1), status="APPROVED",
                               reviewed_by=3, reviewed_at=self.walk_start - timedelta(days=1)))
            db.add(VolunteerProfile(user_id=1))
        response = self.change_status_while(lambda: self.client.post("/walks", json={"walk_request_id": 1},
                                                                     headers=headers))
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.table_count(Walk), 0)
        # The shelter's own actions are not blocked by the volunteer's status.
        shelter = {"Authorization": "Bearer shelter-token"}
        self.assertEqual(self.client.get("/walk-requests/1", headers=shelter).status_code, 200)

    def test_survey_and_matching_after_the_status_change_are_refused(self):
        headers = self.start_walk_api()
        response = self.change_status_while(lambda: self.client.post("/surveys/", json={"user_id": 1}, headers=headers))
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.table_count(Survey), 0)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE users SET account_status = 'ACTIVE', age_review_required_at = NULL")
        survey = self.client.post("/surveys/", json={"user_id": 1}, headers=headers)
        self.assertEqual(survey.status_code, 200, survey.text)
        survey_id = survey.json()["id"]
        response = self.change_status_while(lambda: self.client.post(f"/matching/{survey_id}", headers=headers))
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.table_count(MatchingResult), 0)

    def test_chat_message_checks_after_the_external_call_without_holding_the_users_row(self):
        headers = self.start_walk_api()
        with self.Session.begin() as db:
            survey = Survey(user_id=1)
            db.add(survey)
            db.flush()
            db.add(ChatSession(id=1, user_id=1, dog_id=1, survey_id=survey.id, context_snapshot={},
                               consent_to_external_ai=True))

        def reply_while_the_status_changes(*args, **kwargs):
            # The users row must not be locked during the external call: this commit would
            # otherwise wait for the request (and time out the test).
            with self.Session() as db:
                change_to_age_review(db, 1, self.now)
            return "응답", "stub-model"

        with patch("app.routers.chat.generate_reply", side_effect=reply_while_the_status_changes):
            response = self.client.post("/chat/sessions/1/messages", json={"message": "안녕"}, headers=headers)
        self.assertEqual((response.status_code, response.json()),
                         (403, {"detail": "연령 확인 정보를 먼저 수정해 주세요."}))
        self.assertEqual(self.table_count(ChatMessage), 0)
        self.assertEqual(self.account_status(1), "AGE_REVIEW_REQUIRED")

    def test_parallel_active_writes_of_one_user_do_not_deadlock(self):
        headers = self.start_walk_api()
        workers = 4
        barrier = Barrier(workers)

        def write(_):
            barrier.wait(10)
            return self.client.post("/surveys/", json={"user_id": 1}, headers=headers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = [r.status_code for r in pool.map(write, range(workers))]
        self.assertEqual(statuses, [200] * workers)  # share locks do not serialize ACTIVE writers
        self.assertEqual(self.table_count(Survey), workers)


    # Onboarding Step 2 dog experience (Issue #42)

    def start_step2_api(self):
        """User 1 with the current pledge and Step 1 done, on the real routers."""
        from datetime import date
        self.start_api()
        self.client.app.include_router(onboarding_experience.router)
        with self.Session.begin() as db:
            db.add(User(id=1, login_id="walker01", nickname="봉사자", password=UNUSABLE_PASSWORD))
            db.flush()
            db.add(UserPledgeAcknowledgement(user_id=1, pledge_version=PLEDGE_VERSION, acknowledged_at=STEP1_START))
            db.add(VolunteerOnboardingBasic(user_id=1, full_name="김민지", birth_date=date(2002, 12, 10),
                                            phone_e164="+821012345678", phone_verified_at=STEP1_START,
                                            completed_at=STEP1_START, updated_at=STEP1_START))
        return self.add_session(1, "step2-token")

    def put_step2(self, headers, body=None):
        return self.client.put("/users/me/onboarding/dog-experience", json=body or STEP2_FULL, headers=headers)

    def step2_rows(self):
        return (self.table_count(VolunteerOnboardingExperience), self.table_count(VolunteerExperienceChallenge))

    def test_step2_save_that_locked_first_commits_before_the_status_change(self):
        headers = self.start_step2_api()
        entered, release = self.hold("app.dog_experience_service.first_incomplete_step")  # runs under the share lock
        results = {}
        save = self.run_in_thread(results, "save", lambda: self.put_step2(headers))
        self.assertTrue(entered.wait(10))

        def change():
            with self.Session() as db:
                change_to_age_review(db, 1, self.now)
            return "done"

        status_change = self.run_in_thread(results, "change", change)
        self.wait_for_lock_waiter()
        self.assertEqual(self.account_status(1), "ACTIVE")
        release.set()
        save.join(10)
        status_change.join(10)
        self.assertEqual(results["save"].status_code, 200, results["save"].text)
        self.assertEqual(self.account_status(1), "AGE_REVIEW_REQUIRED")
        self.assertEqual(self.step2_rows(), (1, 1))

    def test_step2_save_after_the_status_change_is_refused_without_rows(self):
        headers = self.start_step2_api()
        response = self.change_status_while(lambda: self.put_step2(headers))
        self.assertEqual((response.status_code, response.json()["code"]), (403, "AGE_REVIEW_REQUIRED"))
        self.assertEqual(self.step2_rows(), (0, 0))

    def test_parallel_identical_step2_saves_keep_one_answer_and_one_time(self):
        headers = self.start_step2_api()
        workers = 4
        barrier = Barrier(workers)
        original = sys.modules["app.dog_experience_service"].first_incomplete_step
        waited = local()

        def synchronized(*args, **kwargs):
            # Only the precondition check waits; the next step of the answer (#45) calls it again.
            if not getattr(waited, "done", False):
                waited.done = True
                barrier.wait(10)  # every request saw "not completed" before any insert
            return original(*args, **kwargs)

        with patch("app.dog_experience_service.first_incomplete_step", side_effect=synchronized), \
                ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(lambda _: self.put_step2(headers), range(workers)))
        self.assertEqual([r.status_code for r in responses], [200] * workers, [r.text for r in responses])
        self.assertEqual(len({r.json()["data"]["completed_at"] for r in responses}), 1)
        self.assertEqual(self.step2_rows(), (1, 1))

    def test_parallel_different_step2_saves_one_wins_the_other_gets_409(self):
        headers = self.start_step2_api()
        barrier = Barrier(2)
        original = sys.modules["app.dog_experience_service"].first_incomplete_step
        waited = local()

        def synchronized(*args, **kwargs):
            if not getattr(waited, "done", False):  # the precondition check only (see above)
                waited.done = True
                barrier.wait(10)
            return original(*args, **kwargs)

        bodies = [STEP2_FULL, {"has_experience": False}]
        with patch("app.dog_experience_service.first_incomplete_step", side_effect=synchronized), \
                ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda body: self.put_step2(headers, body), bodies))
        outcomes = sorted((r.status_code, r.json()["code"]) for r in responses)
        self.assertEqual(outcomes, [(200, "OK"), (409, "DOG_EXPERIENCE_ALREADY_COMPLETED")])
        winner = bodies[[r.status_code for r in responses].index(200)]
        with self.Session() as db:
            stored = db.get(VolunteerOnboardingExperience, 1)
            self.assertEqual(stored.has_experience, winner["has_experience"])
            self.assertEqual(db.query(VolunteerExperienceChallenge).count(), len(winner.get("challenge_codes", [])))

    def test_step2_challenge_failure_rolls_back_on_postgres(self):
        headers = self.start_step2_api()

        def broken_row(user_id, challenge_code):
            return VolunteerExperienceChallenge(user_id=user_id, challenge_code="BROKEN")

        with patch("app.dog_experience_service.VolunteerExperienceChallenge", side_effect=broken_row), \
                self.assertLogs("app.api_contract", "ERROR"):
            response = self.put_step2(headers)
        self.assertEqual((response.status_code, response.json()["code"]), (500, "INTERNAL_SERVER_ERROR"))
        self.assertEqual(self.step2_rows(), (0, 0))
        self.assertEqual(self.put_step2(headers).status_code, 200)

    def test_step2_database_checks_on_postgres(self):
        self.start_step2_api()
        bad_rows = [dict(has_experience=True, experienced_breeds_text="푸들", dog_size_code=None,
                         walking_frequency="DAILY", perceived_difficulty="EASY"),
                    dict(has_experience=False, experienced_breeds_text=None, dog_size_code="SMALL",
                         walking_frequency=None, perceived_difficulty=None)]
        for values in bad_rows:
            with self.subTest(values=values), self.assertRaises(IntegrityError), self.Session.begin() as db:
                db.add(VolunteerOnboardingExperience(user_id=1, completed_at=STEP1_START, updated_at=STEP1_START,
                                                     **values))
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingExperience(user_id=1, has_experience=False, completed_at=STEP1_START,
                                                 updated_at=STEP1_START))
        for code in ("BITING",):
            with self.assertRaises(IntegrityError), self.Session.begin() as db:
                db.add(VolunteerExperienceChallenge(user_id=1, challenge_code=code))

    def test_migration_025_adds_only_step2_tables_and_step2_works_without_create_all(self):
        self.migrate_dev_chain_through_016_with_accounts()
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
        for migration in (GUIDE_MIGRATION, PLEDGE_MIGRATION, STEP1_MIGRATION):
            self.run_migration(migration)
        accounts = self.user_rows()
        before_tables, before_indexes, before_constraints = self.schema_snapshot()
        self.run_migration(STEP2_MIGRATION)
        tables, indexes, constraints = self.schema_snapshot()
        new_tables = {"volunteer_onboarding_experiences", "volunteer_experience_challenges"}
        self.assertEqual(set(tables) - set(before_tables), new_tables)
        self.assertEqual({k: v for k, v in tables.items() if k not in new_tables}, before_tables)
        self.assertEqual([i for i in indexes if i[0] not in new_tables], before_indexes)
        self.assertEqual([c for c in constraints if c[0] not in new_tables], before_constraints)
        self.assertIn(("volunteer_onboarding_experiences", "fk_onboarding_experiences_user_id",
                       "FOREIGN KEY (user_id) REFERENCES users(id)"), constraints)
        self.assertIn(("volunteer_experience_challenges", "fk_experience_challenges_experience",
                       "FOREIGN KEY (user_id) REFERENCES volunteer_onboarding_experiences(user_id)"), constraints)
        self.assertEqual(self.user_rows(), accounts)
        self.run_migration(STEP2_MIGRATION)  # re-running is a no-op
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        # The Step 2 API runs on the migrated tables with no create_all().
        from datetime import date
        self.migrate_users_for_current_models(after=STEP2_MIGRATION)
        self.start_api(create_schema=False)
        self.client.app.include_router(onboarding_experience.router)
        with self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=1, pledge_version=PLEDGE_VERSION, acknowledged_at=STEP1_START))
            db.add(VolunteerOnboardingBasic(user_id=1, full_name="김민지", birth_date=date(2002, 12, 10),
                                            phone_e164="+821099998888", phone_verified_at=STEP1_START,
                                            completed_at=STEP1_START, updated_at=STEP1_START))
        login = self.client.post("/auth/login", json={"identifier": "walker_one", "password": PASSWORD})
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        self.assertEqual(self.put_step2(headers).status_code, 200)
        self.assertEqual(self.put_step2(headers, {"has_experience": False}).json()["code"],
                         "DOG_EXPERIENCE_ALREADY_COMPLETED")
        self.assertEqual(self.step2_rows(), (1, 1))

    def test_exit_removes_step2_rows_in_fk_order_and_rolls_back_on_failure(self):
        headers = self.setup_age_review()  # account 1 in AGE_REVIEW_REQUIRED, full schema
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingExperience(user_id=1, has_experience=True, experienced_breeds_text="푸들",
                                                 dog_size_code="SMALL", walking_frequency="DAILY",
                                                 perceived_difficulty="EASY", completed_at=STEP1_START,
                                                 updated_at=STEP1_START))
            db.flush()
            db.add(VolunteerExperienceChallenge(user_id=1, challenge_code="NONE"))
        # A failure after the Step 2 rows were deleted rolls them back with everything else.
        with patch("app.age_review_service.has_retained_activity", return_value=False),                 patch.object(sys.modules["sqlalchemy.orm"].Session, "commit",
                             side_effect=RuntimeError("commit failed")),                 self.assertLogs("app.api_contract", "ERROR"):
            failed = self.exit_account(headers)
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(self.step2_rows(), (1, 1))
        self.assertEqual(self.remaining()["users"], 1)
        response = self.exit_account(headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.step2_rows(), (0, 0))
        self.assertEqual(self.remaining(), {"users": 0, "auth_sessions": 0, "user_consents": 0,
                                            "user_pledge_acknowledgements": 0})


    # Onboarding Step 3 walking routine (Issue #44)

    def add_step3_regions(self):
        with self.Session.begin() as db:
            for code, name, level, parent in STEP3_REGIONS:
                db.add(Region(region_code=code, region_name=name, region_level=level, parent_region_code=parent,
                              is_active=True))
                db.flush()

    def add_step3_ready_account(self, user_id, login_id=None):
        """An ACTIVE user with the current pledge, Step 1 and Step 2 done (Step 2: no experience)."""
        from datetime import date
        with self.Session.begin() as db:
            if login_id is not None:
                db.add(User(id=user_id, login_id=login_id, nickname=f"봉사자{user_id}", password=UNUSABLE_PASSWORD))
                db.flush()
            db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION, acknowledged_at=STEP1_START))
            db.add(VolunteerOnboardingBasic(user_id=user_id, full_name="김민지", birth_date=date(2002, 12, 10),
                                            phone_e164=f"+8210555{user_id:05d}", phone_verified_at=STEP1_START,
                                            completed_at=STEP1_START, updated_at=STEP1_START))
            db.flush()
            db.add(VolunteerOnboardingExperience(user_id=user_id, has_experience=False, completed_at=STEP1_START,
                                                 updated_at=STEP1_START))

    def start_step3_api(self):
        """User 1 ready for Step 3 on the real routers, with a synthetic region master."""
        self.start_api()
        self.client.app.include_router(onboarding_routine.router)
        self.add_step3_regions()
        self.add_step3_ready_account(1, "walker01")
        return self.add_session(1, "step3-token")

    def put_step3(self, headers, body=None):
        return self.client.put("/users/me/onboarding/walking-routine", json=body or STEP3_FULL, headers=headers)

    def step3_rows(self):
        return (self.table_count(VolunteerOnboardingRoutine), self.table_count(VolunteerRoutineRegion),
                self.table_count(VolunteerRoutineTimeSlot))

    def concurrent_step3_inserts(self, workers):
        """Every request has seen "no routine" before any of them inserts, so all but one
        must take the primary key conflict branch. Returns (context, replays): `replays`
        records each re-read answer, which only that branch produces here."""
        from contextlib import ExitStack
        service = sys.modules["app.walking_routine_service"]
        barrier = Barrier(workers)
        original_check, original_replay = service._completed_routine, service._replay
        replays = []

        def checked_then_wait(*args, **kwargs):
            existing = original_check(*args, **kwargs)
            barrier.wait(10)
            return existing

        def counted_replay(*args, **kwargs):
            replays.append("replay")
            return original_replay(*args, **kwargs)

        stack = ExitStack()
        stack.enter_context(patch("app.walking_routine_service._completed_routine", side_effect=checked_then_wait))
        stack.enter_context(patch("app.walking_routine_service._replay", side_effect=counted_replay))
        return stack, replays

    def test_step3_save_that_locked_first_commits_before_the_status_change(self):
        headers = self.start_step3_api()
        entered, release = self.hold("app.walking_routine_service._require_regions")  # under the users share lock
        results = {}
        save = self.run_in_thread(results, "save", lambda: self.put_step3(headers))
        self.assertTrue(entered.wait(10))

        def change():
            with self.Session() as db:
                change_to_age_review(db, 1, self.now)
            return "done"

        status_change = self.run_in_thread(results, "change", change)
        self.wait_for_lock_waiter()
        self.assertEqual(self.account_status(1), "ACTIVE")
        release.set()
        save.join(10)
        status_change.join(10)
        self.assertEqual(results["save"].status_code, 200, results["save"].text)
        self.assertEqual(self.account_status(1), "AGE_REVIEW_REQUIRED")
        self.assertEqual(self.step3_rows(), (1, 2, 2))

    def test_step3_save_after_the_status_change_is_refused_without_rows(self):
        headers = self.start_step3_api()
        response = self.change_status_while(lambda: self.put_step3(headers))
        self.assertEqual((response.status_code, response.json()["code"]), (403, "ONBOARDING_NOT_ALLOWED"))
        self.assertEqual(self.step3_rows(), (0, 0, 0))

    def test_parallel_identical_step3_saves_keep_one_routine_and_one_time(self):
        headers = self.start_step3_api()
        workers = 4
        inserts, replays = self.concurrent_step3_inserts(workers)
        with inserts, ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(lambda _: self.put_step3(headers), range(workers)))
        self.assertEqual([r.status_code for r in responses], [200] * workers, [r.text for r in responses])
        self.assertEqual(len(replays), workers - 1)  # the losers recovered from the primary key conflict
        self.assertEqual(len({r.json()["data"]["completed_at"] for r in responses}), 1)
        self.assertEqual({r.json()["data"]["next_onboarding_step"] for r in responses}, {None})
        self.assertEqual(self.step3_rows(), (1, 2, 2))  # no duplicate link rows
        with self.Session() as db:
            consents = db.query(UserConsent).filter_by(user_id=1).all()
            self.assertEqual(len(consents), 2)
            self.assertEqual({(row.consent_type, row.document_version, row.action) for row in consents},
                             {("TERMS_OF_SERVICE", TEST_TERMS_VERSION, "AGREE"),
                              ("PRIVACY_COLLECTION_USE", TEST_DOCUMENT_VERSION, "AGREE")})

    def test_parallel_different_step3_saves_one_wins_the_other_gets_409(self):
        headers = self.start_step3_api()
        bodies = [STEP3_FULL, {**STEP3_FULL, "region_codes": ["36_SPECIAL_ALL"],
                               "available_time_slot_codes": list(TIME_SLOT_CODES)}]
        inserts, replays = self.concurrent_step3_inserts(2)
        with inserts, ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda body: self.put_step3(headers, body), bodies))
        outcomes = sorted((r.status_code, r.json()["code"]) for r in responses)
        self.assertEqual(outcomes, [(200, "OK"), (409, "WALKING_ROUTINE_ALREADY_COMPLETED")])
        self.assertEqual(len(replays), 1)  # the 409 came from the primary key conflict branch
        winner = bodies[[r.status_code for r in responses].index(200)]
        with self.Session() as db:
            self.assertEqual(sorted(r.region_code for r in db.query(VolunteerRoutineRegion)),
                             sorted(winner["region_codes"]))
            self.assertEqual(db.query(VolunteerRoutineTimeSlot).count(), len(winner["available_time_slot_codes"]))

    def test_step3_link_failure_rolls_back_on_postgres(self):
        headers = self.start_step3_api()

        def broken_slot(user_id, time_slot_code):
            return VolunteerRoutineTimeSlot(user_id=user_id, time_slot_code="BROKEN")

        with patch("app.walking_routine_service.VolunteerRoutineTimeSlot", side_effect=broken_slot), \
                self.assertLogs("app.api_contract", "ERROR"):
            response = self.put_step3(headers)
        self.assertEqual((response.status_code, response.json()["code"]), (500, "INTERNAL_SERVER_ERROR"))
        self.assertEqual(self.step3_rows(), (0, 0, 0))  # the routine and region rows were rolled back too
        self.assertEqual(self.table_count(UserConsent), 0)
        self.assertEqual(self.put_step3(headers).status_code, 200)

    def test_step3_consent_insert_failure_rolls_back_routine_and_preserves_signup_history(self):
        headers = self.start_step3_api()
        with self.Session.begin() as db:
            db.add(UserConsent(user_id=1, consent_type="PRIVACY_COLLECTION_USE",
                               document_version="old-privacy", action="AGREE", recorded_at=STEP1_START))
        with self.engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE user_consents ADD CONSTRAINT test_refuse_new_privacy "
                "CHECK (consent_type <> 'PRIVACY_COLLECTION_USE' OR document_version = 'old-privacy')"))
        with self.assertLogs("app.api_contract", "ERROR"):
            response = self.put_step3(headers)
        self.assertEqual((response.status_code, response.json()["code"]), (500, "INTERNAL_SERVER_ERROR"))
        self.assertEqual(self.step3_rows(), (0, 0, 0))
        with self.Session() as db:
            (record,) = db.query(UserConsent).filter_by(user_id=1).all()
            self.assertEqual((record.consent_type, record.document_version, record.action, record.recorded_at),
                             ("PRIVACY_COLLECTION_USE", "old-privacy", "AGREE", STEP1_START))

    def test_region_deactivation_waits_for_a_step3_save_that_locked_the_region(self):
        headers = self.start_step3_api()
        locked, release = Event(), Event()
        original = sys.modules["app.walking_routine_service"].require_selectable_regions

        def lock_then_wait(*args, **kwargs):
            result = original(*args, **kwargs)  # the chosen regions are now FOR SHARE
            locked.set()
            release.wait(10)
            return result

        results = {}
        with patch("app.walking_routine_service.require_selectable_regions", side_effect=lock_then_wait):
            save = self.run_in_thread(results, "save", lambda: self.put_step3(headers))
            self.assertTrue(locked.wait(10))

            def deactivate():
                with self.engine.begin() as connection:  # a region sync marks a chosen code abolished
                    connection.execute(text("UPDATE regions SET is_active = false WHERE region_code = '26350'"))
                return "done"

            sync = self.run_in_thread(results, "sync", deactivate)
            self.wait_for_lock_waiter()
            release.set()
            save.join(10)
            sync.join(10)
        self.assertEqual(results["save"].status_code, 200, results["save"].text)
        self.assertEqual(results["sync"], "done")
        self.assertEqual(self.step3_rows(), (1, 2, 2))
        # After the deactivation committed, a new save with that code is refused.
        self.add_step3_ready_account(2, "walker02")
        other = self.add_session(2, "step3-token-2")
        response = self.put_step3(other)
        self.assertEqual((response.status_code, response.json()["code"]), (422, "INVALID_REGION_CODE"))
        with self.Session() as db:
            self.assertIsNone(db.get(VolunteerOnboardingRoutine, 2))

    def test_step3_database_checks_on_postgres(self):
        self.start_step3_api()
        for values in ({"walking_duration_code": "UNDER_30_MIN"}, {"comfortable_dog_size_code": "HUGE"}):
            row = {"walking_duration_code": "UP_TO_30_MIN", "comfortable_dog_size_code": "SMALL", **values}
            with self.subTest(values=values), self.assertRaises(IntegrityError), self.Session.begin() as db:
                db.add(VolunteerOnboardingRoutine(user_id=1, completed_at=STEP1_START, updated_at=STEP1_START, **row))
        with self.assertRaises(IntegrityError), self.Session.begin() as db:  # link rows need the routine
            db.add(VolunteerRoutineTimeSlot(user_id=1, time_slot_code="WEEKDAY_MORNING"))
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingRoutine(user_id=1, walking_duration_code="OVER_60_MIN",
                                              comfortable_dog_size_code="LARGE", completed_at=STEP1_START,
                                              updated_at=STEP1_START))
        for model, values in ((VolunteerRoutineTimeSlot, {"time_slot_code": "WEEKDAY_EVENING"}),
                              (VolunteerRoutineRegion, {"region_code": "99999"})):  # unknown region: FK
            with self.subTest(values=values), self.assertRaises(IntegrityError), self.Session.begin() as db:
                db.add(model(user_id=1, **values))
        with self.Session.begin() as db:
            db.add(VolunteerRoutineRegion(user_id=1, region_code="11680"))
        with self.assertRaises(IntegrityError), self.Session.begin() as db:  # composite primary key
            db.add(VolunteerRoutineRegion(user_id=1, region_code="11680"))

    def test_migration_026_adds_only_step3_tables_and_step3_works_without_create_all(self):
        self.migrate_dev_chain_through_016_with_accounts()
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
        for migration in (GUIDE_MIGRATION, PLEDGE_MIGRATION, STEP1_MIGRATION, STEP2_MIGRATION, REGION_MIGRATION):
            self.run_migration(migration)
        accounts = self.user_rows()
        before_tables, before_indexes, before_constraints = self.schema_snapshot()
        self.run_migration(STEP3_MIGRATION)
        tables, indexes, constraints = self.schema_snapshot()
        new_tables = {"volunteer_onboarding_routines", "volunteer_routine_regions", "volunteer_routine_time_slots"}
        self.assertEqual(set(tables) - set(before_tables), new_tables)
        self.assertEqual({k: v for k, v in tables.items() if k not in new_tables}, before_tables)
        self.assertEqual([i for i in indexes if i[0] not in new_tables], before_indexes)
        self.assertEqual([c for c in constraints if c[0] not in new_tables], before_constraints)
        for expected in (
            ("volunteer_onboarding_routines", "fk_onboarding_routines_user_id", "FOREIGN KEY (user_id) REFERENCES users(id)"),
            ("volunteer_routine_regions", "fk_routine_regions_routine",
             "FOREIGN KEY (user_id) REFERENCES volunteer_onboarding_routines(user_id)"),
            ("volunteer_routine_regions", "fk_routine_regions_region_code",
             "FOREIGN KEY (region_code) REFERENCES regions(region_code)"),
            ("volunteer_routine_time_slots", "fk_routine_time_slots_routine",
             "FOREIGN KEY (user_id) REFERENCES volunteer_onboarding_routines(user_id)"),
            ("volunteer_routine_regions", "pk_volunteer_routine_regions", "PRIMARY KEY (user_id, region_code)"),
            ("volunteer_routine_time_slots", "pk_volunteer_routine_time_slots", "PRIMARY KEY (user_id, time_slot_code)"),
        ):
            self.assertIn(expected, constraints)
        # Check the historical projection without the columns added by 027.
        for model in (VolunteerOnboardingRoutine, VolunteerRoutineRegion, VolunteerRoutineTimeSlot):
            expected_columns = set(model.__table__.columns.keys())
            if model is VolunteerOnboardingRoutine:
                expected_columns -= {"terms_document_version", "privacy_document_version"}
            self.assertEqual({column for column, _, _ in tables[model.__tablename__]},
                             expected_columns)
        self.assertEqual(self.user_rows(), accounts)
        self.run_migration(STEP3_MIGRATION)  # re-running is a no-op
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        # The CHECKs of the SQL file accept exactly the codes the API accepts.
        with self.engine.connect() as connection:
            def accepted(table, column, value):
                transaction = connection.begin()
                try:
                    if table == "volunteer_onboarding_routines":
                        row = {"walking_duration_code": "UP_TO_30_MIN", "comfortable_dog_size_code": "SMALL",
                               column: value}
                        connection.execute(text(
                            "INSERT INTO volunteer_onboarding_routines VALUES"
                            " (1, :walking_duration_code, :comfortable_dog_size_code, now(), now())"), row)
                    else:
                        connection.execute(text(
                            "INSERT INTO volunteer_onboarding_routines VALUES (1, 'UP_TO_30_MIN', 'SMALL', now(), now())"))
                        connection.execute(text(f"INSERT INTO {table} VALUES (1, :value)"), {"value": value})
                    return True
                except IntegrityError:
                    return False
                finally:
                    transaction.rollback()

            for column, codes in (("walking_duration_code", WALKING_DURATION_CODES),
                                  ("comfortable_dog_size_code", DOG_SIZE_CODES)):
                for code in codes:
                    self.assertTrue(accepted("volunteer_onboarding_routines", column, code), code)
            for code in TIME_SLOT_CODES:
                self.assertTrue(accepted("volunteer_routine_time_slots", "time_slot_code", code), code)
            for table, column, code in (("volunteer_onboarding_routines", "walking_duration_code", "UNDER_30_MIN"),
                                        ("volunteer_onboarding_routines", "comfortable_dog_size_code", "XLARGE"),
                                        ("volunteer_routine_time_slots", "time_slot_code", "WEEKDAY_EVENING")):
                self.assertFalse(accepted(table, column, code), code)
        self.assertEqual(self.step3_rows_raw(), (0, 0, 0))
        # The Step 3 API runs on the migrated tables with no create_all().
        self.migrate_users_for_current_models(after=STEP3_MIGRATION)
        self.start_api(create_schema=False)
        self.client.app.include_router(onboarding_routine.router)
        self.add_step3_regions()
        self.add_step3_ready_account(1)
        login = self.client.post("/auth/login", json={"identifier": "walker_one", "password": PASSWORD})
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        self.assertEqual(self.put_step3(headers).status_code, 200)
        read = self.client.get("/users/me/onboarding/walking-routine", headers=headers).json()["data"]
        self.assertEqual((read["step3_completed"], read["routine"]["walking_duration_code"],
                          read["next_onboarding_step"]), (True, "UP_TO_30_MIN", None))
        changed = self.put_step3(headers, {**STEP3_FULL, "walking_duration_code": "OVER_60_MIN"})
        self.assertEqual(changed.json()["code"], "WALKING_ROUTINE_ALREADY_COMPLETED")
        self.assertEqual(self.step3_rows(), (1, 2, 2))

    def test_migration_027_preserves_legacy_routines_and_consent_history_and_accepts_terms(self):
        self.migrate_dev_chain_through_016_with_accounts()
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
        for name in API_MIGRATIONS_AFTER_018[:-1]:
            self.run_migration(name)
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO volunteer_onboarding_routines "
                "(user_id, walking_duration_code, comfortable_dog_size_code, completed_at, updated_at) "
                "VALUES (1, 'UP_TO_30_MIN', 'SMALL', :time, :time)"), {"time": STEP1_START})
            connection.execute(text(
                "INSERT INTO user_consents (user_id, consent_type, document_version, action, recorded_at) "
                "VALUES (1, 'PRIVACY_COLLECTION_USE', :version, 'AGREE', :time)"),
                {"version": TEST_DOCUMENT_VERSION, "time": STEP1_START})
        self.run_migration(AGREEMENTS_MIGRATION)
        snapshot = self.schema_snapshot()
        self.run_migration(AGREEMENTS_MIGRATION)
        self.assertEqual(self.schema_snapshot(), snapshot)
        session_factory = sessionmaker(bind=self.engine)
        with session_factory.begin() as db:
            legacy = db.get(VolunteerOnboardingRoutine, 1)
            self.assertEqual((legacy.completed_at, legacy.terms_document_version, legacy.privacy_document_version),
                             (STEP1_START, None, None))
            (privacy_consent,) = db.query(UserConsent).filter_by(user_id=1).all()
            self.assertEqual((privacy_consent.consent_type, privacy_consent.document_version,
                              privacy_consent.action, privacy_consent.recorded_at),
                             ("PRIVACY_COLLECTION_USE", TEST_DOCUMENT_VERSION, "AGREE", STEP1_START))
            db.add(UserConsent(user_id=1, consent_type="TERMS_OF_SERVICE",
                               document_version=TEST_TERMS_VERSION, action="AGREE", recorded_at=STEP1_START))
        with session_factory() as db:
            self.assertEqual(db.query(UserConsent).count(), 2)
        with self.assertRaises(IntegrityError), session_factory.begin() as db:
            db.add(UserConsent(user_id=1, consent_type="UNSUPPORTED_TYPE",
                               document_version=TEST_TERMS_VERSION, action="AGREE", recorded_at=STEP1_START))

    def step3_rows_raw(self):
        with self.engine.connect() as connection:
            return tuple(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                         for table in ("volunteer_onboarding_routines", "volunteer_routine_regions",
                                       "volunteer_routine_time_slots"))

    def test_exit_removes_step3_rows_in_fk_order_and_rolls_back_on_failure(self):
        headers = self.setup_age_review()  # account 1 in AGE_REVIEW_REQUIRED, full schema
        self.add_step3_regions()
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingRoutine(user_id=1, walking_duration_code="UP_TO_30_MIN",
                                              comfortable_dog_size_code="SMALL", completed_at=STEP1_START,
                                              updated_at=STEP1_START))
            db.flush()
            db.add(VolunteerRoutineRegion(user_id=1, region_code="36_SPECIAL_ALL"))
            db.add(VolunteerRoutineTimeSlot(user_id=1, time_slot_code="WEEKEND_AFTERNOON"))
        with patch("app.age_review_service.has_retained_activity", return_value=False), \
                patch.object(sys.modules["sqlalchemy.orm"].Session, "commit",
                             side_effect=RuntimeError("commit failed")), \
                self.assertLogs("app.api_contract", "ERROR"):
            failed = self.exit_account(headers)
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(self.step3_rows(), (1, 1, 1))
        response = self.exit_account(headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.step3_rows(), (0, 0, 0))
        self.assertEqual(self.table_count(Region), len(STEP3_REGIONS))  # the shared master stays
        self.assertEqual(self.remaining(), {"users": 0, "auth_sessions": 0, "user_consents": 0,
                                            "user_pledge_acknowledgements": 0})


    # GET /users/me onboarding progress (Issue #45)

    def me(self, headers):
        response = self.client.get("/users/me", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["data"]

    def test_users_me_reads_with_three_selects_and_never_writes_on_postgres(self):
        from sqlalchemy import event
        headers = self.start_step3_api()  # pledge, Step 1 and Step 2 done
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(self.engine, "before_cursor_execute", record)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record)
        for stage in ("before Step 3", "after Step 3"):
            statements.clear()
            data = self.me(headers)
            with self.subTest(stage=stage):
                # Session, account row and one progress snapshot. No INSERT, UPDATE or DELETE.
                self.assertEqual(statements, ["SELECT"] * 3)
            if stage == "before Step 3":
                self.assertEqual((data["step3_completed"], data["next_onboarding_step"]), (False, "WALKING_ROUTINE"))
                event.remove(self.engine, "before_cursor_execute", record)
                self.assertEqual(self.put_step3(headers).json()["data"]["next_onboarding_step"], None)
                event.listen(self.engine, "before_cursor_execute", record)
        self.assertEqual((data["step3_completed"], data["onboarding_completed"], data["next_onboarding_step"]),
                         (True, True, None))

    def test_users_me_takes_status_and_steps_from_the_progress_query_not_the_authenticated_row(self):
        headers = self.start_step3_api()
        authenticated, release = Event(), Event()
        original = sys.modules["app.onboarding_common"].load_progress

        def after_authentication(*args, **kwargs):
            if not authenticated.is_set():  # the reader only; the Step 3 save below runs normally
                authenticated.set()  # the account row was read as ACTIVE without Step 3
                release.wait(10)
            return original(*args, **kwargs)

        results = {}
        with patch("app.onboarding_common.load_progress", side_effect=after_authentication):
            reader = self.run_in_thread(results, "me", lambda: self.client.get("/users/me", headers=headers))
            self.assertTrue(authenticated.wait(10))
            # Another session commits Step 3 and then a status change before the progress query.
            self.assertEqual(self.put_step3(headers).status_code, 200)
            with self.Session() as db:
                change_to_age_review(db, 1, self.now)
            release.set()
            reader.join(10)
        data = results["me"].json()["data"]
        # The status comes from the progress statement, not the row read at authentication. That
        # statement reads status and records together (one statement is shown by the SELECT count test).
        self.assertEqual((data["account_status"], data["step3_completed"], data["onboarding_completed"],
                          data["next_onboarding_step"]), ("AGE_REVIEW_REQUIRED", True, False, "AGE_REVIEW"))

    def test_parallel_users_me_requests_return_the_same_state(self):
        headers = self.start_step3_api()
        workers = 8
        barrier = Barrier(workers)

        def read(_):
            barrier.wait(10)
            return self.client.get("/users/me", headers=headers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(read, range(workers)))
        self.assertEqual({r.status_code for r in responses}, {200})
        self.assertEqual(len({json.dumps(r.json()["data"], sort_keys=True) for r in responses}), 1)
        self.assertEqual(responses[0].json()["data"]["next_onboarding_step"], "WALKING_ROUTINE")


if __name__ == "__main__":
    unittest.main()
