"""Authentication tests use an isolated SQLite DB, never the development DB."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.models import User, UserRole, Dog, Shelter, Survey, AuthSession, SessionToken, MatchingResult
    from app.routers import auth, users, surveys, matching, simulation, dogs, breed_traits, public_api
    from app.auth import hash_password, verify_password, token_digest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PASSWORD = "correct horse battery staple"


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(PASSWORD)

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        for router in (auth.router, users.router, surveys.router, matching.router, simulation.router,
                       dogs.router, breed_traits.router, public_api.router):
            app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[dogs.get_db] = test_db
        self.client = TestClient(app)
        with self.Session.begin() as db:
            db.add_all([User(id=1, email="one@example.com", nickname="one", password=self.password_hash),
                        User(id=2, email="two@example.com", nickname="two", password=self.password_hash),
                        User(id=3, email="demo@example.com", nickname="demo"),
                        User(id=4, email="old@example.com", nickname="old", password=PASSWORD),
                        Dog(id=1, name="테스트", energy_level=3)])

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def login(self, email="one@example.com"):
        response = self.client.post("/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"
        return token

    def test_registration_hashes_password_and_normalizes_email(self):
        response = self.client.post("/users/", json={"email": "New@Example.com", "nickname": " new ", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("password", response.json())
        with self.Session() as db:
            user = db.get(User, response.json()["id"])
            self.assertEqual(user.email, "new@example.com")
            self.assertEqual(user.nickname, "new")
            self.assertNotEqual(user.password, PASSWORD)
            self.assertTrue(verify_password(PASSWORD, user.password))
        self.login("NEW@example.com")

    def test_duplicate_email_and_invalid_registration(self):
        self.assertEqual(self.client.post("/users/", json={"email": "ONE@example.com", "nickname": "n", "password": PASSWORD}).status_code, 409)
        for changes in ({"password": "short"}, {"email": "invalid"}, {"nickname": " "}):
            self.assertEqual(self.client.post("/users/", json={"email": "new@example.com", "nickname": "n", "password": PASSWORD, **changes}).status_code, 422)

    def test_login_and_only_hashed_token_is_persisted(self):
        token = self.login()
        self.assertEqual(self.client.get("/users/me").json()["id"], 1)
        with self.Session() as db:
            session = db.query(AuthSession).one()
            self.assertEqual(session.token_hash, token_digest(token))
            self.assertNotEqual(session.token_hash, token)

    def test_bad_credentials_and_legacy_accounts_are_rejected_unchanged(self):
        for email, password in (("missing@example.com", PASSWORD), ("one@example.com", "wrong"),
                                ("demo@example.com", PASSWORD), ("old@example.com", PASSWORD)):
            response = self.client.post("/auth/login", json={"email": email, "password": password})
            self.assertEqual(response.status_code, 401)
        with self.Session() as db:
            self.assertEqual(db.get(User, 4).password, PASSWORD)
            self.assertIsNone(db.get(User, 3).password)

    def test_missing_and_invalid_tokens(self):
        self.assertEqual(self.client.get("/surveys/options").status_code, 200)
        for header in (None, "Bearer invalid", "Basic invalid"):
            self.client.headers.pop("Authorization", None)
            if header:
                self.client.headers["Authorization"] = header
            for path in ("/users/me", "/users/1", "/surveys/1", "/matching/history/1", "/simulation/timeline?user_id=1&dog_id=1"):
                self.assertEqual(self.client.get(path).status_code, 401, path)
            self.assertEqual(self.client.post("/surveys/", json={"user_id": 1}).status_code, 401)
            self.assertEqual(self.client.post("/matching/1").status_code, 401)
            self.assertEqual(self.client.post("/matching/score", json={"user_id": 1, "dog_id": 1}).status_code, 401)

    def test_expired_session(self):
        token = self.login()
        with self.Session.begin() as db:
            db.get(AuthSession, token_digest(token)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.client.get("/users/me").status_code, 401)

    def test_logout_revokes_session(self):
        self.login()
        self.assertEqual(self.client.post("/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/users/me").status_code, 401)

    def test_other_users_private_routes_forbidden(self):
        self.login()
        for path in ("/users/2", "/surveys/2", "/matching/history/2", "/simulation/timeline?user_id=2&dog_id=1"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.post("/surveys/", json={"user_id": 2}).status_code, 403)
        self.assertEqual(self.client.post("/matching/score", json={"user_id": 2, "dog_id": 1}).status_code, 403)
        with self.Session() as db:
            self.assertEqual(db.query(Survey).count(), 0)

    def test_foreign_survey_unavailable_even_when_matches_exist(self):
        with self.Session.begin() as db:
            survey = Survey(user_id=2)
            db.add(survey)
            db.flush()
            survey_id = survey.id
            db.add(MatchingResult(user_id=2, dog_id=1, survey_id=survey.id, score=50))
        self.login()
        self.assertEqual(self.client.post(f"/matching/{survey_id}").status_code, 404)
        with self.Session() as db:
            self.assertEqual(db.query(MatchingResult).count(), 1)

    def test_authenticated_survey_matching_flow_and_idempotency(self):
        self.login()
        survey = self.client.post("/surveys/", json={"user_id": 1, "housing_type": "studio"}).json()
        response = self.client.post(f"/matching/{survey['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.client.post(f"/matching/{survey['id']}").json())
        self.assertEqual(response.json(), self.client.get("/matching/history/1").json())
        self.assertEqual(self.client.get("/simulation/timeline?user_id=1&dog_id=1").status_code, 200)

    def test_two_real_sessions_remain_isolated(self):
        first_token = self.login()
        survey = self.client.post("/surveys/", json={"user_id": 1}).json()
        self.client.post(f"/matching/{survey['id']}")
        self.login("two@example.com")
        self.assertEqual(self.client.get("/users/me").json()["id"], 2)
        self.assertEqual(self.client.get("/surveys/2").json(), [])
        self.assertEqual(self.client.get("/matching/history/2").json(), [])
        self.assertEqual(self.client.post(f"/matching/{survey['id']}").status_code, 404)
        self.client.post("/auth/logout")
        self.client.headers["Authorization"] = f"Bearer {first_token}"
        self.assertEqual(self.client.get("/users/me").status_code, 200)

    def test_token_tampering_and_missing_user_are_rejected(self):
        token = self.login()
        self.client.headers["Authorization"] = f"Bearer {token}tampered"
        self.assertEqual(self.client.get("/users/me").status_code, 401)
        self.client.headers["Authorization"] = f"Bearer {token}"
        with self.Session.begin() as db:
            db.delete(db.get(User, 1))
        self.assertEqual(self.client.get("/users/me").status_code, 401)

    def test_role_defaults_cannot_be_selected_during_registration(self):
        response = self.client.post("/users/", json={"email": "role@example.com", "nickname": "role",
                                                    "password": PASSWORD, "role": "admin"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "user")
        with self.Session() as db:
            self.assertEqual(db.get(User, response.json()["id"]).role, UserRole.USER)

    def test_one_session_authenticates_personal_and_role_protected_routes(self):
        with self.Session.begin() as db:
            db.add(Shelter(id=1, name="담당 보호소"))
            db.flush()
            db.get(User, 1).role = UserRole.SHELTER
            db.get(User, 1).shelter_id = 1
            db.get(Dog, 1).shelter_id = 1
        self.login()
        self.assertEqual(self.client.get("/users/me").json(), self.client.get("/auth/me").json())
        self.assertEqual(self.client.get("/auth/me").json()["role"], "shelter")
        self.assertEqual(self.client.post("/dogs/", json={"name": "권한 검증", "shelter_id": 1}).status_code, 200)
        self.assertEqual(self.client.put("/dogs/1/traits", json={"description": "관찰"}).status_code, 200)
        self.assertEqual(self.client.post("/breed-traits/", json={"breed_name": "권한 검증"}).status_code, 403)
        self.client.post("/auth/logout")
        self.assertEqual(self.client.post("/dogs/", json={"name": "차단"}).status_code, 401)

    def test_regular_users_cannot_write_managed_dog_data(self):
        self.login()
        self.assertEqual(self.client.post("/dogs/", json={"name": "차단"}).status_code, 403)
        self.assertEqual(self.client.put("/dogs/1/traits", json={"description": "차단"}).status_code, 403)
        self.assertEqual(self.client.post("/breed-traits/", json={"breed_name": "차단"}).status_code, 403)
        with self.Session() as db:
            self.assertEqual(db.query(Dog).count(), 1)

    def test_admin_can_use_hashed_session_for_managed_routes_and_import(self):
        with self.Session.begin() as db:
            db.get(User, 1).role = UserRole.ADMIN
        self.login()
        self.assertEqual(self.client.post("/breed-traits/", json={"breed_name": "검증"}).status_code, 200)
        with patch("app.routers.public_api.fetch_pages", return_value=([], {"pages": 1, "total_count": 0})):
            response = self.client.post("/public-api/fetch-dogs", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["dry_run"])
            self.assertEqual(self.client.get("/public-api/test").status_code, 200)
        with patch.dict(os.environ, {"PUBLIC_IMPORT_KEY": "test-import-key"}):
            self.assertEqual(self.client.post("/public-api/fetch-dogs",
                                             headers={"X-Import-Key": "wrong"}, json={}).status_code, 403)

    def test_non_admin_import_is_blocked_before_contacting_provider(self):
        for role in (UserRole.USER, UserRole.SHELTER):
            with self.Session.begin() as db:
                db.get(User, 1).role = role
            self.login()
            with patch("app.routers.public_api.fetch_pages") as fetch:
                self.assertEqual(self.client.post("/public-api/fetch-dogs", json={}).status_code, 403)
                self.assertEqual(self.client.get("/public-api/test").status_code, 403)
                fetch.assert_not_called()

    def test_legacy_plaintext_session_is_not_a_second_authentication_path(self):
        with self.Session.begin() as db:
            db.add(SessionToken(user_id=1, token="legacy-token", is_active=True,
                                expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        self.client.headers["Authorization"] = "Bearer legacy-token"
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/dogs/", json={"name": "차단"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
