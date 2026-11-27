"""Onboarding Step 1 (Issue #40) on isolated SQLite with an injected clock and the mock
SMS provider. No real SMS is sent and no test waits for real time."""
from datetime import date, datetime, timedelta, timezone
import io
import logging
import os
import re
import unicodedata
import unittest
from unittest.mock import patch
import uuid

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import onboarding_common, pledge_service
    from app.account_status import change_to_age_review
    from app.auth import UNUSABLE_PASSWORD, token_digest
    from app.database import Base, get_db
    from app.models import (AuthSession, PhoneVerification, PhoneVerificationDailyCount, User,
                            UserPledgeAcknowledgement, UserRole, VolunteerOnboardingBasic)
    from app.onboarding_common import age_years, first_incomplete_step, get_clock
    from app.phone_verification_service import purge_phone_verifications
    from app.phone_verification_service import SmsDispatcher, get_sms_dispatcher
    from app.routers import auth, onboarding_basic, users
    from app.sms_provider import (MockSmsProvider, SolapiSmsProvider, configured_sms_provider, get_sms_provider,
                                  SmsProviderUnavailable)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Test-only settings. Real values are deployment secrets and undecided versions.
PLEDGE_VERSION = "pledge-test-v1"
TEST_ENV = {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION,
            "PHONE_VERIFICATION_HMAC_KEY": "test-only-hmac-key-0123456789abcdef",
            "SMS_MOCK_FIXED_CODE": ""}  # SMS_MOCK_FIXED_CODE: a shell or root .env value must not reach these tests (#128)
START = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)  # 10:00 in Asia/Seoul
PHONE, PHONE_E164 = "010-1234-5678", "+821012345678"
OTHER_PHONE, OTHER_E164 = "01098765432", "+821098765432"
SEND, CONFIRM, INFO = "/users/me/phone-verifications", "/users/me/phone-verifications/confirm", "/users/me/onboarding/basic-info"


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class Step1TestCase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.clock = FakeClock(START)
        self.sms = MockSmsProvider()
        app = FastAPI()
        for router in (auth.router, users.router, onboarding_basic.router):
            app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_clock] = lambda: self.clock
        app.dependency_overrides[get_sms_provider] = lambda: self.sms
        self.dispatcher = SmsDispatcher(inline=True)  # delivery runs inside the request (#110)
        app.dependency_overrides[get_sms_dispatcher] = lambda: self.dispatcher
        for limiter in (users.SIGNUP_RATE_LIMITER, users.AVAILABILITY_RATE_LIMITER, auth.LOGIN_RATE_LIMITER):
            app.dependency_overrides[limiter] = lambda: None
        self.app = app
        self.client = TestClient(app)
        with self.Session.begin() as db:
            db.add_all([
                User(id=1, login_id="walker01", nickname="봉사자1", password=UNUSABLE_PASSWORD),
                User(id=2, login_id="walker02", nickname="봉사자2", password=UNUSABLE_PASSWORD),
                User(id=3, login_id="walker03", nickname="미서약", password=UNUSABLE_PASSWORD),
                User(id=4, login_id="shelter01", nickname="보호소", password=UNUSABLE_PASSWORD, role=UserRole.SHELTER),
                User(id=5, login_id="admin01", nickname="관리자", password=UNUSABLE_PASSWORD, role=UserRole.ADMIN),
            ])
            db.flush()
            for user_id in (1, 2, 3, 4, 5):
                db.add(AuthSession(token_hash=token_digest(f"token-{user_id}"), user_id=user_id,
                                   expires_at=START + timedelta(days=30)))
            for user_id in (1, 2):
                db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION,
                                                 acknowledged_at=START - timedelta(minutes=5)))

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def call(self, method, path, user_id=1, **kwargs):
        headers = {"Authorization": f"Bearer token-{user_id}"} if user_id else {}
        return self.client.request(method, path, headers=headers, **kwargs)

    def send(self, phone=PHONE, user_id=1):
        return self.call("POST", SEND, user_id, json={"phone_number": phone})

    def last_code(self):
        return re.search(r"\d{6}", self.sms.outbox[-1]["text"]).group()

    def confirm(self, verification_id, code, user_id=1):
        return self.call("POST", CONFIRM, user_id, json={"verification_id": verification_id, "code": code})

    def save(self, verification_id, user_id=1, full_name="김민지", birth_date="2002-12-10"):
        return self.call("PUT", INFO, user_id, json={"full_name": full_name, "birth_date": birth_date,
                                                     "phone_verification_id": verification_id})

    def verified(self, phone=PHONE, user_id=1):
        response = self.send(phone, user_id)
        self.assertEqual(response.status_code, 200, response.text)
        verification_id = response.json()["data"]["verification_id"]
        self.assertEqual(self.confirm(verification_id, self.last_code(), user_id).status_code, 200)
        return verification_id

    def assert_error(self, response, status, code, data=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["code"], code, response.text)
        if data is not None:
            self.assertEqual(response.json()["data"], data)

    def rows(self, model):
        with self.Session() as db:
            return db.query(model).all()

    def user(self, user_id=1):
        with self.Session() as db:
            return db.get(User, user_id)


