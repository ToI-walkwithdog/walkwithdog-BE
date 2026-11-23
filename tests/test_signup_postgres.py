"""Sign-up uniqueness, consent atomicity, concurrency and migrations 016 to 018 on real PostgreSQL.

SIGNUP_TEST_DATABASE_URL must name an EMPTY local toi_walk_test_* database.
Each test works in its own new su_<uuid> schema and drops only that schema
afterwards, so the same test DB can be reused; no other schema is touched.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

# app.database builds its engine from DATABASE_URL at import time. Pin it to an
# in-memory SQLite URL so this file imports on its own, never touches the dev DB
# named in DATABASE_URL or .env, and does not depend on another test importing app
# first. Every test overrides get_db with SIGNUP_TEST_DATABASE_URL.
with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import signup_service
    from app.auth import UNUSABLE_PASSWORD, hash_password
    from app.database import Base, get_db
    from app.models import User, UserConsent
    from app.routers import auth, users

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from privacy_test_support import TEST_DOCUMENT_VERSION, privacy, use_test_privacy_policy

from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
SCHEMA_NAME = re.compile(r"su_[0-9a-f]{32}")
ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "migrations" / "016_onboarding_signup.sql").read_text(encoding="utf-8")
EMAIL_REMOVAL = (ROOT / "migrations" / "017_remove_user_email.sql").read_text(encoding="utf-8")
CONSENT_MIGRATION = (ROOT / "migrations" / "018_user_consents.sql").read_text(encoding="utf-8")
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
        self.run_sql_file(EMAIL_REMOVAL)
        self.run_sql_file(CONSENT_MIGRATION)
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
        # The current code requires the consent schema on all user lookups.
        self.run_sql_file(CONSENT_MIGRATION)
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

        self.start_api(create_schema=False)
        response = self.client.post("/users/", json=self.body("consent"))
        self.assertEqual(response.status_code, 200, response.text)
        self.run_sql_file(CONSENT_MIGRATION)
        self.assertEqual(self.schema_snapshot(), (tables, indexes, constraints))
        with self.Session() as db:
            record = db.query(UserConsent).one()
            self.assertEqual(record.user_id, response.json()["data"]["id"])
            self.assertEqual(record.document_version, TEST_DOCUMENT_VERSION)

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


if __name__ == "__main__":
    unittest.main()
