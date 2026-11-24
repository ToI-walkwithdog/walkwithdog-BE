"""Issue #103 on a real, isolated PostgreSQL: migration 035, and the row locks that order
profile PATCH, Step 1 saves and phone changes of two accounts."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import test_onboarding_basic_info as step1_module
from test_onboarding_basic_info import OTHER_PHONE
from test_user_profile import THIRD_PHONE, ProfileTestCase
from scripts.prepare_demo_db import PreparationError, validate_local_engine
import app.basic_info_service as basic_info_service
import app.profile_service as profile_service

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "035_profile_phone_change.sql"
WAIT = 8


class IsolatedSchema:
    """A throwaway schema in an empty local test database, dropped after the test."""

    def isolated_engine(self):
        admin = create_engine(TEST_URL)
        self.addCleanup(admin.dispose)
        validate_local_engine(admin)
        if not admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("새 격리 PostgreSQL 테스트 DB가 필요합니다.")
        schema = "user_profile_" + uuid4().hex
        with admin.begin() as connection:
            if inspect(connection).get_table_names(schema="public"):
                raise PreparationError("공용 스키마에 테이블이 없는 테스트 DB가 필요합니다.")
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

        def remove_schema():
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')

        self.addCleanup(remove_schema)
        engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={schema} -clock_timeout=4000"})
        self.addCleanup(engine.dispose)
        return engine


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class ProfileMigrationPostgresTests(IsolatedSchema, unittest.TestCase):
    def run_migration(self, engine):
        connection = engine.raw_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

    def test_035_adds_defaults_and_purpose_check_and_reruns_as_no_op(self):
        engine = self.isolated_engine()
        with engine.begin() as connection:  # the two tables as they were before 035, with rows
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE phone_verifications (id UUID PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO users VALUES (1)")
            connection.execute(text("INSERT INTO phone_verifications VALUES (:id)"), {"id": uuid4()})
        self.run_migration(engine)
        self.run_migration(engine)
        with engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT profile_version FROM users").scalar(), 0)
            self.assertEqual(connection.exec_driver_sql("SELECT purpose FROM phone_verifications").scalar(),
                             "ONBOARDING")
            checks = connection.exec_driver_sql(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_phone_verifications_purpose'").scalar()
            self.assertEqual(checks, 1)
            connection.execute(text("INSERT INTO phone_verifications (id, purpose) VALUES (:id, 'PROFILE_PHONE_CHANGE')"),
                               {"id": uuid4()})
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.execute(text("INSERT INTO phone_verifications (id, purpose) VALUES (:id, 'OTHER')"),
                               {"id": uuid4()})
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO users (id, profile_version) VALUES (2, NULL)")


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class ProfileLockingPostgresTests(IsolatedSchema, ProfileTestCase):
    def setUp(self):
        engine = self.isolated_engine()
        with patch.object(step1_module, "create_engine", lambda *args, **kwargs: engine):
            super().setUp()

    def hold(self, module, name):
        """Patch module.name so its first call waits (inside the caller's transaction) until released."""
        reached, release, original = Event(), Event(), getattr(module, name)

        def held(*args, **kwargs):
            result = original(*args, **kwargs)
            if not reached.is_set():
                reached.set()
                if not release.wait(WAIT):
                    raise AssertionError(f"{name} was not released")
            return result

        patcher = patch.object(module, name, held)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reached, release

    def race(self, held_module, held_name, first, second, second_waits=True):
        reached, release = self.hold(held_module, held_name)
        with ThreadPoolExecutor(2) as pool:
            holder = pool.submit(first)
            try:
                self.assertTrue(reached.wait(WAIT), "the first request did not reach the held point")
                other = pool.submit(second)
                if second_waits:  # it is queued on a row lock, not finished
                    with self.assertRaises(TimeoutError):
                        other.result(timeout=0.5)
                else:
                    other.result(timeout=WAIT)
            finally:
                release.set()
            return holder.result(timeout=WAIT), other.result(timeout=WAIT)

    def test_two_edits_from_the_same_version_save_only_once(self):
        self.complete_step1()
        first, second = self.race(
            profile_service, "nickname_used_by_other",
            lambda: self.patch_profile({"expected_profile_version": 1, "nickname": "먼저저장"}),
            lambda: self.patch_profile({"expected_profile_version": 1, "nickname": "나중저장"}))
        self.assertEqual(first.status_code, 200, first.text)
        self.assert_error(second, 409, "PROFILE_VERSION_MISMATCH")
        self.assertEqual((self.user().nickname, self.version()), ("먼저저장", 2))

    def test_two_accounts_moving_to_one_number_keep_only_the_first_commit(self):
        self.complete_step1(user_id=1)
        self.complete_step1(phone=THIRD_PHONE, user_id=2)
        proof1 = self.profile_verified(OTHER_PHONE, user_id=1)
        proof2 = self.profile_verified(OTHER_PHONE, user_id=2)
        body = lambda proof: {"expected_profile_version": 1, "phone_number": OTHER_PHONE,
                              "phone_verification_id": proof}
        held, other = self.race(profile_service, "usable_profile_proof",
                                lambda: self.patch_profile(body(proof1), 1),
                                lambda: self.patch_profile(body(proof2), 2), second_waits=False)
        self.assertEqual(other.status_code, 200, other.text)
        self.assert_error(held, 409, "PHONE_ALREADY_REGISTERED")
        self.assertEqual((self.basic(1).phone_e164, self.basic(2).phone_e164), ("+821012345678", "+821098765432"))
        self.assertIsNone(self.proof(proof1).consumed_at)
        self.assertIsNotNone(self.proof(proof2).consumed_at)
        self.assertEqual((self.version(1), self.version(2)), (1, 2))

    def test_a_patch_waits_for_a_step1_save_and_sees_its_new_version(self):
        verification_id = self.verified(user_id=1)
        saved, patched = self.race(
            basic_info_service, "registered_to_other_user",
            lambda: self.save(verification_id),
            lambda: self.patch_profile({"expected_profile_version": 0, "nickname": "동시수정"}))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assert_error(patched, 409, "PROFILE_VERSION_MISMATCH")
        self.assertEqual((self.user().nickname, self.version()), ("봉사자1", 1))
