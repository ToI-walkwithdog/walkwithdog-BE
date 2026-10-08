"""Ownership regressions use real bearer sessions and an isolated SQLite database."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.auth import token_digest
    from app.database import Base, get_db
    from app.models import AuthSession, Dog, Shelter, User, UserRole
    from app.routers import auth, dogs, personality, shelters, users

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class ShelterPermissionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        for router in (auth.router, users.router, dogs.router, shelters.router, personality.router):
            app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        for dependency in (get_db, dogs.get_db, shelters.get_db):
            app.dependency_overrides[dependency] = test_db
        self.client = TestClient(app)
        with self.Session.begin() as db:
            db.add_all([Shelter(id=1, name="첫 보호소"), Shelter(id=2, name="다른 보호소")])
            db.flush()
            db.add_all([
                User(id=1, email="user@example.com", nickname="일반"),
                User(id=2, email="first@example.com", nickname="담당1", role=UserRole.SHELTER, shelter_id=1),
                User(id=3, email="second@example.com", nickname="담당2", role=UserRole.SHELTER, shelter_id=2),
                User(id=4, email="unset@example.com", nickname="미지정", role=UserRole.SHELTER),
                User(id=5, email="admin@example.com", nickname="관리자", role=UserRole.ADMIN),
                Dog(id=1, name="첫 강아지", shelter_id=1, description="기존 설명", energy_level=2),
                Dog(id=2, name="다른 강아지", shelter_id=2, description="기존 설명", energy_level=2),
                Dog(id=3, name="미지정 강아지", description="기존 설명", energy_level=2),
            ])
            db.flush()
            for user_id in range(1, 6):
                db.add(AuthSession(token_hash=token_digest(f"session-{user_id}"), user_id=user_id,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def authenticate(self, user_id):
        self.client.headers["Authorization"] = f"Bearer session-{user_id}"

    def dog_rows(self):
        with self.Session() as db:
            return [tuple(getattr(dog, column.name) for column in Dog.__table__.columns)
                    for dog in db.query(Dog).order_by(Dog.id)]

    def test_registration_cannot_choose_role_or_shelter(self):
        for role in ("shelter", "admin"):
            response = self.client.post("/users/", json={"email": f"new-{role}@example.com", "nickname": "새 사용자",
                                                        "password": "a sufficiently long password", "role": role,
                                                        "shelter_id": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["role"], "user")
            self.assertIsNone(response.json()["shelter_id"])
            with self.Session() as db:
                user = db.get(User, response.json()["id"])
                self.assertEqual(user.role, UserRole.USER)
                self.assertIsNone(user.shelter_id)

    def test_admin_can_assign_change_and_clear_with_current_session(self):
        self.authenticate(5)
        for shelter_id in (1, 2, None):
            response = self.client.patch("/users/4/shelter", json={"shelter_id": shelter_id})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["shelter_id"], shelter_id)
            self.assertEqual(response.json()["role"], "shelter")
            self.authenticate(4)
            for path in ("/users/me", "/auth/me"):
                self.assertEqual(self.client.get(path).json()["shelter_id"], shelter_id)
            self.authenticate(5)
            with self.Session() as db:
                self.assertEqual(db.get(User, 4).shelter_id, shelter_id)

    def test_only_admin_can_change_or_clear_assignment(self):
        for user_id in (None, 1, 2, 3, 4):
            self.client.headers.pop("Authorization", None)
            if user_id is not None:
                self.authenticate(user_id)
            for shelter_id in (2, None):
                response = self.client.patch("/users/2/shelter", json={"shelter_id": shelter_id})
                self.assertEqual(response.status_code, 401 if user_id is None else 403, response.text)
            with self.Session() as db:
                self.assertEqual(db.get(User, 2).shelter_id, 1)
                self.assertEqual(db.get(User, 2).role, UserRole.SHELTER)

    def test_assignment_errors_preserve_existing_user(self):
        self.authenticate(5)
        for path, payload, status in (
            ("/users/999/shelter", {"shelter_id": 1}, 404),
            ("/users/2/shelter", {"shelter_id": 999}, 404),
            ("/users/1/shelter", {"shelter_id": 1}, 409),
            ("/users/5/shelter", {"shelter_id": 1}, 409),
            ("/users/2/shelter", {}, 422),
            ("/users/2/shelter", {"shelter_id": 0}, 422),
            ("/users/2/shelter", {"shelter_id": 2, "role": "admin"}, 422),
        ):
            self.assertEqual(self.client.patch(path, json=payload).status_code, status, (path, payload))
        with self.Session() as db:
            self.assertEqual(db.get(User, 2).shelter_id, 1)
            self.assertEqual(db.get(User, 2).role, UserRole.SHELTER)
            self.assertIsNone(db.get(User, 1).shelter_id)

    def test_shelter_staff_can_create_only_in_own_shelter(self):
        for user_id, shelter_id in ((2, 1), (3, 2)):
            self.authenticate(user_id)
            response = self.client.post("/dogs/", json={"name": "새 강아지", "shelter_id": shelter_id,
                                                       "description": "활발하고 사람 좋아", "special_note": "훈련"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["shelter_id"], shelter_id)
            self.assertEqual(response.json()["energy_level"], 4)
            self.assertEqual(response.json()["sociability_level"], 4)
            self.assertEqual(response.json()["training_level"], 4)

    def test_forbidden_creates_never_calculate_traits_or_insert(self):
        before = self.dog_rows()
        for user_id, shelter_ids in ((None, (1,)), (1, (1, None)), (2, (2, None, 999)),
                                     (3, (1, None)), (4, (1, 2, None))):
            self.client.headers.pop("Authorization", None)
            if user_id is not None:
                self.authenticate(user_id)
            for shelter_id in shelter_ids:
                with patch("app.routers.dogs.calculate_traits_from_text") as calculate:
                    response = self.client.post("/dogs/", json={"name": "차단", "shelter_id": shelter_id})
                    self.assertEqual(response.status_code, 401 if user_id is None else 403, response.text)
                    calculate.assert_not_called()
        self.authenticate(2)
        self.assertEqual(self.client.post("/dogs/", json={"name": "소속 누락"}).status_code, 403)
        self.assertEqual(self.dog_rows(), before)

    def test_staff_update_own_dog_recalculates_traits(self):
        self.authenticate(2)
        response = self.client.put("/dogs/1/traits", json={"name": "변경", "description": "활발하고 사람 좋아",
                                                         "special_note": "훈련"})
        self.assertEqual(response.status_code, 200, response.text)
        with self.Session() as db:
            dog = db.get(Dog, 1)
            self.assertEqual(dog.name, "변경")
            self.assertEqual((dog.energy_level, dog.sociability_level, dog.training_level), (4, 4, 4))
            self.assertEqual(dog.trait_source, "shelter_text_parsed")

    def test_forbidden_updates_preserve_every_dog_column(self):
        before = self.dog_rows()
        for user_id, dog_ids in ((None, (1,)), (1, (1, 2, 3)), (2, (2, 3)), (3, (1, 3)), (4, (1, 2, 3))):
            self.client.headers.pop("Authorization", None)
            if user_id is not None:
                self.authenticate(user_id)
            for dog_id in dog_ids:
                with patch("app.routers.dogs.calculate_traits_from_text") as calculate:
                    response = self.client.put(f"/dogs/{dog_id}/traits", json={"name": "차단", "description": "활발",
                                                                              "special_note": "훈련"})
                    self.assertEqual(response.status_code, 401 if user_id is None else 403, response.text)
                    calculate.assert_not_called()
        self.assertEqual(self.dog_rows(), before)

    def test_assignment_changes_and_revocation_take_effect_without_new_login(self):
        self.authenticate(5)
        self.assertEqual(self.client.patch("/users/2/shelter", json={"shelter_id": 2}).status_code, 200)
        self.authenticate(2)
        self.assertEqual(self.client.put("/dogs/1/traits", json={"description": "차단"}).status_code, 403)
        self.assertEqual(self.client.put("/dogs/2/traits", json={"description": "허용"}).status_code, 200)
        self.authenticate(5)
        self.assertEqual(self.client.patch("/users/2/shelter", json={"shelter_id": None}).status_code, 200)
        self.authenticate(2)
        self.assertEqual(self.client.put("/dogs/2/traits", json={"description": "차단"}).status_code, 403)
        self.assertEqual(self.client.post("/dogs/", json={"name": "차단", "shelter_id": 2}).status_code, 403)

    def test_admin_can_create_and_update_all_shelters_and_unassigned_dogs(self):
        self.authenticate(5)
        for shelter_id in (1, 2, None):
            response = self.client.post("/dogs/", json={"name": "관리자 등록", "shelter_id": shelter_id})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["shelter_id"], shelter_id)
        for dog_id in (1, 2, 3):
            self.assertEqual(self.client.put(f"/dogs/{dog_id}/traits", json={"description": "관리자 수정"}).status_code, 200)
        with self.Session() as db:
            self.assertEqual([db.get(Dog, i).description for i in (1, 2, 3)], ["관리자 수정"] * 3)

    def test_missing_dog_or_shelter_remains_404_and_preserves_data(self):
        self.authenticate(5)
        before = self.dog_rows()
        self.assertEqual(self.client.post("/dogs/", json={"name": "실패", "shelter_id": 999}).status_code, 404)
        self.assertEqual(self.client.put("/dogs/999/traits", json={"description": "실패"}).status_code, 404)
        self.assertEqual(self.dog_rows(), before)

    def test_only_admin_can_create_shelters(self):
        for user_id in (None, 1, 2, 3, 4):
            self.client.headers.pop("Authorization", None)
            if user_id is not None:
                self.authenticate(user_id)
            response = self.client.post("/shelters/", json={"name": "차단"})
            self.assertEqual(response.status_code, 401 if user_id is None else 403, response.text)
        with self.Session() as db:
            self.assertEqual(db.query(Shelter).count(), 2)
        self.authenticate(5)
        response = self.client.post("/shelters/", json={"name": "관리자 보호소", "phone": "02-123-4567"})
        self.assertEqual(response.status_code, 200, response.text)
        with self.Session() as db:
            self.assertEqual(db.query(Shelter).count(), 3)

    def test_public_reads_remain_available_without_login(self):
        for path in ("/dogs/", "/dogs/1", "/dogs/2", "/dogs/3", "/dogs/1/personality", "/shelters/1"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