class AccessOrderTests(Step1TestCase):
    def all_routes(self, user_id):
        bad = str(uuid.uuid4())
        return [self.call("GET", INFO, user_id), self.send(user_id=user_id),
                self.confirm(bad, "123456", user_id), self.save(bad, user_id)]

    def test_pledge_is_required_before_any_step1_api_and_nothing_is_sent_or_stored(self):
        for response in self.all_routes(3):
            self.assert_error(response, 409, "PREVIOUS_STEP_NOT_COMPLETED", {"required_step": "PLEDGE"})
        self.assertEqual((self.sms.outbox, self.rows(PhoneVerification), self.rows(PhoneVerificationDailyCount)),
                         ([], [], []))

    def test_pledge_for_an_older_version_only_is_not_enough(self):
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "pledge-test-v2"}):
            self.assert_error(self.send(), 409, "PREVIOUS_STEP_NOT_COMPLETED", {"required_step": "PLEDGE"})
        self.assertEqual(self.sms.outbox, [])

    def test_shelter_admin_and_missing_tokens(self):
        for user_id in (4, 5):
            for response in self.all_routes(user_id):
                self.assert_error(response, 403, "ONBOARDING_NOT_ALLOWED", {})
        for response in self.all_routes(None):
            self.assert_error(response, 401, "AUTHENTICATION_REQUIRED", {})

    def test_age_review_account_is_blocked_before_the_pledge_check(self):
        with self.Session() as db:
            change_to_age_review(db, 3, START)
        for response in self.all_routes(3):  # user 3 has no pledge: status still comes first
            self.assert_error(response, 403, "AGE_REVIEW_REQUIRED", {})


