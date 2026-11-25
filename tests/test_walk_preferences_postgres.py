"""Issue #105 on a real, isolated PostgreSQL: migration 036, the users foreign key with the
sign-up exit, and the users row lock that orders concurrent preference saves."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import test_onboarding_basic_info as step1_module
from test_onboarding_basic_info import START
from test_user_profile_postgres import IsolatedSchema
from test_walk_preferences import PreferenceTestCase
from app import age_review_service
from app.auth import UNUSABLE_PASSWORD
from app.database import Base
from app.models import User, VolunteerWalkPreference
import app.walk_preference_service as walk_preference_service

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "036_walk_preferences.sql"
WAIT = 8


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class WalkPreferenceSchemaPostgresTests(IsolatedSchema, unittest.TestCase):
    def run_migration(self, engine):
        connection = engine.raw_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

    def test_036_creates_the_table_on_existing_users_and_reruns_as_no_op(self):
        engine = self.isolated_engine()
        with engine.begin() as connection:  # the base users table as before 036, with a row
            connection.exec_driver_sql("CREATE TABLE users (id SERIAL PRIMARY KEY, nickname VARCHAR(20) NOT NULL)")
            connection.exec_driver_sql("INSERT INTO users (nickname) VALUES ('기존회원')")
        self.run_migration(engine)
        self.run_migration(engine)
        insert = text("INSERT INTO volunteer_walk_preferences VALUES (:user_id, :duration, :size, :version, now())")
        with engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT nickname FROM users").scalar(), "기존회원")
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM volunteer_walk_preferences").scalar(), 0)
            connection.execute(insert, {"user_id": 1, "duration": None, "size": "SMALL", "version": 1})
        for values in ({"user_id": 2, "duration": None, "size": None, "version": 1},  # no such user
                       {"user_id": 1, "duration": None, "size": "SMALL", "version": 1},  # one row per user
                       {"user_id": 1, "duration": "30", "size": None, "version": 1},
                       {"user_id": 1, "duration": None, "size": "TINY", "version": 1},
                       {"user_id": 1, "duration": None, "size": None, "version": 0}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), engine.begin() as connection:
                if values["user_id"] == 1 and values["size"] != "SMALL":
                    connection.exec_driver_sql("DELETE FROM volunteer_walk_preferences")
                connection.execute(insert, values)
        with self.assertRaises(IntegrityError), engine.begin() as connection:  # no ON DELETE CASCADE
            connection.exec_driver_sql("DELETE FROM users WHERE id = 1")

    def test_sign_up_exit_deletes_the_preferences_under_the_real_foreign_key(self):
        engine = self.isolated_engine()
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session.begin() as db:
            db.add(User(id=1, login_id="minor01", nickname="연령확인", password=UNUSABLE_PASSWORD,
                        account_status="AGE_REVIEW_REQUIRED", age_review_required_at=START))
            db.flush()
            db.add(VolunteerWalkPreference(user_id=1, duration_code="OVER_60_MIN", size_code="LARGE", version=1,
                                           updated_at=START - timedelta(hours=1)))
        with Session() as db:
            age_review_service.delete_account(db, 1)
        with Session() as db:
            self.assertEqual((db.query(User).count(), db.query(VolunteerWalkPreference).count()), (0, 0))


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class WalkPreferenceLockingPostgresTests(IsolatedSchema, PreferenceTestCase):
    def setUp(self):
        engine = self.isolated_engine()
        with patch.object(step1_module, "create_engine", lambda *args, **kwargs: engine):
            super().setUp()

    def race(self, first, second):
        """Hold the first request right after it read the preference row under the users lock;
        the second must queue on that lock and decide only after the first commits."""
        reached, release, original = Event(), Event(), walk_preference_service.current_preference

        def held(*args):
            row = original(*args)
            if not reached.is_set():
                reached.set()
                if not release.wait(WAIT):
                    raise AssertionError("the first request was not released")
            return row

        with patch.object(walk_preference_service, "current_preference", held), ThreadPoolExecutor(2) as pool:
            holder = pool.submit(first)
            try:
                self.assertTrue(reached.wait(WAIT), "the first request did not reach the held point")
                other = pool.submit(second)
                with self.assertRaises(TimeoutError):
                    other.result(timeout=0.5)  # queued on the users row lock
            finally:
                release.set()
            return holder.result(timeout=WAIT), other.result(timeout=WAIT)

    def test_two_first_saves_from_version_0_keep_only_one(self):
        first, second = self.race(lambda: self.patch_preferences({"expected_version": 0, "size_code": "SMALL"}),
                                  lambda: self.patch_preferences({"expected_version": 0, "size_code": "LARGE"}))
        self.assertEqual(first.status_code, 200, first.text)
        self.assert_error(second, 409, "PREFERENCE_VERSION_MISMATCH")
        self.assertEqual((self.preference_rows(), self.stored().version, self.stored().size_code), (1, 1, "SMALL"))

    def test_two_edits_from_the_same_version_save_only_once(self):
        self.data(self.patch_preferences({"expected_version": 0, "duration_code": "UP_TO_30_MIN"}))
        first, second = self.race(
            lambda: self.patch_preferences({"expected_version": 1, "duration_code": "OVER_60_MIN"}),
            lambda: self.patch_preferences({"expected_version": 1, "duration_code": "BETWEEN_30_AND_60_MIN"}))
        self.assertEqual(first.status_code, 200, first.text)
        self.assert_error(second, 409, "PREFERENCE_VERSION_MISMATCH")
        self.assertEqual((self.stored().version, self.stored().duration_code), (2, "OVER_60_MIN"))
