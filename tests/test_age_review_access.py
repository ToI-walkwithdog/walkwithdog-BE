"""Issue #41 access control over every registered route of the real application.

Every route must be one of:
  - allowed for AGE_REVIEW_REQUIRED accounts (login, logout, GET /users/me, correction, exit),
  - public (no user identity, so there is nothing to check),
  - contract onboarding routes that answer 403 AGE_REVIEW_REQUIRED, or
  - `detail` routes that answer 403 {"detail": ...} for that account.
A route added later that fits none of these makes the inventory test fail.
Isolated SQLite with synthetic accounts; no external service is called.
"""
from datetime import datetime, timedelta, timezone
import os
import re
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.account_status import AGE_REVIEW_REQUIRED_MESSAGE
    from app.auth import UNUSABLE_PASSWORD, hash_password, token_digest
    from app.database import Base, get_db
    from app.main import app
    from app.models import AuthSession, User, UserPledgeAcknowledgement, UserRole
    from app.onboarding_common import get_clock
    from app.routers import auth, dogs, shelters, users
    from app.sms_provider import MockSmsProvider, get_sms_provider

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PASSWORD = "CorrectCurrentPassword1!"
PLEDGE_VERSION = "pledge-test-v1"  # test-only value
NOW = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)
UNAUTHORIZED_DETAIL = {"detail": "로그인이 필요하거나 세션이 만료되었습니다."}
AGE_DETAIL = {"detail": AGE_REVIEW_REQUIRED_MESSAGE}

ALLOWED = {("POST", "/auth/login"), ("POST", "/auth/logout"), ("GET", "/users/me"),
           ("PUT", "/users/me/age-review/birth-date"), ("DELETE", "/users/me")}
CONTRACT_BLOCKED = {("PUT", "/users/me/pledge"), ("GET", "/users/me/onboarding/basic-info"),
                    ("PUT", "/users/me/onboarding/basic-info"), ("POST", "/users/me/phone-verifications"),
                    ("POST", "/users/me/phone-verifications/confirm")}
# No user identity: shared, read-only or sign-up data that anyone may request.
PUBLIC = {("GET", "/"), ("GET", "/db-test"), ("GET", "/dogs/"), ("GET", "/dogs/{dog_id}"),
          ("GET", "/dogs/{dog_id}/personality"), ("GET", "/shelters/{shelter_id}"),
          ("GET", "/shelters/{shelter_id}/dogs"), ("GET", "/surveys/options"), ("GET", "/breed-traits/"),
          ("POST", "/users/"), ("GET", "/users/id-availability"), ("GET", "/users/nickname-availability")}
# Import-key routes: without a Bearer token they use X-Import-Key, not an account.
IMPORT_KEY = {("GET", "/public-api/test"), ("POST", "/public-api/fetch-dogs")}


def all_routes():
    found = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                found.add((method, route.path))
    return found


def concrete(path):
    return re.sub(r"\{[^}]+\}", "1", path)


class AccessControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(PASSWORD)

    def setUp(self):
        env = patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION,
                                      "PHONE_VERIFICATION_HMAC_KEY": "test-only-hmac-key-0123456789abcdef"})
        env.start()
        self.addCleanup(env.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def test_db():
            with self.Session() as db:
                yield db

        overrides = {get_db: test_db, dogs.get_db: test_db, shelters.get_db: test_db,
                     get_clock: lambda: (lambda: NOW), get_sms_provider: lambda: MockSmsProvider()}
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            overrides[limiter] = lambda: None
        app.dependency_overrides.update(overrides)
        self.addCleanup(app.dependency_overrides.clear)
        # Nothing here may reach an external service: stub the two that could.
        for target, value in (("app.routers.chat.generate_reply", ("stub", "stub")),
                              ("app.routers.public_api.fetch_pages", ([], {"pages": 1, "total_count": 0}))):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        with self.Session.begin() as db:
            db.add_all([
                User(id=1, login_id="minor01", nickname="연령확인", password=self.password_hash,
                     account_status="AGE_REVIEW_REQUIRED", age_review_required_at=NOW),
                User(id=2, login_id="walker02", nickname="활성", password=UNUSABLE_PASSWORD),
                User(id=3, login_id="shelter01", nickname="보호소", password=UNUSABLE_PASSWORD, role=UserRole.SHELTER),
                User(id=4, login_id="admin01", nickname="관리자", password=UNUSABLE_PASSWORD, role=UserRole.ADMIN),
            ])
            db.flush()
            for user_id in (1, 2, 3, 4):
                db.add(AuthSession(token_hash=token_digest(f"token-{user_id}"), user_id=user_id,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
                db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION, acknowledged_at=NOW))

    def call(self, method, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        kwargs = {} if method in ("GET", "DELETE") else {"json": {}}
        return self.client.request(method, concrete(path), headers=headers, **kwargs)

    def detail_routes(self):
        return sorted(all_routes() - ALLOWED - CONTRACT_BLOCKED - PUBLIC)

    def test_every_route_is_classified_and_the_restricted_account_is_blocked(self):
        routes = all_routes()
        self.assertLessEqual(ALLOWED | CONTRACT_BLOCKED | PUBLIC, routes)  # the lists match real routes
        blocked = self.detail_routes()
        self.assertGreaterEqual(len(blocked), 45)  # unique (method, path) pairs at the time of #41
        for method, path in blocked:
            with self.subTest(route=f"{method} {path}"):
                response = self.call(method, path, "token-1")
                self.assertEqual((response.status_code, response.json()), (403, AGE_DETAIL))
        for method, path in sorted(CONTRACT_BLOCKED):
            with self.subTest(route=f"{method} {path}"):
                response = self.call(method, path, "token-1")
                self.assertEqual((response.status_code, response.json()["code"]), (403, "AGE_REVIEW_REQUIRED"))

    def test_allowed_routes_still_work_for_the_restricted_account(self):
        self.assertEqual(self.client.get("/users/me", headers={"Authorization": "Bearer token-1"}).status_code, 200)
        login = self.client.post("/auth/login", json={"identifier": "minor01", "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["data"]["access_token"]
        self.assertEqual(self.client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"}).status_code, 200)
        # Correction and exit are reachable (their own rules apply; exit without a body is 422).
        exit_response = self.client.request("DELETE", "/users/me", headers={"Authorization": "Bearer token-1"}, json={})
        self.assertEqual(exit_response.json()["code"], "PASSWORD_REQUIRED")
        corrected = self.client.put("/users/me/age-review/birth-date", json={"birth_date": "2006-05-17"},
                                    headers={"Authorization": "Bearer token-1"})
        self.assertEqual(corrected.status_code, 200, corrected.text)

    def test_active_shelter_and_admin_accounts_are_never_blocked_by_status(self):
        for token in ("token-2", "token-3", "token-4"):
            for method, path in self.detail_routes() + sorted(CONTRACT_BLOCKED):
                with self.subTest(token=token, route=f"{method} {path}"):
                    response = self.call(method, path, token)
                    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    self.assertNotEqual(body, AGE_DETAIL)
                    self.assertNotEqual(body.get("code") if isinstance(body, dict) else None, "AGE_REVIEW_REQUIRED")

    def test_missing_and_expired_sessions_keep_the_existing_401(self):
        for method, path in self.detail_routes():
            if (method, path) in IMPORT_KEY:
                continue  # without a token these use the import key instead of a session
            with self.subTest(route=f"{method} {path}"):
                response = self.call(method, path)
                self.assertEqual((response.status_code, response.json()), (401, UNAUTHORIZED_DETAIL))
        with self.Session.begin() as db:
            db.get(AuthSession, token_digest("token-1")).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        response = self.call("GET", "/walk-requests/my", "token-1")
        self.assertEqual((response.status_code, response.json()), (401, UNAUTHORIZED_DETAIL))

    def test_correction_restores_access_at_once(self):
        self.assertEqual(self.call("GET", "/walk-requests/my", "token-1").status_code, 403)
        self.client.put("/users/me/age-review/birth-date", json={"birth_date": "2006-05-17"},
                        headers={"Authorization": "Bearer token-1"})
        response = self.call("GET", "/walk-requests/my", "token-1")
        self.assertEqual(response.status_code, 200, response.text)
        for method, path in self.detail_routes():
            with self.subTest(route=f"{method} {path}"):
                body = self.call(method, path, "token-1").json()
                self.assertNotEqual(body, AGE_DETAIL)

    def test_role_errors_keep_their_detail_format(self):
        # An ACTIVE general user on an admin route: the existing role 403 is unchanged.
        response = self.call("PATCH", "/users/{user_id}/shelter", "token-2")
        self.assertEqual(response.status_code, 403)
        self.assertIn("detail", response.json())
        self.assertNotEqual(response.json(), AGE_DETAIL)


if __name__ == "__main__":
    unittest.main()
