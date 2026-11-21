"""Sign-up and availability contract tests on an isolated SQLite DB (synthetic data only)."""
import os
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.api_contract import too_many_requests
    from app.auth import UNUSABLE_PASSWORD, verify_password
    from app.database import Base, get_db
    from app.models import User, UserRole
    from app.routers import auth, users

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateIndex

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "WalkTogether2026!"
VALID = {"login_id": "walker04", "nickname": "산책왕", "email": "walker@example.com", "password": PASSWORD}
REQUIRED = ("REQUIRED_FIELD", "필수 입력값을 입력해주세요.")
MESSAGES = {
    "INVALID_LOGIN_ID_LENGTH": "아이디는 4자 이상 20자 이하로 입력해주세요.",
    "INVALID_LOGIN_ID_FORMAT": "아이디는 영문 소문자, 숫자, 밑줄(_)만 사용할 수 있습니다.",
    "INVALID_NICKNAME_LENGTH": "닉네임은 3자 이상 20자 이하로 입력해주세요.",
    "INVALID_NICKNAME_FORMAT": "닉네임은 한글, 영문, 숫자, 밑줄(_)만 사용할 수 있습니다.",
    "INVALID_EMAIL": "올바른 이메일 주소를 입력해주세요.",
    "INVALID_PASSWORD_LENGTH": "비밀번호는 15자 이상 128자 이하로 입력해주세요.",
    "PASSWORD_UPPERCASE_REQUIRED": "비밀번호에 영문 대문자를 1개 이상 포함해주세요.",
    "PASSWORD_LOWERCASE_REQUIRED": "비밀번호에 영문 소문자를 1개 이상 포함해주세요.",
    "PASSWORD_NUMBER_REQUIRED": "비밀번호에 숫자를 1개 이상 포함해주세요.",
    "PASSWORD_SPECIAL_REQUIRED": "비밀번호에 특수문자를 1개 이상 포함해주세요.",
    "LOGIN_ID_ALREADY_EXISTS": "이미 사용 중인 아이디입니다.",
    "NICKNAME_ALREADY_EXISTS": "이미 사용 중인 닉네임입니다.",
    "EMAIL_ALREADY_EXISTS": "이미 가입된 이메일입니다.",
    "LOGIN_ID_REQUIRED": "아이디를 입력해주세요.",
    "NICKNAME_REQUIRED": "닉네임을 입력해주세요.",
}
LOGIN_ID_LENGTH = {"field": "login_id", "min_length": 4, "max_length": 20}
NICKNAME_LENGTH = {"field": "nickname", "min_length": 3, "max_length": 20}
PASSWORD_LENGTH = {"field": "password", "min_length": 15, "max_length": 128}