class FlowTests(Step1TestCase):
    def test_send_confirm_save_and_read(self):
        before = self.call("GET", INFO)
        self.assertEqual(before.json()["data"], {"step1_completed": False, "profile": None,
                                                 "next_step": "BASIC_INFORMATION"})
        self.assertEqual(before.headers["cache-control"], "no-store")
        sent = self.send()
        self.assertEqual(sent.status_code, 200, sent.text)
        data = sent.json()["data"]
        self.assertEqual((data["expires_in_seconds"], data["resend_after_seconds"]), (300, 60))
        code = self.last_code()
        self.assertEqual(self.sms.outbox[-1]["to"], PHONE_E164)
        self.assertNotIn(code, sent.text)
        self.assertNotIn("5678", sent.text)
        self.clock.advance(seconds=30)
        confirmed = self.confirm(data["verification_id"], code)
        self.assertEqual(confirmed.json()["data"], {"verified": True, "verified_at": self.clock.now.isoformat()})
        self.clock.advance(minutes=2)
        saved = self.save(data["verification_id"], full_name=" 김 민지 ")
        self.assertEqual(saved.status_code, 200, saved.text)
        body = saved.json()["data"]
        self.assertEqual(body, {"user_id": 1, "full_name": "김 민지", "birth_date": "2002-12-10", "age_years": 23,
                                "phone_masked": "010-****-5678", "phone_verified_at": (START + timedelta(seconds=30)).isoformat(),
                                "step1_completed": True, "completed_at": self.clock.now.isoformat(),
                                "next_step": "PET_EXPERIENCE"})
        after = self.call("GET", INFO).json()["data"]
        self.assertEqual(after["profile"], {k: body[k] for k in ("full_name", "birth_date", "age_years", "phone_masked",
                                                                  "phone_verified_at", "completed_at")})
        (basic,), (verification,) = self.rows(VolunteerOnboardingBasic), self.rows(PhoneVerification)
        self.assertEqual((basic.user_id, basic.phone_e164), (1, PHONE_E164))
        self.assertIsNotNone(verification.consumed_at)
        self.assertNotIn(code, verification.code_digest)
        self.assertEqual(len(verification.code_digest), 64)
        self.assertIsNone(self.user().phone)  # users.phone (#28) is left alone
        with self.Session() as db:
            self.assertEqual(first_incomplete_step(db, db.get(User, 1)), "PET_EXPERIENCE")

    def test_save_replays_same_input_and_refuses_different_input(self):
        verification_id = self.verified()
        first = self.save(verification_id).json()["data"]
        self.clock.advance(hours=1)
        again = self.save(verification_id)
        self.assertEqual((again.status_code, again.json()["data"]["completed_at"]), (200, first["completed_at"]))
        self.assert_error(self.save(verification_id, full_name="다른이름"), 409, "BASIC_INFO_ALREADY_COMPLETED", {})
        # Even an under-14 date on a completed account is just a mismatch: no status change.
        self.assert_error(self.save(verification_id, birth_date="2020-01-01"), 409, "BASIC_INFO_ALREADY_COMPLETED")
        self.assertEqual(self.user().account_status, "ACTIVE")
        self.assertEqual(len(self.rows(VolunteerOnboardingBasic)), 1)

    def test_verification_success_alone_does_not_complete_step1(self):
        self.verified()
        self.assertFalse(self.call("GET", INFO).json()["data"]["step1_completed"])
        self.assertEqual(self.rows(VolunteerOnboardingBasic), [])


class PhoneRuleTests(Step1TestCase):
    def test_phone_number_rules(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                response = self.call("POST", SEND, json={} if value is None else {"phone_number": value})
                self.assert_error(response, 422, "PHONE_NUMBER_REQUIRED", {"field": "phone_number"})
        for value in ("011-1234-5678", "+821012345678", "010123456789", "0101234567", "010-12-345678",
                      "010 1234 5678", "abc", 1012345678, "０１０１２３４５６７８"):
            with self.subTest(value=value):
                self.assert_error(self.send(value), 422, "INVALID_PHONE_NUMBER", {"field": "phone_number"})
        self.assertEqual(self.rows(PhoneVerification), [])
        self.assertEqual(self.send("01012345678").status_code, 200)
        self.assertEqual(self.sms.outbox[-1]["to"], PHONE_E164)

    def test_number_registered_by_another_account_is_refused(self):
        # Issue #110: the send answers like any other (no disclosure); the request is
        # SUPPRESSED, nothing is sent, and no code can verify it or save Step 1.
        self.save(self.verified(PHONE, user_id=2), user_id=2)
        self.clock.advance(seconds=61)
        sent_before = len(self.sms.outbox)
        with patch("app.phone_verification_service.secrets.randbelow", return_value=123456):
            response = self.send(PHONE, user_id=1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()["data"]), {"verification_id", "expires_in_seconds", "resend_after_seconds"})
        self.assertEqual(len(self.sms.outbox), sent_before)
        verification_id = response.json()["data"]["verification_id"]
        with self.Session() as db:
            self.assertEqual(db.get(PhoneVerification, uuid.UUID(verification_id)).send_status, "SUPPRESSED")
        self.assert_error(self.confirm(verification_id, "123456", user_id=1), 422, "VERIFICATION_CODE_INVALID")
        self.assert_error(self.save(verification_id, user_id=1), 409, "PHONE_VERIFICATION_REQUIRED")


