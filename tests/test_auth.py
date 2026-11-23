"""Authentication tests use an isolated SQLite DB, never the development DB."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.models import User, UserRole, Dog, Shelter, Survey, AuthSession, SessionToken, MatchingResult
    from app.routers import auth, users, surveys, matching, simulation, dogs, breed_traits, public_api
    from app.auth import UNUSABLE_PASSWORD, hash_password, verify_password, token_digest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PASSWORD = "correct horse battery staple"
# Sign-up enforces complexity; login (Issue #32 scope) does not re-check it.
SIGNUP_PASSWORD = "Correct-Horse-Battery-9"


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
        # The real per-IP limiters stay on; reset so counts never carry over between tests.
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER):
            limiter.reset()
            self.addCleanup(limiter.reset)
        self.client = TestClient(app)
        with self.Session.begin() as db:
            db.add_all([User(id=1, login_id="user_one", nickname="one", password=self.password_hash),
                        User(id=2, login_id="user_two", nickname="two", password=self.password_hash),
                        User(id=3, login_id="user_demo", nickname="demo", password=UNUSABLE_PASSWORD),
                        User(id=4, login_id="user_old", nickname="old", password=PASSWORD),
                        Dog(id=1, name="테스트", energy_level=3)])

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def login(self, identifier="user_one", password=PASSWORD):
        response = self.client.post("/auth/login", json={"identifier": identifier, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["data"]["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"
        return token

    def session_count(self):
        with self.Session() as db:
            return db.query(AuthSession).count()

    def test_registration_without_email_hashes_password_and_logs_in_by_id(self):
        response = self.client.post("/users/", json={"login_id": "NewUser", "nickname": " new ",
                                                    "password": SIGNUP_PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("password", response.json()["data"])
        self.assertNotIn("email", response.json()["data"])
        with self.Session() as db:
            user = db.get(User, response.json()["data"]["id"])
            self.assertEqual(user.login_id, "newuser")
            self.assertEqual(user.nickname, "new")
            self.assertNotEqual(user.password, SIGNUP_PASSWORD)
            self.assertTrue(verify_password(SIGNUP_PASSWORD, user.password))
        self.login("NEWUSER", SIGNUP_PASSWORD)
        self.assertEqual(self.client.get("/users/me").json()["login_id"], "newuser")

    def test_duplicate_login_id_and_invalid_registration(self):
        body = {"login_id": "newuser", "nickname": "new", "password": SIGNUP_PASSWORD}
        self.assertEqual(self.client.post("/users/", json=body).status_code, 200)
        response = self.client.post("/users/", json={**body, "login_id": "NEWUSER", "nickname": "other"})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "LOGIN_ID_ALREADY_EXISTS"))
        for changes in ({"password": "short"}, {"login_id": "new_user"}, {"nickname": " "}):
            self.assertEqual(self.client.post("/users/", json={**body, **changes}).status_code, 422)

    def test_login_success_contract(self):
        response = self.client.post("/auth/login", json={"identifier": "  USER_One ", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["code"], body["message"]), ("OK", "로그인에 성공했습니다."))
        self.assertEqual(set(body["data"]), {"access_token", "token_type", "expires_in"})
        self.assertEqual((body["data"]["token_type"], body["data"]["expires_in"]), ("bearer", 3600))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.session_count(), 1)

    def test_login_input_errors_are_422_and_create_no_session(self):
        cases = (
            ({"password": PASSWORD}, "IDENTIFIER_REQUIRED", "identifier", "아이디를 입력해 주세요."),
            ({"identifier": "   ", "password": PASSWORD}, "IDENTIFIER_REQUIRED", "identifier", None),
            ({"identifier": None, "password": PASSWORD}, "IDENTIFIER_REQUIRED", "identifier", None),
            ({"identifier": 1234, "password": PASSWORD}, "IDENTIFIER_REQUIRED", "identifier", None),
            ({}, "IDENTIFIER_REQUIRED", "identifier", None),
            # The removed email field is not an alias: the request is missing identifier.
            ({"email": "one@example.com", "password": PASSWORD}, "IDENTIFIER_REQUIRED", "identifier", None),
            ({"identifier": "user_one"}, "PASSWORD_REQUIRED", "password", "비밀번호를 입력해 주세요."),
            ({"identifier": "user_one", "password": ""}, "PASSWORD_REQUIRED", "password", None),
            ({"identifier": "user_one", "password": "   "}, "PASSWORD_REQUIRED", "password", None),
            ({"identifier": "a" * 101, "password": PASSWORD}, "INVALID_IDENTIFIER_FORMAT", "identifier",
             "아이디 형식을 확인해 주세요."),
        )
        for body, code, field, message in cases:
            with self.subTest(body=body):
                response = self.client.post("/auth/login", json=body)
                self.assertEqual((response.status_code, response.json()["code"], response.json()["data"]),
                                 (422, code, {"field": field}))
                if message:
                    self.assertEqual(response.json()["message"], message)
        for raw in (b"", b"not json"):
            with self.subTest(raw=raw):
                response = self.client.post("/auth/login", content=raw, headers={"Content-Type": "application/json"})
                self.assertEqual((response.status_code, response.json()["code"]), (422, "IDENTIFIER_REQUIRED"))
        # 100 characters is still a lookup, not a format error; no such account exists.
        response = self.client.post("/auth/login", json={"identifier": "a" * 100, "password": PASSWORD})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.session_count(), 0)

    def test_login_and_only_hashed_token_is_persisted(self):
        token = self.login()
        self.assertEqual(self.client.get("/users/me").json()["id"], 1)
        with self.Session() as db:
            session = db.query(AuthSession).one()
            self.assertEqual(session.token_hash, token_digest(token))
            self.assertNotEqual(session.token_hash, token)

    def test_bad_credentials_and_legacy_accounts_are_rejected_unchanged(self):
        expected = {"code": "INVALID_CREDENTIALS", "data": {}, "message": "아이디와 비밀번호를 확인해 주세요."}
        # Unknown ID, wrong password, unusable hash, plaintext legacy password, an email
        # address (email login was removed) and an over-long password all look the same.
        for identifier, password in (("missing", PASSWORD), ("user_one", "wrong"), ("user_demo", PASSWORD),
                                     ("user_old", PASSWORD), ("one@example.com", PASSWORD),
                                     ("user_one", PASSWORD + "x" * 129)):
            with self.subTest(identifier=identifier):
                response = self.client.post("/auth/login", json={"identifier": identifier, "password": password})
                self.assertEqual((response.status_code, response.json()), (401, expected))
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.session_count(), 0)
        with self.Session() as db:
            self.assertEqual(db.get(User, 4).password, PASSWORD)
            self.assertEqual(db.get(User, 3).password, UNUSABLE_PASSWORD)

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
        self.login("user_two")
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
        response = self.client.post("/users/", json={"login_id": "roleuser", "nickname": "role",
                                                    "password": SIGNUP_PASSWORD, "role": "admin"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["role"], "user")
        with self.Session() as db:
            self.assertEqual(db.get(User, response.json()["data"]["id"]).role, UserRole.USER)

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

    def test_user_shelter_and_admin_log_in_by_id_and_user_views_have_no_email(self):
        with self.Session.begin() as db:
            db.add(Shelter(id=1, name="담당 보호소"))
            db.add_all([User(id=5, login_id="shelter01", nickname="보호소", password=self.password_hash,
                             role=UserRole.SHELTER, shelter_id=1),
                        User(id=6, login_id="admin01", nickname="관리자", password=self.password_hash,
                             role=UserRole.ADMIN)])
        for identifier, user_id, role in (("user_one", 1, "user"), ("SHELTER01", 5, "shelter"), ("Admin01", 6, "admin")):
            with self.subTest(role=role):
                self.login(identifier)
                expected = {"id": user_id, "login_id": identifier.lower(), "role": role}
                for path in ("/users/me", "/auth/me", f"/users/{user_id}"):
                    body = self.client.get(path).json()
                    self.assertNotIn("email", body, path)
                    self.assertEqual({key: body[key] for key in expected}, expected, path)
                self.client.post("/auth/logout")
        self.assertEqual(self.session_count(), 0)

    def test_legacy_plaintext_session_is_not_a_second_authentication_path(self):
        with self.Session.begin() as db:
            db.add(SessionToken(user_id=1, token="legacy-token", is_active=True,
                                expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        self.client.headers["Authorization"] = "Bearer legacy-token"
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/dogs/", json={"name": "차단"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
