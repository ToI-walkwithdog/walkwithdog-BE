"""Issue #124: the mock SMS test environment with a fixed code (SMS_MOCK_FIXED_CODE).

Isolated SQLite with the injected clock, a recording mock provider and an inline delivery
dispatcher (PrivacyTestCase). The fixed code here is a synthetic test value; the real setting
is never written anywhere. A fixed code proves nothing about owning a number, so it only works
for the internal synthetic test numbers 010-0000-0000 ~ 010-0000-9999. Every #110 rule
(asynchronous delivery, SUPPRESSED, limits, expiry, attempts, latest request, purposes) holds."""
from contextlib import contextmanager
import io
import logging
import os
from unittest.mock import patch

from test_onboarding_basic_info import PHONE, PLEDGE_VERSION, START, TEST_ENV
from test_phone_verification_privacy import FREE_PHONE, PrivacyTestCase
from test_user_profile import THIRD_PHONE
from app.models import (PhoneVerification, PhoneVerificationDailyCount, User, UserPledgeAcknowledgement,
                        VolunteerOnboardingBasic)
from app.auth import UNUSABLE_PASSWORD
from app.phone_verification_service import SmsDispatcher, get_sms_dispatcher
from app.sms_provider import (MockSmsProvider, SmsProviderUnavailable, _mock_singleton, configured_sms_provider,
                              get_sms_provider, is_synthetic_test_number, mock_fixed_code)

SYNTHETIC_CODE = "246810"  # test-only value; never the configured one
FIXED_ENV = {"SMS_PROVIDER": "mock", "APP_ENV": "test", "SMS_MOCK_FIXED_CODE": SYNTHETIC_CODE}
TEST_FREE, TEST_FREE_E164 = "010-0000-1001", "+821000001001"
TEST_OTHER, TEST_OTHER_E164 = "010-0000-1002", "+821000001002"
TEST_REGISTERED, TEST_REGISTERED_E164 = "010-0000-9001", "+821000009001"  # owned by user 6


@contextmanager
def environment(**changes):
    """Replace settings for the block: a None value removes that setting."""
    env = {name: value for name, value in os.environ.items() if name not in changes}
    env.update({name: value for name, value in changes.items() if value is not None})
    with patch.dict(os.environ, env, clear=True):
        yield


class HeldDispatcher(SmsDispatcher):
    """Keeps submitted jobs until run_held(): the request answers while the row is still PENDING."""

    def __init__(self):
        super().__init__(inline=True)
        self.held = []

    def submit(self, job, slot):
        self.held.append((job, slot))

    def run_held(self):
        while self.held:
            job, slot = self.held.pop(0)
            try:
                job()
            finally:
                slot.release()


class MockEnvironmentTestCase(PrivacyTestCase):
    def setUp(self):
        super().setUp()  # random codes: user 1 owns PHONE, user 2 owns THIRD_PHONE (outside the range)
        with self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=3, pledge_version=PLEDGE_VERSION, acknowledged_at=START))
            db.add(User(id=6, login_id="walker06", nickname="봉사자6", password=UNUSABLE_PASSWORD))
            db.flush()
            db.add(VolunteerOnboardingBasic(user_id=6, full_name="테스트", birth_date=START.date().replace(year=2000),
                                            phone_e164=TEST_REGISTERED_E164, phone_verified_at=START,
                                            completed_at=START, updated_at=START))
        env = patch.dict(os.environ, FIXED_ENV)
        env.start()
        self.addCleanup(env.stop)

    def nothing_written(self, rows, outbox):
        self.assertEqual(len(self.rows(PhoneVerification)), rows)
        self.assertEqual(self.counters(), [])
        self.assertEqual(len(self.sms.outbox), outbox)
        self.assertEqual(self.dispatcher.available(), self.dispatcher.capacity)

    def attempts(self, verification_id):
        return self.proof(verification_id).attempt_count

    def sent_id(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]["verification_id"]