class TimingTests(Step1TestCase):
    def test_code_expires_after_300_seconds(self):
        data = self.send().json()["data"]
        code = self.last_code()
        self.clock.advance(seconds=300)
        self.assert_error(self.confirm(data["verification_id"], code), 410, "VERIFICATION_EXPIRED", {})
        self.clock.advance(seconds=1)
        data = self.send().json()["data"]
        self.clock.advance(seconds=299)
        self.assertEqual(self.confirm(data["verification_id"], self.last_code()).status_code, 200)

    def test_resend_wait_is_60_seconds_and_replaces_the_previous_request(self):
        first = self.send().json()["data"]["verification_id"]
        first_code = self.last_code()
        self.clock.advance(seconds=59)
        response = self.send()
        self.assert_error(response, 429, "PHONE_VERIFICATION_RATE_LIMITED", {"retry_after_seconds": 1})
        self.assertEqual(response.headers["Retry-After"], "1")
        self.clock.advance(seconds=1)
        second = self.send().json()["data"]["verification_id"]
        self.assert_error(self.confirm(first, first_code), 410, "VERIFICATION_EXPIRED")
        self.assertEqual(self.confirm(second, self.last_code()).status_code, 200)
        self.assertEqual(len(self.sms.outbox), 2)

    def test_verified_request_can_be_saved_for_15_minutes_only(self):
        verification_id = self.verified()
        self.clock.advance(minutes=15)
        self.assert_error(self.save(verification_id), 409, "PHONE_VERIFICATION_REQUIRED",
                          {"field": "phone_verification_id"})
        verification_id = self.verified(user_id=2, phone=OTHER_PHONE)
        self.clock.advance(minutes=15, seconds=-1)
        self.assertEqual(self.save(verification_id, user_id=2).status_code, 200)

    def test_a_verified_request_replaced_by_a_newer_one_cannot_be_saved(self):
        old = self.verified()
        self.clock.advance(seconds=61)
        self.send(OTHER_PHONE)
        self.assert_error(self.save(old), 409, "PHONE_VERIFICATION_REQUIRED")


class AttemptTests(Step1TestCase):
    def test_five_failures_then_blocked_even_with_the_right_code(self):
        verification_id = self.send().json()["data"]["verification_id"]
        code = self.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assert_error(self.confirm(verification_id, wrong), 422, "VERIFICATION_CODE_INVALID", {"field": "code"})
        self.assert_error(self.confirm(verification_id, code), 429, "VERIFICATION_ATTEMPTS_EXCEEDED", {})
        (row,) = self.rows(PhoneVerification)
        self.assertEqual((row.attempt_count, row.verified_at), (5, None))

    def test_success_retry_returns_the_same_time_without_counting_a_failure(self):
        verification_id = self.send().json()["data"]["verification_id"]
        code = self.last_code()
        first = self.confirm(verification_id, code).json()["data"]
        self.clock.advance(seconds=20)
        self.assertEqual(self.confirm(verification_id, code).json()["data"], first)
        wrong = "000000" if code != "000000" else "111111"
        self.assert_error(self.confirm(verification_id, wrong), 422, "VERIFICATION_CODE_INVALID")
        self.assertEqual(self.rows(PhoneVerification)[0].attempt_count, 0)

    def test_payload_owner_and_consumed_checks(self):
        verification_id = self.send().json()["data"]["verification_id"]
        code = self.last_code()
        for body, field in (({"verification_id": "not-a-uuid", "code": "123456"}, "verification_id"),
                            ({"verification_id": verification_id, "code": "12345"}, "code"),
                            ({"verification_id": verification_id, "code": "１２３４５６"}, "code"),
                            ({"verification_id": verification_id, "code": 123456}, "code"),
                            ({"verification_id": verification_id}, "code")):
            with self.subTest(body=body):
                self.assert_error(self.call("POST", CONFIRM, json=body), 422, "INVALID_VERIFICATION_PAYLOAD",
                                  {"field": field})
        self.assert_error(self.confirm(str(uuid.uuid4()), code), 404, "VERIFICATION_NOT_FOUND", {})
        self.assert_error(self.confirm(verification_id, code, user_id=2), 404, "VERIFICATION_NOT_FOUND", {})
        self.assertEqual(self.confirm(verification_id, code).status_code, 200)
        self.assertEqual(self.save(verification_id).status_code, 200)
        self.assert_error(self.confirm(verification_id, code), 409, "VERIFICATION_ALREADY_CONSUMED", {})
        self.assert_error(self.save(verification_id, user_id=2), 404, "VERIFICATION_NOT_FOUND")


