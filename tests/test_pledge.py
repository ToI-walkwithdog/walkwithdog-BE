"""PUT /users/me/pledge (Issue #33) on an isolated SQLite DB with synthetic accounts."""
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import pledge_service
    from app.auth import UNUSABLE_PASSWORD, token_digest
    from app.database import Base, get_db
    from app.models import AuthSession, User, UserPledgeAcknowledgement, UserRole
    from app.routers import auth, users

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from privacy_test_support import privacy, use_test_privacy_policy

# Test-only value. The real current pledge version is a server setting that is not decided yet.
VERSION = "pledge-test-v1"
PATH = "/users/me/pledge"
OK_MESSAGE = "서약 확인이 완료되었습니다."


class PledgeTests(unittest.TestCase):
    def setUp(self):
        version_env = patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": VERSION})
        version_env.start()
        self.addCleanup(version_env.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(users.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            app.dependency_overrides[limiter] = lambda: None
        self.client = TestClient(app)
        with self.Session.begin() as db:
            db.add_all([
                User(id=1, login_id="walker01", nickname="봉사자", password=UNUSABLE_PASSWORD),
                User(id=2, login_id="shelter01", nickname="보호소", password=UNUSABLE_PASSWORD, role=UserRole.SHELTER),
                User(id=3, login_id="admin01", nickname="관리자", password=UNUSABLE_PASSWORD, role=UserRole.ADMIN),
            ])
            db.flush()
            for user_id in (1, 2, 3):
                db.add(AuthSession(token_hash=token_digest(f"token-{user_id}"), user_id=user_id,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def pledge(self, user_id=1, body=None, **kwargs):
        headers = {"Authorization": f"Bearer token-{user_id}"} if user_id else {}
        return self.client.put(PATH, json={"pledge_version": VERSION} if body is None else body,
                               headers=headers, **kwargs)

    def records(self):
        with self.Session() as db:
            return [(r.user_id, r.pledge_version, r.acknowledged_at)
                    for r in db.query(UserPledgeAcknowledgement).order_by(UserPledgeAcknowledgement.id)]

    def assert_error(self, response, status, code, data, message=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual((response.json()["code"], response.json()["data"]), (code, data))
        if message:
            self.assertEqual(response.json()["message"], message)

    # Success and idempotency

    def test_first_pledge_stores_one_row_and_returns_the_contract(self):
        response = self.pledge()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["code"], body["message"]), ("OK", OK_MESSAGE))
        self.assertEqual(set(body["data"]), {"pledge_completed", "pledge_version", "acknowledged_at"})
        self.assertEqual((body["data"]["pledge_completed"], body["data"]["pledge_version"]), (True, VERSION))
        (user_id, version, stored), = self.records()
        self.assertEqual((user_id, version), (1, VERSION))
        returned = datetime.fromisoformat(body["data"]["acknowledged_at"])
        self.assertEqual(returned, stored.replace(tzinfo=timezone.utc))
        self.assertLess(abs(datetime.now(timezone.utc) - returned), timedelta(minutes=1))

    def test_user_id_and_time_come_from_the_server_not_the_request(self):
        later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        response = self.pledge(body={"pledge_version": VERSION, "user_id": 2, "acknowledged_at": later})
        self.assertEqual(response.status_code, 200, response.text)
        (user_id, _, stored), = self.records()
        self.assertEqual(user_id, 1)
        self.assertNotEqual(response.json()["data"]["acknowledged_at"], later)

    def test_same_version_again_keeps_the_first_time_and_one_row(self):
        first = self.pledge().json()["data"]["acknowledged_at"]
        later = datetime.now(timezone.utc) + timedelta(hours=5)
        with patch("app.pledge_service.datetime") as clock:
            clock.now.return_value = later
            for _ in range(2):  # e.g. a retry after a lost response
                again = self.pledge()
                self.assertEqual((again.status_code, again.json()["data"]["acknowledged_at"]), (200, first))
        self.assertEqual(len(self.records()), 1)

    def test_signed_up_user_pledges_right_after_login_before_any_step(self):
        use_test_privacy_policy(self)
        signup = self.client.post("/users/", json={"login_id": "newwalker", "nickname": "새봉사자",
                                                   "password": "WalkTogether2026!", "privacy": privacy()})
        self.assertEqual(signup.status_code, 200, signup.text)
        login = self.client.post("/auth/login", json={"identifier": "newwalker", "password": "WalkTogether2026!"})
        token = login.json()["data"]["access_token"]
        # No Step 1 to 3 record exists for this account; the pledge does not require one.
        response = self.client.put(PATH, json={"pledge_version": VERSION}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r[0] for r in self.records()], [signup.json()["data"]["id"]])
        # GET /users/me is unchanged here: onboarding status fields belong to Issue #45.
        me = self.client.get("/users/me", headers={"Authorization": f"Bearer {token}"}).json()["data"]
        self.assertEqual(set(me), {"id", "login_id", "nickname", "role", "shelter_id"})

    def test_older_version_rows_are_kept_and_the_new_version_needs_its_own_pledge(self):
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=1, pledge_version="pledge-test-v0", acknowledged_at=old_time))
        with self.Session() as db:
            # An older version alone is not a completed pledge for the current version.
            self.assertIsNone(pledge_service.current_pledge_acknowledgement(db, 1))
        self.assertEqual(self.pledge().status_code, 200)
        records = self.records()
        self.assertEqual([(r[0], r[1]) for r in records], [(1, "pledge-test-v0"), (1, VERSION)])
        self.assertEqual(records[0][2].replace(tzinfo=timezone.utc), old_time)
        with self.Session() as db:
            self.assertEqual(pledge_service.current_pledge_acknowledgement(db, 1).pledge_version, VERSION)
        # The client cannot record an older version again: it is not the server's version.
        self.assert_error(self.pledge(body={"pledge_version": "pledge-test-v0"}), 409,
                          "PLEDGE_VERSION_MISMATCH", {"field": "pledge_version"})

    def test_each_user_has_their_own_record(self):
        with self.Session.begin() as db:
            db.add(User(id=4, login_id="walker02", nickname="봉사자2", password=UNUSABLE_PASSWORD))
            db.flush()
            db.add(AuthSession(token_hash=token_digest("token-4"), user_id=4,
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        self.assertEqual(self.pledge(1).status_code, 200)
        self.assertEqual(self.pledge(4).status_code, 200)
        self.assertEqual(sorted(r[0] for r in self.records()), [1, 4])

    # Request validation

    def test_version_required_and_format_errors_store_nothing(self):
        required = ("PLEDGE_VERSION_REQUIRED", "서약 버전을 확인해 주세요.")
        invalid = ("INVALID_PLEDGE_VERSION", "올바른 서약 버전을 입력해 주세요.")
        cases = [({}, required), ({"pledge_version": None}, required), ({"pledge_version": ""}, required),
                 ({"pledge_version": "   "}, required), ({"pledge_version": 1}, invalid),
                 ({"pledge_version": True}, invalid), ({"pledge_version": ["pledge-test-v1"]}, invalid),
                 ({"pledge_version": "v" * 33}, invalid)]
        for body, (code, message) in cases:
            with self.subTest(body=body):
                self.assert_error(self.pledge(body=body), 422, code, {"field": "pledge_version"}, message)
        for raw in (b"", b"not json"):
            with self.subTest(raw=raw):
                response = self.client.put(PATH, content=raw, headers={"Authorization": "Bearer token-1",
                                                                       "Content-Type": "application/json"})
                self.assert_error(response, 422, "PLEDGE_VERSION_REQUIRED", {"field": "pledge_version"})
        self.assertEqual(self.records(), [])

    def test_version_mismatch_is_409_and_stores_nothing(self):
        for version in ("pledge-test-v2", "v" * 32, f" {VERSION}x "):
            with self.subTest(version=version):
                self.assert_error(self.pledge(body={"pledge_version": version}), 409, "PLEDGE_VERSION_MISMATCH",
                                  {"field": "pledge_version"}, "서약 내용을 다시 확인해 주세요.")
        self.assertEqual(self.records(), [])
        # Surrounding spaces are trimmed before the comparison.
        self.assertEqual(self.pledge(body={"pledge_version": f"  {VERSION}  "}).status_code, 200)

    # Authentication and role

    def test_authentication_errors_store_nothing(self):
        self.assert_error(self.pledge(user_id=None), 401, "AUTHENTICATION_REQUIRED", {}, "로그인이 필요합니다.")
        response = self.client.put(PATH, json={"pledge_version": VERSION}, headers={"Authorization": "Bearer forged"})
        self.assert_error(response, 401, "INVALID_ACCESS_TOKEN", {}, "유효하지 않은 인증 정보입니다.")
        with self.Session.begin() as db:
            db.get(AuthSession, token_digest("token-1")).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assert_error(self.pledge(), 401, "SESSION_EXPIRED", {}, "로그인 세션이 만료되었습니다.")
        # Authentication is checked before the body: a bad body without a token is still 401.
        self.assert_error(self.pledge(user_id=None, body={}), 401, "AUTHENTICATION_REQUIRED", {})
        self.assertEqual(self.records(), [])

    def test_shelter_and_admin_get_403_before_body_validation(self):
        for user_id in (2, 3):
            with self.subTest(user_id=user_id):
                self.assert_error(self.pledge(user_id), 403, "PLEDGE_NOT_ALLOWED", {}, "서약할 수 없는 계정입니다.")
                self.assert_error(self.pledge(user_id, body={}), 403, "PLEDGE_NOT_ALLOWED", {})
        self.assertEqual(self.records(), [])

    def test_age_review_account_gets_403_and_no_record(self):
        """Issue #40: an AGE_REVIEW_REQUIRED account gets 403 and no row; earlier rows are kept."""
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=1, pledge_version="pledge-test-v0", acknowledged_at=old_time))
            db.get(User, 1).account_status = "AGE_REVIEW_REQUIRED"
        self.assert_error(self.pledge(), 403, "AGE_REVIEW_REQUIRED", {}, "연령 확인 정보를 먼저 수정해 주세요.")
        # Role is checked first, status before the body: a bad body still gets this 403.
        self.assert_error(self.pledge(body={}), 403, "AGE_REVIEW_REQUIRED", {})
        self.assertEqual([(r[0], r[1]) for r in self.records()], [(1, "pledge-test-v0")])

    # Server settings and failures

    def test_missing_or_invalid_server_version_fails_closed(self):
        for value in (None, "", "   ", "v" * 33):
            env = {k: v for k, v in os.environ.items() if k != "PLEDGE_CURRENT_VERSION"}
            if value is not None:
                env["PLEDGE_CURRENT_VERSION"] = value
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True), \
                    self.assertLogs("app.api_contract", "ERROR") as logs:
                self.assert_error(self.pledge(), 500, "INTERNAL_SERVER_ERROR", {})
                self.assertIn("PledgeVersionNotConfigured", "".join(logs.output))
        self.assertEqual(self.records(), [])

    def test_failed_commit_rolls_back_and_a_retry_succeeds(self):
        original = Session.commit
        calls = []

        def failing_commit(session):
            if not calls:
                calls.append(1)
                raise OperationalError("COMMIT", {}, Exception("disk full"))
            return original(session)

        with patch.object(Session, "commit", failing_commit), self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.pledge(), 500, "INTERNAL_SERVER_ERROR", {})
        self.assertEqual(self.records(), [])
        self.assertEqual(self.pledge().status_code, 200)
        self.assertEqual(len(self.records()), 1)

    def test_openapi_documents_the_route(self):
        app = FastAPI()
        app.include_router(users.router)
        operation = app.openapi()["paths"][PATH]["put"]
        self.assertEqual(set(operation["responses"]), {"200", "401", "403", "409", "422", "500"})


if __name__ == "__main__":
    unittest.main()
