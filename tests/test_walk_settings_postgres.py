"""Opt-in, real PostgreSQL row locks and rollback tests on a NEW empty local DB."""
from datetime import datetime, time, timedelta, timezone
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.auth import token_digest
    from app.database import Base, get_db
    from app.dog_walk_settings import apply_walk_settings
    from app.models import AuthSession, Dog, DogWalkSetting, DogWalkSlot, Shelter, User, UserRole
    from app.routers import dogs, shelters
    from app.walk_schemas import WalkSettingsPatch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("WALK_SETTINGS_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "WALK_SETTINGS_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다")
class WalkSettingsPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(TEST_URL)
        validate_local_engine(cls.engine)
        if not cls.engine.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        with cls.engine.begin() as connection:
            if inspect(connection).get_table_names():
                raise PreparationError("새로운 빈 테스트 DB만 허용합니다.")
            Base.metadata.create_all(connection)
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        app = FastAPI()
        app.include_router(dogs.router)
        app.include_router(shelters.router)

        def test_db():
            with self.Session() as db:
                yield db

        for dependency in (get_db, dogs.get_db, shelters.get_db):
            app.dependency_overrides[dependency] = test_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token = uuid4().hex
        with self.Session.begin() as db:
            own, other = Shelter(name="담당 보호소"), Shelter(name="다른 보호소")
            db.add_all([own, other])
            db.flush()
            self.shelter_id, self.other_shelter_id = own.id, other.id
            staff = User(email=f"{token}@example.com", nickname="담당", role=UserRole.SHELTER, shelter_id=own.id)
            db.add(staff)
            dog = Dog(name="테스트 강아지", shelter_id=own.id, energy_level=2)
            db.add(dog)
            db.flush()
            self.dog_id = dog.id
            db.add(AuthSession(token_hash=token_digest(token), user_id=staff.id,
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        self.client.headers["Authorization"] = f"Bearer {token}"

    def patch(self, payload):
        return self.client.patch(f"/dogs/{self.dog_id}/walk-settings", json=payload)

    def detail(self):
        response = self.client.get(f"/dogs/{self.dog_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["walk_settings"]

    def lock_dog(self, db):
        return db.query(Dog).filter_by(id=self.dog_id).with_for_update().one()

    def test_simultaneous_first_upsert_is_409_then_retry_keeps_single_setting(self):
        body = {"walkable": True, "slots": [{"day_of_week": 0, "start_time": "09:00", "end_time": "11:00"}]}
        with self.Session.begin() as first:
            apply_walk_settings(first, self.lock_dog(first), WalkSettingsPatch(**body))
            # The first writer has flushed but not committed its new setting.
            response = self.patch({"precautions": "재시도"})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertNotIn("SELECT", response.text)
            self.assertIsNone(self.detail())
        retry = self.patch({"precautions": "재시도"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["walkable"])
        self.assertEqual(len(retry.json()["slots"]), 1)
        with self.Session() as db:
            self.assertEqual(db.query(DogWalkSetting).filter_by(dog_id=self.dog_id).count(), 1)

    def test_existing_replacement_is_atomic_and_retry_preserves_omitted_slots(self):
        original = {"walkable": True, "slots": [{"day_of_week": 0, "start_time": "09:00", "end_time": "11:00"}]}
        self.assertEqual(self.patch(original).status_code, 200)
        before = self.detail()
        replacement = {"slots": [{"day_of_week": 2, "start_time": "12:00", "end_time": "13:00"}]}
        with self.Session.begin() as first:
            apply_walk_settings(first, self.lock_dog(first), WalkSettingsPatch(**replacement))
            self.assertEqual(self.patch({"walkable": False}).status_code, 409)
            self.assertEqual(self.detail(), before)  # no partial delete/insert is visible
        response = self.patch({"walkable": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["walkable"])
        self.assertEqual(response.json()["slots"][0]["day_of_week"], 2)
        self.assertEqual(response.json()["slots"][0]["start_time"], "12:00:00")

    def test_actual_database_constraint_error_restores_deleted_slots_and_parent(self):
        original = {"walkable": True, "slots": [{"day_of_week": 0, "start_time": "09:00", "end_time": "11:00"}]}
        self.assertEqual(self.patch(original).status_code, 200)
        before = self.detail()
        with self.Session() as db:
            old_updated = db.get(DogWalkSetting, before["id"]).updated_at

        def violate_check(mapper, connection, target):
            if target.walk_setting_id == before["id"]:
                target.day_of_week = 7  # Force the actual PostgreSQL CHECK after DELETE.

        event.listen(DogWalkSlot, "before_insert", violate_check)
        try:
            response = self.patch({"walkable": False, "slots": [{"day_of_week": 1, "start_time": "12:00", "end_time": "13:00"}]})
        finally:
            event.remove(DogWalkSlot, "before_insert", violate_check)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.detail(), before)
        with self.Session() as db:
            self.assertEqual(db.get(DogWalkSetting, before["id"]).updated_at, old_updated)
            self.assertEqual(db.get(Dog, self.dog_id).energy_level, 2)

    def test_parent_lock_and_recheck_protect_shelter_transfer(self):
        with self.Session.begin() as transfer:
            dog = self.lock_dog(transfer)
            dog.shelter_id = self.other_shelter_id
            transfer.flush()
            self.assertEqual(self.patch({"walkable": True}).status_code, 409)
        self.assertEqual(self.patch({"walkable": True}).status_code, 403)
        self.assertIsNone(self.detail())

    def test_native_local_time_roundtrip_and_public_filters(self):
        body = {"walkable": True, "slots": [{"day_of_week": 6, "start_time": "09:00:01.123456", "end_time": "10:00"}]}
        response = self.patch(body)
        self.assertEqual(response.status_code, 200, response.text)
        with self.Session() as db:
            setting = db.query(DogWalkSetting).filter_by(dog_id=self.dog_id).one()
            self.assertEqual(setting.slots[0].start_time, time(9, 0, 1, 123456))
            self.assertIsNone(setting.slots[0].start_time.tzinfo)
            self.assertIsNotNone(setting.updated_at.tzinfo)
        self.client.headers.pop("Authorization")
        for path in (f"/dogs/?shelter_id={self.shelter_id}&walkable=true", f"/shelters/{self.shelter_id}/dogs?walkable=true"):
            listed = self.client.get(path)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([row["id"] for row in listed.json()], [self.dog_id])


if __name__ == "__main__":
    unittest.main()