class DailyLimitTests(Step1TestCase):
    def test_five_sends_per_account_and_reset_at_kst_midnight(self):
        self.clock.now = datetime(2026, 11, 23, 14, 50, tzinfo=timezone.utc)  # 23:50 KST
        for index in range(5):
            self.assertEqual(self.send(PHONE if index % 2 else OTHER_PHONE).status_code, 200)
            self.clock.advance(seconds=61)
        response = self.send()
        seconds = int((datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc) - self.clock.now).total_seconds())
        self.assert_error(response, 429, "PHONE_VERIFICATION_RATE_LIMITED", {"retry_after_seconds": seconds})
        self.assertEqual(len(self.sms.outbox), 5)
        self.clock.now = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # 00:00 KST next day
        self.assertEqual(self.send().status_code, 200)

    def test_five_sends_per_number_across_accounts(self):
        for index in range(5):
            self.assertEqual(self.send(PHONE, user_id=1 if index < 3 else 2).status_code, 200)
            self.clock.advance(seconds=61)
        self.assert_error(self.send(PHONE, user_id=2), 429, "PHONE_VERIFICATION_RATE_LIMITED")
        self.assertEqual(self.send(OTHER_PHONE, user_id=2).status_code, 200)  # account 2 used only 2
        with self.Session() as db:
            counts = {(row.scope, row.send_count) for row in db.query(PhoneVerificationDailyCount)}
            subjects = [row.subject for row in db.query(PhoneVerificationDailyCount)]
        self.assertIn(("PHONE", 5), counts)
        self.assertFalse(any("1012345678" in subject for subject in subjects))