class SignupTests(unittest.TestCase):
    def setUp(self):
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
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def signup(self, **changes):
        return self.client.post("/users/", json={**VALID, **changes})

    def user_count(self):
        with self.Session() as db:
            return db.query(User).count()

    def assert_contract(self, response, status, code, data, message=None):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body), {"code", "data", "message"})
        self.assertEqual((body["code"], body["data"]), (code, data))
        expected = message or MESSAGES.get(code) or (REQUIRED[1] if code == REQUIRED[0] else None)
        self.assertEqual(body["message"], expected)

    # Sign-up success and storage

    def test_signup_returns_contract_and_stores_normalized_values_with_scrypt_hash(self):
        response = self.signup(login_id="  Walker04 ", email=" Walker@Example.COM ", nickname="  산책왕 ")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["code"], "OK")
        self.assertEqual(body["message"], "회원가입이 완료되었습니다.")
        self.assertEqual(body["data"], {"id": body["data"]["id"], "login_id": "walker04", "nickname": "산책왕",
                                        "email": "walker@example.com", "role": "user", "shelter_id": None})
        self.assertNotIn(PASSWORD, response.text)
        self.assertNotIn("scrypt$", response.text)
        self.assertNotIn("access_token", response.text)
        with self.Session() as db:
            user = db.get(User, body["data"]["id"])
            self.assertTrue(user.password.startswith("scrypt$"))
            self.assertTrue(verify_password(PASSWORD, user.password))
            self.assertEqual(user.role, UserRole.USER)
            self.assertIsNone(user.shelter_id)

    def test_password_is_hashed_exactly_as_entered(self):
        spaced = "  WalkTogether2026!  "
        response = self.signup(password=spaced)
        self.assertEqual(response.status_code, 200, response.text)
        with self.Session() as db:
            stored = db.get(User, response.json()["data"]["id"]).password
        self.assertTrue(verify_password(spaced, stored))
        self.assertFalse(verify_password(spaced.strip(), stored))

    def test_role_and_shelter_cannot_be_chosen_and_new_account_can_log_in(self):
        response = self.signup(role="admin", shelter_id=1)
        self.assertEqual((response.json()["data"]["role"], response.json()["data"]["shelter_id"]), ("user", None))
        login = self.client.post("/auth/login", json={"email": VALID["email"], "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)

    def test_unicode_nickname_is_stored_in_nfc(self):
        decomposed = unicodedata.normalize("NFD", "산책왕")
        self.assertNotEqual(decomposed, "산책왕")
        response = self.signup(nickname=decomposed)
        self.assertEqual(response.json()["data"]["nickname"], "산책왕")

    # Input validation

    def test_required_fields(self):
        for field in VALID:
            for label, body in (("missing", {k: v for k, v in VALID.items() if k != field}),
                                ("null", {**VALID, field: None}),
                                ("empty", {**VALID, field: ""}),
                                ("number", {**VALID, field: 1234})):
                with self.subTest(field=field, case=label):
                    self.assert_contract(self.client.post("/users/", json=body), 422, "REQUIRED_FIELD", {"field": field})
        for field in ("login_id", "nickname", "email"):
            with self.subTest(field=field, case="blank"):
                self.assert_contract(self.signup(**{field: "   "}), 422, "REQUIRED_FIELD", {"field": field})
        for label, kwargs in (("no body", {}), ("broken json", {"content": b"{bad",
                                                               "headers": {"Content-Type": "application/json"}}),
                              ("array", {"json": [VALID]})):
            with self.subTest(case=label):
                self.assert_contract(self.client.post("/users/", **kwargs), 422, "REQUIRED_FIELD", {"field": "login_id"})
        self.assertEqual(self.user_count(), 0)

    def test_first_invalid_field_in_request_order_is_reported(self):
        response = self.client.post("/users/", json={"login_id": "ab", "email": "bad"})
        self.assert_contract(response, 422, "INVALID_LOGIN_ID_LENGTH", LOGIN_ID_LENGTH)
        response = self.signup(nickname="no", password="short")
        self.assert_contract(response, 422, "INVALID_NICKNAME_LENGTH", NICKNAME_LENGTH)

    def test_login_id_length_and_format(self):
        for value in ("abc", "a" * 21, " abc "):
            with self.subTest(value=value):
                self.assert_contract(self.signup(login_id=value), 422, "INVALID_LOGIN_ID_LENGTH", LOGIN_ID_LENGTH)
        for value in ("walker-04", "walker 04", "walker.04", "산책아이디", "walker!", "Kelvin1"):
            with self.subTest(value=value):
                self.assert_contract(self.signup(login_id=value), 422, "INVALID_LOGIN_ID_FORMAT", {"field": "login_id"})
        for value in ("abcd", "a" * 20, "____", "1234", "WALKER_04"):
            with self.subTest(value=value):
                self.assertEqual(self.client.get("/users/id-availability", params={"login_id": value}).status_code, 200)

    def test_nickname_length_and_format(self):
        for value in ("ab", "가" * 21, " 가나 "):
            with self.subTest(value=value):
                self.assert_contract(self.signup(nickname=value), 422, "INVALID_NICKNAME_LENGTH", NICKNAME_LENGTH)
        for value in ("산책 왕", "산책-왕", "산책왕😀", "ㅅㅏㄴ", "walker.king", "산책왕!"):
            with self.subTest(value=value):
                self.assert_contract(self.signup(nickname=value), 422, "INVALID_NICKNAME_FORMAT", {"field": "nickname"})
        for value in ("abc", "가" * 20, "Walker_왕1", "___"):
            with self.subTest(value=value):
                self.assertEqual(self.client.get("/users/nickname-availability", params={"nickname": value}).status_code, 200)

    def test_email_format_and_length(self):
        too_long = "a" * 64 + "@" + "b" * 40 + ".com"
        for value in ("walker", "walker@", "@example.com", "walker@@example.com", "walker example@test.com", too_long):
            with self.subTest(value=value):
                self.assert_contract(self.signup(email=value), 422, "INVALID_EMAIL", {"field": "email"})

    def test_password_length_and_character_rules(self):
        for value in ("Walk2026!short", "Aa1!" + "a" * 125):
            with self.subTest(length=len(value)):
                self.assert_contract(self.signup(password=value), 422, "INVALID_PASSWORD_LENGTH", PASSWORD_LENGTH)
        for value, code, rule in (("walktogether2026!", "PASSWORD_UPPERCASE_REQUIRED", "uppercase"),
                                  ("WALKTOGETHER2026!", "PASSWORD_LOWERCASE_REQUIRED", "lowercase"),
                                  ("WalkTogetherNow!!", "PASSWORD_NUMBER_REQUIRED", "number"),
                                  ("WalkTogether2026 ", "PASSWORD_SPECIAL_REQUIRED", "special")):
            with self.subTest(code=code):
                self.assert_contract(self.signup(password=value), 422, code, {"field": "password", "rule": rule})
        for value in ("Walk2026!abcdef", "Aa1!" + "a" * 124):
            with self.subTest(length=len(value)):
                self.assertEqual(self.signup(password=value, login_id=f"len_{len(value)}",
                                             nickname=f"len_{len(value)}", email=f"len{len(value)}@example.com").status_code, 200)
        self.assertEqual(self.user_count(), 2)

    # Duplicates

    def test_duplicates_are_case_and_unicode_insensitive_and_leave_no_rows(self):
        self.assertEqual(self.signup(nickname="Walker_King").status_code, 200)
        cases = (
            ({"login_id": "WALKER04"}, "LOGIN_ID_ALREADY_EXISTS", "login_id"),
            ({"login_id": "other_id", "nickname": "walker_KING"}, "NICKNAME_ALREADY_EXISTS", "nickname"),
            ({"login_id": "other_id", "nickname": "other_nick", "email": "WALKER@example.com"},
             "EMAIL_ALREADY_EXISTS", "email"),
        )
        for changes, code, field in cases:
            with self.subTest(code=code):
                self.assert_contract(self.signup(**changes), 409, code, {"field": field})
        self.assertEqual(self.user_count(), 1)

    def test_nfc_equivalent_nickname_is_a_duplicate(self):
        self.assertEqual(self.signup().status_code, 200)
        response = self.signup(login_id="other_id", email="other@example.com",
                               nickname=unicodedata.normalize("NFD", "산책왕"))
        self.assert_contract(response, 409, "NICKNAME_ALREADY_EXISTS", {"field": "nickname"})

    def test_duplicate_fields_are_reported_in_request_order(self):
        self.assertEqual(self.signup().status_code, 200)
        self.assert_contract(self.signup(), 409, "LOGIN_ID_ALREADY_EXISTS", {"field": "login_id"})
        self.assert_contract(self.signup(login_id="other_id"), 409, "NICKNAME_ALREADY_EXISTS", {"field": "nickname"})

    def test_unique_index_violation_reports_the_conflicting_field_and_rolls_back(self):
        self.assertEqual(self.signup(nickname="Walker_King").status_code, 200)
        cases = (
            ({"login_id": "Walker04", "nickname": "n_one", "email": "one@example.com"}, "LOGIN_ID_ALREADY_EXISTS", "login_id"),
            ({"login_id": "id_two", "nickname": "WALKER_king", "email": "two@example.com"}, "NICKNAME_ALREADY_EXISTS", "nickname"),
            ({"login_id": "id_three", "nickname": "n_three", "email": "Walker@Example.com"}, "EMAIL_ALREADY_EXISTS", "email"),
        )
        # Skip the pre-check so only the database constraint can stop the insert.
        with patch("app.signup_service.first_taken_field", return_value=None):
            for changes, code, field in cases:
                with self.subTest(code=code):
                    self.assert_contract(self.signup(**changes), 409, code, {"field": field})
        self.assertEqual(self.user_count(), 1)
        self.assertEqual(self.signup(login_id="after_race", nickname="after_race", email="after@example.com").status_code, 200)

    def test_unrecognized_constraint_name_falls_back_to_a_fresh_lookup(self):
        self.assertEqual(self.signup().status_code, 200)
        # Pre-check misses the row; after the IntegrityError the second lookup finds it.
        with patch("app.signup_service.conflicted_field", return_value=None), \
                patch("app.signup_service.first_taken_field", side_effect=[None, "login_id"]) as lookup:
            self.assert_contract(self.signup(), 409, "LOGIN_ID_ALREADY_EXISTS", {"field": "login_id"})
        self.assertEqual(lookup.call_count, 2)

    def test_unexplained_integrity_error_is_500_without_leaking_values(self):
        self.assertEqual(self.signup().status_code, 200)
        # The insert really violates a unique index, but no field can be identified.
        with patch("app.signup_service.conflicted_field", return_value=None), \
                patch("app.signup_service.first_taken_field", return_value=None), \
                self.assertLogs("app.api_contract", "ERROR") as logs:
            response = self.signup()
        self.assert_contract(response, 500, "INTERNAL_SERVER_ERROR", {},
                             "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        output = response.text + "".join(logs.output)
        self.assertNotIn("scrypt$", output)
        self.assertNotIn(PASSWORD, output)
        self.assertIn("IntegrityError", output)
        self.assertEqual(self.user_count(), 1)

    def test_unexpected_failure_is_500_and_password_is_not_logged(self):
        with patch("app.signup_service.hash_password", side_effect=RuntimeError(f"boom {PASSWORD}")), \
                self.assertLogs("app.api_contract", "ERROR") as logs:
            response = self.signup()
        self.assert_contract(response, 500, "INTERNAL_SERVER_ERROR", {},
                             "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
        self.assertNotIn(PASSWORD, response.text + "".join(logs.output))
        self.assertEqual(self.user_count(), 0)

    # Availability checks

    def test_login_id_availability(self):
        response = self.client.get("/users/id-availability", params={"login_id": "walker04"})
        self.assert_contract(response, 200, "OK", {"available": True}, "사용 가능한 아이디입니다.")
        self.assertEqual(self.signup().status_code, 200)
        for value in ("walker04", " WALKER04 "):
            with self.subTest(value=value):
                response = self.client.get("/users/id-availability", params={"login_id": value})
                self.assert_contract(response, 200, "OK", {"available": False}, "이미 사용 중인 아이디입니다.")

    def test_nickname_availability(self):
        response = self.client.get("/users/nickname-availability", params={"nickname": "Walker_King"})
        self.assert_contract(response, 200, "OK", {"available": True}, "사용 가능한 닉네임입니다.")
        self.assertEqual(self.signup(nickname="Walker_King").status_code, 200)
        self.assertEqual(self.signup(login_id="second", nickname="산책왕", email="second@example.com").status_code, 200)
        for value in ("walker_king", " WALKER_KING ", unicodedata.normalize("NFD", "산책왕")):
            with self.subTest(value=value):
                response = self.client.get("/users/nickname-availability", params={"nickname": value})
                self.assert_contract(response, 200, "OK", {"available": False}, "이미 사용 중인 닉네임입니다.")

    def test_availability_validation_uses_the_signup_rules(self):
        cases = (
            ("/users/id-availability", {}, "LOGIN_ID_REQUIRED", {"field": "login_id"}),
            ("/users/id-availability", {"login_id": "  "}, "LOGIN_ID_REQUIRED", {"field": "login_id"}),
            ("/users/id-availability", {"login_id": "abc"}, "INVALID_LOGIN_ID_LENGTH", LOGIN_ID_LENGTH),
            ("/users/id-availability", {"login_id": "walker-04"}, "INVALID_LOGIN_ID_FORMAT", {"field": "login_id"}),
            ("/users/nickname-availability", {}, "NICKNAME_REQUIRED", {"field": "nickname"}),
            ("/users/nickname-availability", {"nickname": ""}, "NICKNAME_REQUIRED", {"field": "nickname"}),
            ("/users/nickname-availability", {"nickname": "ab"}, "INVALID_NICKNAME_LENGTH", NICKNAME_LENGTH),
            ("/users/nickname-availability", {"nickname": "산책 왕"}, "INVALID_NICKNAME_FORMAT", {"field": "nickname"}),
        )
        for path, params, code, data in cases:
            with self.subTest(path=path, params=params):
                self.assert_contract(self.client.get(path, params=params), 422, code, data)

    def test_availability_does_not_reserve_and_signup_rechecks(self):
        params = {"login_id": "walker04"}
        self.assertTrue(self.client.get("/users/id-availability", params=params).json()["data"]["available"])
        self.assertTrue(self.client.get("/users/nickname-availability", params={"nickname": "산책왕"}).json()["data"]["available"])
        # Someone else registers both values after the first person checked them.
        self.assertEqual(self.signup(email="first@example.com").status_code, 200)
        self.assert_contract(self.signup(email="second@example.com"), 409, "LOGIN_ID_ALREADY_EXISTS", {"field": "login_id"})
        self.assert_contract(self.signup(login_id="second_id", email="second@example.com"), 409,
                             "NICKNAME_ALREADY_EXISTS", {"field": "nickname"})
        self.assertEqual(self.user_count(), 1)

    def test_availability_is_public_and_does_not_clash_with_user_id_routes(self):
        self.assertEqual(self.signup().status_code, 200)
        self.client.headers.pop("Authorization", None)
        for path, params in (("/users/id-availability", {"login_id": "walker04"}),
                             ("/users/nickname-availability", {"nickname": "산책왕"})):
            with self.subTest(path=path):
                response = self.client.get(path, params=params)
                self.assertEqual(response.status_code, 200, response.text)
                # Only the availability flag: no user ID, email or owner details.
                self.assertEqual(response.json()["data"], {"available": False})
        # Other /users routes keep FastAPI's default error format.
        self.assertEqual(self.client.get("/users/1").json(), {"detail": "로그인이 필요하거나 세션이 만료되었습니다."})
        self.assertIn("detail", self.client.get("/users/not-a-number").json())
        login = self.client.post("/auth/login", json={"email": VALID["email"], "password": PASSWORD})
        self.client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        own_id = self.client.get("/users/me").json()["id"]
        self.assertEqual(self.client.get(f"/users/{own_id}").status_code, 200)

    # Schema and contract details

    def test_unique_indexes_match_migration_014(self):
        migration = (ROOT / "migrations" / "014_onboarding_signup.sql").read_text(encoding="utf-8")
        indexes = {index.name: index for index in User.__table__.indexes}
        for name, column in (("uq_users_login_id_lower", "login_id"), ("uq_users_nickname_lower", "nickname"),
                             ("uq_users_email_lower", "email")):
            with self.subTest(index=name):
                self.assertTrue(indexes[name].unique)
                ddl = str(CreateIndex(indexes[name]).compile(dialect=postgresql.dialect()))
                self.assertEqual(ddl, f"CREATE UNIQUE INDEX {name} ON users (lower({column}))")
                self.assertIn(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON users (lower({column}));", migration)
        columns = User.__table__.columns
        for name, length in (("login_id", 20), ("nickname", 20), ("email", 100), ("password", 255)):
            with self.subTest(column=name):
                self.assertFalse(columns[name].nullable)
                self.assertEqual(columns[name].type.length, length)
        self.assertTrue(columns["email"].unique)

    def test_existing_foreign_keys_to_users_are_kept(self):
        references = {(fk.parent.table.name, fk.parent.name) for table in Base.metadata.tables.values()
                      for fk in table.foreign_keys if fk.column.table.name == "users"}
        self.assertLessEqual({("session_tokens", "user_id"), ("auth_sessions", "user_id"), ("surveys", "user_id"),
                              ("matching_results", "user_id"), ("chat_sessions", "user_id"),
                              ("walk_requests", "user_id"), ("walk_requests", "reviewed_by")}, references)
        self.assertEqual([fk.target_fullname for fk in User.__table__.columns["shelter_id"].foreign_keys], ["shelters.id"])

    def test_database_unique_indexes_ignore_case(self):
        with self.Session.begin() as db:
            db.add(User(login_id="walker04", nickname="Walker", email="a@example.com", password=UNUSABLE_PASSWORD))
        for changes in ({"login_id": "WALKER04"}, {"nickname": "WALKER"}, {"email": "A@EXAMPLE.COM"}):
            with self.subTest(changes=changes):
                values = {"login_id": "other", "nickname": "other", "email": "b@example.com", **changes}
                with self.assertRaises(IntegrityError), self.Session.begin() as db:
                    db.add(User(**values, password=UNUSABLE_PASSWORD))
        self.assertEqual(self.user_count(), 1)

    def test_rate_limit_contract_shape(self):
        response = too_many_requests(30).response()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "30")
        self.assertEqual(response.body, '{"code":"TOO_MANY_REQUESTS","data":{"retry_after_seconds":30},'
                                        '"message":"요청이 너무 많습니다. 잠시 후 다시 시도해주세요."}'.encode())

    def test_openapi_documents_the_contract(self):
        app = FastAPI()
        app.include_router(users.router)
        paths = app.openapi()["paths"]
        self.assertEqual(set(paths["/users/"]["post"]["responses"]), {"200", "409", "422", "429", "500"})
        for path in ("/users/id-availability", "/users/nickname-availability"):
            self.assertEqual(set(paths[path]["get"]["responses"]), {"200", "422", "429", "500"})
        self.assertNotIn("409", paths["/users/id-availability"]["get"]["responses"])


if __name__ == "__main__":
    unittest.main()
