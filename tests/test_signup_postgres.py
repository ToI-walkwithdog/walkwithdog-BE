"""Sign-up uniqueness, concurrency and migration 014 on real PostgreSQL.

SIGNUP_TEST_DATABASE_URL must name an EMPTY local toi_walk_test_* database.
Each test works in its own new schema; no existing DB is read or changed.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import signup_service
from app.auth import UNUSABLE_PASSWORD
from app.database import Base, get_db
from app.models import User
from app.routers import users
from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "migrations" / "014_onboarding_signup.sql").read_text(encoding="utf-8")
PASSWORD = "WalkTogether2026!"
INDEXES = {"uq_users_login_id_lower": "lower((login_id)::text)",
           "uq_users_nickname_lower": "lower((nickname)::text)",
           "uq_users_email_lower": "lower((email)::text)"}


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

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        self.schema = "su_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={self.schema}"})
        self.addCleanup(self.engine.dispose)

    def start_api(self):
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(users.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def body(self, suffix, **changes):
        return {"login_id": f"user_{suffix}", "nickname": f"nick_{suffix}", "email": f"{suffix}@example.com",
                "password": PASSWORD, **changes}

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

    def create_pre_014_tables(self):
        """Users as created before migration 014, plus a referencing table."""
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE shelters (id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL)")
            connection.exec_driver_sql(
                "CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL UNIQUE,"
                " nickname VARCHAR(50) NOT NULL, password VARCHAR(255),"
                " shelter_id INTEGER CONSTRAINT fk_users_shelter_id REFERENCES shelters(id),"
                " created_at TIMESTAMPTZ DEFAULT now())")
            connection.exec_driver_sql(
                "CREATE TABLE surveys (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id))")

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
            db.add(User(login_id="walker04", nickname="Walker", email="a@example.com", password=UNUSABLE_PASSWORD))
        for changes in ({"login_id": "WALKER04"}, {"nickname": "wALKER"}, {"email": "A@Example.com"}):
            with self.subTest(changes=changes):
                values = {"login_id": "other", "nickname": "other", "email": "b@example.com", **changes}
                with self.assertRaises(IntegrityError), self.Session.begin() as db:
                    db.add(User(**values, password=UNUSABLE_PASSWORD))
        self.assertEqual(self.count(), 1)

    def test_constraint_violation_maps_to_the_right_field(self):
        self.start_api()
        self.assertEqual(self.client.post("/users/", json=self.body("a", nickname="Walker_King")).status_code, 200)
        cases = (({"login_id": "USER_A"}, "LOGIN_ID_ALREADY_EXISTS", "login_id"),
                 ({"nickname": "walker_king"}, "NICKNAME_ALREADY_EXISTS", "nickname"),
                 ({"email": "A@EXAMPLE.COM"}, "EMAIL_ALREADY_EXISTS", "email"))
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
        for field, code in (("login_id", "LOGIN_ID_ALREADY_EXISTS"), ("nickname", "NICKNAME_ALREADY_EXISTS"),
                            ("email", "EMAIL_ALREADY_EXISTS")):
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

    def test_parallel_identical_signups_store_exactly_one_row(self):
        self.start_api()
        workers = 6
        barrier = Barrier(workers)
        original = signup_service.first_taken_field

        def synchronized(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(10)  # every request passes the pre-check before any insert
            return result

        bodies = [self.body(f"p{index}", login_id="same_login") for index in range(workers)]
        with patch("app.signup_service.first_taken_field", side_effect=synchronized), \
                ThreadPoolExecutor(max_workers=workers) as pool:
            responses = list(pool.map(lambda body: self.client.post("/users/", json=body), bodies))
        statuses = sorted(response.status_code for response in responses)
        self.assertEqual(statuses, [200] + [409] * (workers - 1))
        self.assertEqual({r.json()["code"] for r in responses if r.status_code == 409}, {"LOGIN_ID_ALREADY_EXISTS"})
        self.assertEqual(self.count(), 1)

    # Migration 014

    def test_migration_014_on_empty_users_matches_the_model(self):
        self.create_pre_014_tables()
        self.run_sql_file(MIGRATION)
        columns, indexes, survey_fk, user_fk = self.users_structure()
        self.assertEqual(columns["login_id"], ("VARCHAR(20)", False))
        self.assertEqual(columns["nickname"], ("VARCHAR(20)", False))
        self.assertEqual(columns["email"], ("VARCHAR(100)", False))
        self.assertEqual(columns["password"], ("VARCHAR(255)", False))
        for name, expression in INDEXES.items():
            with self.subTest(index=name):
                self.assertIn(f"({expression})", indexes[name])
        self.assertIn("users_email_key", indexes)
        self.assertEqual(survey_fk, [(["user_id"], "users")])
        self.assertEqual(user_fk, [(["shelter_id"], "shelters")])
        # Re-running on the migrated empty table is a no-op.
        self.run_sql_file(MIGRATION)

    def test_migration_014_aborts_without_changes_when_users_has_rows(self):
        self.create_pre_014_tables()
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


if __name__ == "__main__":
    unittest.main()