class ProviderTests(Step1TestCase):
    def test_definite_failure_is_503_failed_and_cannot_be_confirmed(self):
        # Issue #110: the send was accepted (200); the provider's refusal is internal only.
        self.sms.next_outcome = ["failed"]
        self.assertEqual(self.send().status_code, 200)
        (row,) = self.rows(PhoneVerification)
        self.assertEqual(row.send_status, "FAILED")
        self.assert_error(self.confirm(str(row.id), "123456"), 422, "VERIFICATION_CODE_INVALID")  # same as a wrong code
        self.assertEqual(self.rows(PhoneVerification)[0].attempt_count, 1)

    def test_timeout_is_unknown_never_resent_and_settled_by_lookup(self):
        log = io.StringIO()
        handler = logging.StreamHandler(log)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.sms.next_outcome = ["timeout"]
        with patch("app.phone_verification_service.secrets.randbelow", return_value=23841):
            self.assertEqual(self.send().status_code, 200)  # accepted; the outcome is internal (#110)
        (row,) = self.rows(PhoneVerification)
        self.assertEqual((row.send_status, self.sms.outbox), ("UNKNOWN", []))
        verification_id = str(row.id)
        # A wrong code is the usual 422; the matching code proves delivery and verifies.
        self.assert_error(self.confirm(verification_id, "111111"), 422, "VERIFICATION_CODE_INVALID")
        self.assertEqual(self.confirm(verification_id, "023841").status_code, 200)
        self.assertEqual(self.sms.lookups, [])  # confirm never asks the provider (#110)
        self.assertEqual(len(self.rows(PhoneVerification)), 1)  # nothing was resent
        output = log.getvalue()
        for secret in ("023841", "1234", "5678", PHONE_E164):
            self.assertNotIn(secret, output)
        self.assertIn("SmsProviderTimeout", output)

    def test_lookup_reporting_failure_and_connection_errors(self):
        # Issue #110: no lookup any more; a connection error leaves UNKNOWN and a wrong code is 422.
        self.sms.next_outcome = ["error"]
        self.assertEqual(self.send().status_code, 200)
        (row,) = self.rows(PhoneVerification)
        self.assertEqual(row.send_status, "UNKNOWN")
        self.sms.lookup_results[str(row.id)] = False
        self.assert_error(self.confirm(str(row.id), "123456"), 422, "VERIFICATION_CODE_INVALID")
        self.assertEqual((self.rows(PhoneVerification)[0].send_status, self.sms.lookups), ("UNKNOWN", []))

    def test_unknown_and_failed_sends_count_toward_the_daily_limit(self):
        self.sms.next_outcome = ["timeout", "failed", "accepted", "error", "accepted"]
        for _ in range(5):
            self.send()
            self.clock.advance(seconds=61)
        self.assert_error(self.send(), 429, "PHONE_VERIFICATION_RATE_LIMITED")

    def test_no_provider_or_no_hmac_key_sends_and_stores_nothing(self):
        self.app.dependency_overrides[get_sms_provider] = lambda: None
        self.assert_error(self.send(), 503, "SMS_DELIVERY_UNAVAILABLE", {})
        self.app.dependency_overrides[get_sms_provider] = lambda: self.sms
        env = {k: v for k, v in os.environ.items() if k != "PHONE_VERIFICATION_HMAC_KEY"}
        with patch.dict(os.environ, env, clear=True), self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.send(), 500, "INTERNAL_SERVER_ERROR")
        with patch.dict(os.environ, {"PHONE_VERIFICATION_HMAC_KEY": "short"}), self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.send(), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual((self.rows(PhoneVerification), self.rows(PhoneVerificationDailyCount), self.sms.outbox),
                         ([], [], []))

    def test_mock_provider_is_refused_outside_local_development_and_test(self):
        for app_env in (None, "", "production", "staging"):
            env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
            env["SMS_PROVIDER"] = "mock"
            if app_env is not None:
                env["APP_ENV"] = app_env
            with self.subTest(app_env=app_env), patch.dict(os.environ, env, clear=True):
                self.assertRaises(SmsProviderUnavailable, configured_sms_provider)
                self.assertIsNone(get_sms_provider())
        for app_env in ("local", "development", "test"):
            with patch.dict(os.environ, {"SMS_PROVIDER": "mock", "APP_ENV": app_env}):
                self.assertIsInstance(configured_sms_provider(), MockSmsProvider)
        with patch.dict(os.environ, {"SMS_PROVIDER": "solapi", "SOLAPI_API_KEY": "k", "SOLAPI_API_SECRET": ""}):
            self.assertRaises(SmsProviderUnavailable, configured_sms_provider)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_sms_provider())

    def test_solapi_placeholder_never_reports_a_send(self):
        self.app.dependency_overrides[get_sms_provider] = lambda: SolapiSmsProvider("k", "s", "01000000000")
        self.assertEqual(self.send().status_code, 200)  # accepted (#110); the placeholder never sends
        self.assertEqual(self.rows(PhoneVerification)[0].send_status, "FAILED")