class SettingTests(MockEnvironmentTestCase):
    def test_the_fixed_code_applies_only_with_the_mock_in_an_allowed_environment(self):
        self.assertEqual(mock_fixed_code(), SYNTHETIC_CODE)
        self.assertIs(configured_sms_provider(), _mock_singleton)
        for app_env in ("local", "development", "test"):
            with self.subTest(app_env=app_env), environment(APP_ENV=app_env):
                self.assertEqual(mock_fixed_code(), SYNTHETIC_CODE)
        for value in (None, ""):
            with self.subTest(value=value), environment(SMS_MOCK_FIXED_CODE=value):
                self.assertIsNone(mock_fixed_code())
                self.assertIs(configured_sms_provider(), _mock_singleton)

    def test_unusable_combinations_answer_503_and_write_nothing(self):
        solapi = {"SMS_PROVIDER": "solapi", "SOLAPI_API_KEY": "k", "SOLAPI_API_SECRET": "s",
                  "SOLAPI_SENDER_NUMBER": "01000000000"}
        cases = {
            "solapi provider": solapi,
            "no provider": {"SMS_PROVIDER": None},
            "production": {"APP_ENV": "production"},
            "staging": {"APP_ENV": "staging"},
            "no environment": {"APP_ENV": None},
            "5 digits": {"SMS_MOCK_FIXED_CODE": "24681"},
            "7 digits": {"SMS_MOCK_FIXED_CODE": "2468101"},
            "leading space": {"SMS_MOCK_FIXED_CODE": " 246810"},
            "trailing space": {"SMS_MOCK_FIXED_CODE": "246810 "},
            "only a space": {"SMS_MOCK_FIXED_CODE": " "},
            "letters": {"SMS_MOCK_FIXED_CODE": "24681a"},
            "full-width digits": {"SMS_MOCK_FIXED_CODE": "２４６８１０"},
            "Arabic-Indic digits": {"SMS_MOCK_FIXED_CODE": "٢٤٦٨١٠"},
        }
        rows, outbox = len(self.rows(PhoneVerification)), len(self.sms.outbox)
        for label, changes in cases.items():
            with self.subTest(case=label), environment(**changes):
                self.assertRaises(SmsProviderUnavailable, mock_fixed_code)
                self.assertRaises(SmsProviderUnavailable, configured_sms_provider)
                self.assertIsNone(get_sms_provider())
                # The injected test provider is still there: the setting alone blocks sending.
                for send in (self.send, self.profile_send):
                    for phone in (TEST_FREE, TEST_REGISTERED, FREE_PHONE):
                        self.assert_error(send(phone), 503, "SMS_DELIVERY_UNAVAILABLE", {})
                self.nothing_written(rows, outbox)

    def test_without_the_setting_codes_stay_random_and_any_number_is_accepted(self):
        with environment(SMS_MOCK_FIXED_CODE=None), \
                patch("app.phone_verification_service.secrets.randbelow", return_value=13579):
            verification_id = self.sent_id(self.send(FREE_PHONE))
            self.assertIn("013579", self.sms.outbox[-1]["text"])
            self.assert_error(self.confirm(verification_id, SYNTHETIC_CODE), 422, "VERIFICATION_CODE_INVALID")
            self.assertEqual(self.confirm(verification_id, "013579").status_code, 200)

    def test_the_synthetic_test_range(self):
        for e164 in ("+821000000000", "+821000009999", TEST_FREE_E164):
            self.assertTrue(is_synthetic_test_number(e164), e164)
        for e164 in ("+821000010000", "+821012345678", "+82100000999", "+8210000099990", "+821000009999\n"):
            self.assertFalse(is_synthetic_test_number(e164), e164)