class AgeTests(Step1TestCase):
    def test_age_function_with_29_february(self):
        birth = date(2000, 2, 29)
        for today, expected in ((date(2025, 2, 28), 24), (date(2025, 3, 1), 25),
                                (date(2024, 2, 28), 23), (date(2024, 2, 29), 24)):
            with self.subTest(today=today):
                self.assertEqual(age_years(birth, today), expected)
        self.assertEqual((age_years(date(2012, 11, 23), date(2026, 11, 22)),
                          age_years(date(2012, 11, 23), date(2026, 11, 23))), (13, 14))

    def test_age_uses_the_asia_seoul_date(self):
        self.clock.now = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)  # 2026-11-23 00:00 KST
        self.assertEqual(self.save(self.verified(), birth_date="2012-11-23").status_code, 200)  # 14 today in KST

    def test_14_and_120_boundaries(self):
        today = date(2026, 11, 23)  # START in KST
        self.assertEqual(self.save(self.verified(), birth_date="2012-11-23").json()["data"]["age_years"], 14)
        self.clock.advance(seconds=61)
        self.assertEqual(self.save(self.verified(OTHER_PHONE, 2), user_id=2, birth_date="1906-11-24").json()["data"]
                         ["age_years"], 119)
        self.assertEqual(age_years(date(1906, 11, 23), today), 120)

    def test_over_120_is_422_and_the_account_stays_active(self):
        verification_id = self.verified()
        # On 2026-11-23 (KST) a 1905-11-23 birth date is 121 and 1905-11-24 is still 120.
        self.assert_error(self.save(verification_id, birth_date="1905-11-23"), 422, "AGE_ABOVE_MAXIMUM",
                          {"field": "birth_date", "maximum_age": 120})
        self.assertEqual(self.user().account_status, "ACTIVE")
        saved = self.save(verification_id, birth_date="1905-11-24")
        self.assertEqual((saved.status_code, saved.json()["data"]["age_years"]), (200, 120))

    def test_under_14_switches_status_in_its_own_commit_and_keeps_the_pledge(self):
        verification_id = self.verified()
        self.clock.advance(minutes=1)
        response = self.save(verification_id, birth_date="2012-11-24")  # 14 tomorrow
        self.assert_error(response, 422, "AGE_BELOW_MINIMUM", {"field": "birth_date", "minimum_age": 14})
        user = self.user()
        self.assertEqual((user.account_status, user.age_review_required_at.replace(tzinfo=timezone.utc)),
                         ("AGE_REVIEW_REQUIRED", self.clock.now))
        self.assertEqual(self.rows(VolunteerOnboardingBasic), [])
        self.assertIsNone(self.rows(PhoneVerification)[0].consumed_at)
        self.assertEqual(len(self.rows(UserPledgeAcknowledgement)), 2)  # pledge history is kept
        for response in (self.call("GET", INFO), self.send(), self.save(verification_id)):
            self.assert_error(response, 403, "AGE_REVIEW_REQUIRED", {})
        pledge = self.call("PUT", "/users/me/pledge", json={"pledge_version": PLEDGE_VERSION})
        self.assert_error(pledge, 403, "AGE_REVIEW_REQUIRED", {})
        with self.Session() as db:
            self.assertEqual(first_incomplete_step(db, db.get(User, 1)), "AGE_REVIEW")

    def test_status_change_survives_a_later_failure_in_the_same_request(self):
        verification_id = self.verified()
        with patch("app.basic_info_service.ContractError", side_effect=RuntimeError("fail after commit")), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.save(verification_id, birth_date="2020-01-01"), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.user().account_status, "AGE_REVIEW_REQUIRED")

    def test_name_and_birth_date_rules(self):
        verification_id = self.verified()
        cases = [({"full_name": None}, 422, "REQUIRED_FIELD", {"field": "full_name"}),
                 ({"full_name": "  "}, 422, "REQUIRED_FIELD", {"field": "full_name"}),
                 ({"full_name": "김"}, 422, "INVALID_FULL_NAME", {"field": "full_name"}),
                 ({"full_name": "가" * 11}, 422, "INVALID_FULL_NAME", {"field": "full_name"}),
                 ({"full_name": "김\n민지"}, 422, "INVALID_FULL_NAME", {"field": "full_name"}),
                 ({"birth_date": None}, 422, "REQUIRED_FIELD", {"field": "birth_date"}),
                 ({"birth_date": "2002.12.10"}, 422, "INVALID_BIRTH_DATE", {"field": "birth_date"}),
                 ({"birth_date": "2002-02-30"}, 422, "INVALID_BIRTH_DATE", {"field": "birth_date"}),
                 ({"birth_date": "02-12-10"}, 422, "INVALID_BIRTH_DATE", {"field": "birth_date"}),
                 ({"birth_date": "2026-11-24"}, 422, "INVALID_BIRTH_DATE", {"field": "birth_date"}),
                 ({"phone_verification_id": None}, 422, "REQUIRED_FIELD", {"field": "phone_verification_id"}),
                 ({"phone_verification_id": "not-a-uuid"}, 404, "VERIFICATION_NOT_FOUND", {})]
        base = {"full_name": "김민지", "birth_date": "2002-12-10", "phone_verification_id": verification_id}
        for changes, status, code, data in cases:
            with self.subTest(changes=changes):
                self.assert_error(self.call("PUT", INFO, json={**base, **changes}), status, code, data)
        self.assertEqual(self.user().account_status, "ACTIVE")
        saved = self.save(verification_id, full_name=unicodedata.normalize("NFD", "김민지"))
        self.assertEqual(saved.json()["data"]["full_name"], "김민지")