class FlowTests(MockEnvironmentTestCase):
    def test_onboarding_send_confirm_and_save_with_the_fixed_code(self):
        for phone in ("010-0000-0000", "01000009999"):  # both ends, with and without hyphens
            with self.subTest(phone=phone):
                self.clock.advance(seconds=61)
                self.assertEqual(self.send(phone, user_id=3).status_code, 200)
        self.clock.advance(seconds=61)
        verification_id = self.sent_id(self.send(TEST_FREE, user_id=3))
        self.assertEqual(self.status(verification_id), "SENT")
        self.assertIn(SYNTHETIC_CODE, self.sms.outbox[-1]["text"])  # the test provider records it
        self.assert_error(self.confirm(verification_id, "135790", user_id=3), 422, "VERIFICATION_CODE_INVALID")
        self.assertEqual(self.confirm(verification_id, SYNTHETIC_CODE, user_id=3).status_code, 200)
        saved = self.save(verification_id, user_id=3)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.basic(3).phone_e164, TEST_FREE_E164)

    def test_profile_phone_change_with_the_fixed_code(self):
        verification_id = self.sent_id(self.profile_send(TEST_FREE))
        self.assertEqual(self.profile_confirm(verification_id, SYNTHETIC_CODE).status_code, 200)
        response = self.patch_profile({"expected_profile_version": self.version(), "phone_number": TEST_FREE,
                                       "phone_verification_id": verification_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.basic().phone_e164, TEST_FREE_E164)

    def test_numbers_outside_the_range_are_refused_before_anything_is_written(self):
        rows, outbox = len(self.rows(PhoneVerification)), len(self.sms.outbox)
        for label, send in (("onboarding", self.send), ("profile", self.profile_send)):
            answers = []
            for phone in (FREE_PHONE, THIRD_PHONE, PHONE, "010-0001-0000"):  # free, another's, own, next block
                with self.subTest(api=label, phone=phone):
                    response = send(phone)
                    self.assert_error(response, 422, "INVALID_PHONE_NUMBER", {"field": "phone_number"})
                    answers.append((response.json()["message"], sorted(response.headers.items())))
            self.assertEqual(len(set(map(repr, answers))), 1)  # the same answer whoever owns the number
        self.nothing_written(rows, outbox)


class PolicyTests(MockEnvironmentTestCase):
    def test_a_registered_test_number_is_suppressed_and_the_fixed_code_never_verifies_it(self):
        for label, send, confirm in (("onboarding", self.send, self.confirm),
                                     ("profile", self.profile_send, self.profile_confirm)):
            with self.subTest(api=label):
                self.clock.advance(seconds=61)
                outbox = len(self.sms.outbox)
                free = send(TEST_FREE)
                self.clock.advance(seconds=61)
                registered = send(TEST_REGISTERED)
                self.assertEqual(self.comparable(free), self.comparable(registered))
                verification_id = registered.json()["data"]["verification_id"]
                self.assertEqual(self.status(verification_id), "SUPPRESSED")
                self.assertEqual(len(self.sms.outbox), outbox + 1)  # only the free number
                self.assert_error(confirm(verification_id, SYNTHETIC_CODE), 422, "VERIFICATION_CODE_INVALID")
                self.assertEqual(self.attempts(verification_id), 1)

    def test_resend_wait_daily_limit_and_hmac_storage_are_unchanged(self):
        first = self.sent_id(self.send(TEST_FREE))
        self.assert_error(self.send(TEST_OTHER), 429, "PHONE_VERIFICATION_RATE_LIMITED")  # 60 s per account
        self.assert_error(self.send(TEST_FREE, user_id=3), 429, "PHONE_VERIFICATION_RATE_LIMITED")  # per number
        ids = [first]
        for _ in range(4):
            self.clock.advance(seconds=61)
            ids.append(self.sent_id(self.send(TEST_FREE)))
        self.clock.advance(seconds=61)
        self.assert_error(self.send(TEST_OTHER), 429, "PHONE_VERIFICATION_RATE_LIMITED")  # 5 per account
        self.assert_error(self.send(TEST_FREE, user_id=3), 429, "PHONE_VERIFICATION_RATE_LIMITED")  # 5 per number
        digests = [self.proof(verification_id).code_digest for verification_id in ids]
        self.assertEqual(len(set(digests)), 5)  # bound to each request even with one code
        self.assertTrue(all(SYNTHETIC_CODE not in digest for digest in digests))

    def test_expiry_attempts_latest_request_and_purposes_are_unchanged(self):
        expired = self.sent_id(self.send(TEST_FREE))
        self.clock.advance(seconds=300)
        self.assert_error(self.confirm(expired, SYNTHETIC_CODE), 410, "VERIFICATION_EXPIRED")

        self.clock.advance(seconds=61)
        limited = self.sent_id(self.send(TEST_FREE))
        for _ in range(5):
            self.assert_error(self.confirm(limited, "000000"), 422, "VERIFICATION_CODE_INVALID")
        self.assert_error(self.confirm(limited, SYNTHETIC_CODE), 429, "VERIFICATION_ATTEMPTS_EXCEEDED")

        self.clock.advance(seconds=61)
        older = self.sent_id(self.send(TEST_FREE))
        self.clock.advance(seconds=61)
        newer = self.sent_id(self.send(TEST_OTHER))
        self.assert_error(self.confirm(older, SYNTHETIC_CODE), 410, "VERIFICATION_EXPIRED")
        self.assert_error(self.profile_confirm(newer, SYNTHETIC_CODE), 404, "VERIFICATION_NOT_FOUND")  # purpose
        self.assertEqual(self.confirm(newer, SYNTHETIC_CODE).status_code, 200)

    def test_a_saved_proof_cannot_be_reused(self):
        verification_id = self.sent_id(self.profile_send(TEST_FREE))
        self.assertEqual(self.profile_confirm(verification_id, SYNTHETIC_CODE).status_code, 200)
        body = {"phone_number": TEST_FREE, "phone_verification_id": verification_id}
        self.assertEqual(self.patch_profile({**body, "expected_profile_version": self.version()}).status_code, 200)
        self.assertEqual(self.patch_profile({"expected_profile_version": self.version(), "phone_number": PHONE,
                                             "phone_verification_id": verification_id}).status_code, 409)

    def test_a_confirm_before_the_delivery_job_fails_and_counts_an_attempt(self):
        held = HeldDispatcher()
        self.app.dependency_overrides[get_sms_dispatcher] = lambda: held
        verification_id = self.sent_id(self.send(TEST_FREE))
        self.assertEqual(self.status(verification_id), "PENDING")
        self.assert_error(self.confirm(verification_id, SYNTHETIC_CODE), 422, "VERIFICATION_CODE_INVALID")
        self.assertEqual(self.attempts(verification_id), 1)  # an automatic retry would use up the 5 attempts
        held.run_held()
        self.assertEqual(self.status(verification_id), "SENT")
        self.assertEqual(self.confirm(verification_id, SYNTHETIC_CODE).status_code, 200)
        self.assertEqual(held.available(), held.capacity)


class ExposureTests(MockEnvironmentTestCase):
    def test_the_server_mock_keeps_no_record_of_sends(self):
        self.assertIsInstance(_mock_singleton, MockSmsProvider)
        self.assertFalse(_mock_singleton.keep_outbox)
        self.app.dependency_overrides.pop(get_sms_provider)  # the real settings-built provider
        verification_id = self.sent_id(self.send(TEST_FREE, user_id=3))
        self.assertEqual(self.status(verification_id), "SENT")
        self.assertEqual(_mock_singleton.outbox, [])
        self.assertNotIn(SYNTHETIC_CODE, repr(_mock_singleton))
        self.assertEqual(self.confirm(verification_id, SYNTHETIC_CODE, user_id=3).status_code, 200)
        self.assertEqual(_mock_singleton.outbox, [])

    def test_codes_numbers_and_secrets_never_reach_logs_answers_or_storage(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, previous)
        answers = []
        verification_id = self.sent_id(self.send(TEST_FREE, user_id=3))
        answers.append(self.confirm(verification_id, SYNTHETIC_CODE, user_id=3))
        answers.append(self.save(verification_id, user_id=3))
        self.clock.advance(seconds=61)
        answers.append(self.send(TEST_REGISTERED))
        answers.append(self.send(FREE_PHONE))
        with environment(SMS_MOCK_FIXED_CODE="24681"):
            answers.append(self.send(TEST_FREE))
        with environment(APP_ENV="production"):
            answers.append(self.send(TEST_FREE))
            try:
                mock_fixed_code()
            except SmsProviderUnavailable as exc:
                answers.append(str(exc))
        answers.append(self.client.get("/openapi.json"))
        secrets_ = (SYNTHETIC_CODE, TEST_ENV["PHONE_VERIFICATION_HMAC_KEY"], TEST_FREE_E164, TEST_REGISTERED_E164,
                    "1000001001", "1000009001")
        texts = [stream.getvalue(), repr(_mock_singleton)]
        texts += [answer if isinstance(answer, str) else answer.text + repr(dict(answer.headers))
                  for answer in answers]
        for text in texts:
            for secret in secrets_:
                self.assertNotIn(secret, text)
        with self.Session() as db:
            for row in db.query(PhoneVerification):
                self.assertNotIn(SYNTHETIC_CODE, " ".join(str(value) for value in (
                    row.code_digest, row.provider_message_id, row.provider_group_id)))
            for row in db.query(PhoneVerificationDailyCount):
                self.assertNotIn("1000001001", row.subject)


class ModeSwitchTests(MockEnvironmentTestCase):
    def random_mode_proof(self, send, confirm, phone, user_id):
        with environment(SMS_MOCK_FIXED_CODE=None):
            self.clock.advance(seconds=61)
            verification_id = self.sent_id(send(phone, user_id=user_id))
            self.assertEqual(confirm(verification_id, self.last_code(), user_id=user_id).status_code, 200)
        return verification_id

    def test_a_proof_made_before_the_mode_cannot_save_a_number_outside_the_range(self):
        onboarding = self.random_mode_proof(self.send, self.confirm, FREE_PHONE, 3)
        profile_proof = self.random_mode_proof(self.profile_send, self.profile_confirm, "010-4444-8888", 1)
        for value in (SYNTHETIC_CODE, "24681"):  # an unusable value fails closed as well
            with self.subTest(value=value), environment(SMS_MOCK_FIXED_CODE=value):
                self.assert_error(self.save(onboarding, user_id=3), 409, "PHONE_VERIFICATION_REQUIRED")
                self.assert_error(self.patch_profile({"expected_profile_version": self.version(),
                                                      "phone_number": "010-4444-8888",
                                                      "phone_verification_id": profile_proof}),
                                  409, "PHONE_VERIFICATION_REQUIRED")
        self.assertIsNone(self.basic(3))
        self.assertEqual(self.basic().phone_e164, "+821012345678")
        self.assertIsNone(self.proof(onboarding).consumed_at)
        self.assertIsNone(self.proof(profile_proof).consumed_at)
        with environment(SMS_MOCK_FIXED_CODE=None):  # the same proofs are fine without the mode
            self.assertEqual(self.save(onboarding, user_id=3).status_code, 200)

    def test_requests_made_in_the_mode_end_with_their_expiry_after_it_is_turned_off(self):
        lasting = self.sent_id(self.send(TEST_FREE, user_id=3))
        self.clock.advance(seconds=61)
        expiring = self.sent_id(self.send(TEST_OTHER))
        with environment(SMS_MOCK_FIXED_CODE=None):
            # Documented: a request made with the fixed code stays verifiable until it expires (300 s)
            # and its proof stays savable for 15 minutes; only test-range numbers can have one.
            self.assertEqual(self.confirm(lasting, SYNTHETIC_CODE, user_id=3).status_code, 200)
            self.assertEqual(self.save(lasting, user_id=3).status_code, 200)
            self.clock.advance(seconds=300)
            self.assert_error(self.confirm(expiring, SYNTHETIC_CODE), 410, "VERIFICATION_EXPIRED")