class RollbackTests(Step1TestCase):
    def test_failed_commit_rolls_back_basic_info_and_consumption(self):
        verification_id = self.verified()
        original = Session.commit
        state = {"failed": False}

        def failing_commit(session):
            if not state["failed"] and session.new:
                state["failed"] = True
                raise OperationalError("COMMIT", {}, Exception("disk full"))
            return original(session)

        with patch.object(Session, "commit", failing_commit), self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.save(verification_id), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.rows(VolunteerOnboardingBasic), [])
        self.assertIsNone(self.rows(PhoneVerification)[0].consumed_at)
        self.assertEqual(self.save(verification_id).status_code, 200)


class PledgeConsistencyTests(Step1TestCase):
    def test_status_change_after_the_request_check_is_caught_on_the_locked_row(self):
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "pledge-test-v2"}):
            original = pledge_service.lock_user

            def change_then_lock(db, user_id, **kwargs):
                # Another request switches the account between the route check and the lock.
                with self.Session() as other:
                    change_to_age_review(other, user_id, START)
                return original(db, user_id, **kwargs)

            with patch("app.pledge_service.lock_user", side_effect=change_then_lock):
                response = self.call("PUT", "/users/me/pledge", json={"pledge_version": "pledge-test-v2"})
        self.assert_error(response, 403, "AGE_REVIEW_REQUIRED", {})
        versions = [row.pledge_version for row in self.rows(UserPledgeAcknowledgement) if row.user_id == 1]
        self.assertEqual(versions, [PLEDGE_VERSION])


class StepFunctionTests(Step1TestCase):
    def test_first_incomplete_step(self):
        with self.Session() as db:
            self.assertEqual(first_incomplete_step(db, db.get(User, 3)), "PLEDGE")
            self.assertEqual(first_incomplete_step(db, db.get(User, 1)), "BASIC_INFORMATION")
            self.assertIsNone(first_incomplete_step(db, db.get(User, 4)))
            self.assertIsNone(first_incomplete_step(db, db.get(User, 5)))


class PurgeTests(Step1TestCase):
    def test_purge_removes_only_unusable_requests_and_old_counters(self):
        consumed = self.verified()
        self.save(consumed)
        self.clock.advance(seconds=61)
        self.send(OTHER_PHONE, user_id=2)  # expires at +300 s, never verified
        expired_id = self.rows(PhoneVerification)[-1].id
        with self.Session.begin() as db:
            db.add(PhoneVerificationDailyCount(scope="USER", subject="9", kst_date=date(2026, 11, 20), send_count=1))
        self.clock.advance(seconds=301)
        live = self.send(OTHER_PHONE, user_id=2).json()["data"]["verification_id"]
        with self.Session() as db:
            dry = purge_phone_verifications(db, self.clock.now, apply=False)
        self.assertEqual(dry, {"phone_verifications": 2, "phone_verification_daily_counts": 1})
        self.assertEqual(len(self.rows(PhoneVerification)), 3)
        with self.Session() as db:
            purge_phone_verifications(db, self.clock.now, apply=True)
        self.assertEqual([str(row.id) for row in self.rows(PhoneVerification)], [live])
        self.assertNotIn(expired_id, [row.id for row in self.rows(PhoneVerification)])
        self.assertEqual(len(self.rows(VolunteerOnboardingBasic)), 1)


if __name__ == "__main__":
    unittest.main()
