"""Issue #110: sending a verification code never discloses whether a number is registered.

Isolated SQLite with the injected clock, the mock SMS provider and an inline delivery
dispatcher. The race and crash cases call the service steps (reserve, claim, confirm,
record) directly so their order is exact. Synthetic accounts and numbers only."""
from datetime import timedelta
import logging
from unittest.mock import patch
import uuid

from sqlalchemy.exc import OperationalError

from test_onboarding_basic_info import PHONE, PHONE_E164, START
from test_user_profile import THIRD_PHONE, THIRD_E164, ProfileTestCase
from app.account_status import change_to_age_review
from app.models import PhoneVerification, PhoneVerificationDailyCount, VolunteerOnboardingBasic
import app.phone_verification_service as service
import app.profile_service as service_profile
from app.api_contract import ContractError
from app.phone_verification_service import (PURPOSE_ONBOARDING, PURPOSE_PROFILE, SmsDispatcher, confirm_code,
                                            deliver_verification,
                                            get_sms_dispatcher, hmac_key, purge_phone_verifications, reserve_send)
from app.sms_provider import get_sms_provider

FREE_PHONE, FREE_E164 = "010-4444-7777", "+821044447777"
SEND_KEYS = {"verification_id", "expires_in_seconds", "resend_after_seconds"}


class PrivacyTestCase(ProfileTestCase):
    def setUp(self):
        super().setUp()
        # user 2 owns THIRD_PHONE; user 1 has Step 1 with PHONE (needed for profile sends).
        self.complete_step1(phone=THIRD_PHONE, user_id=2)
        self.clock.advance(seconds=61)
        self.complete_step1(phone=PHONE, user_id=1)
        self.clock.advance(seconds=61)
        self.reset_counters()

    def reset_counters(self):
        with self.Session.begin() as db:
            db.query(PhoneVerificationDailyCount).delete()

    def counters(self):
        with self.Session() as db:
            return sorted((row.scope, row.send_count) for row in db.query(PhoneVerificationDailyCount))

    def status(self, verification_id):
        with self.Session() as db:
            return db.get(PhoneVerification, uuid.UUID(verification_id)).send_status

    def set_status(self, verification_id, status):
        with self.Session.begin() as db:
            db.get(PhoneVerification, uuid.UUID(verification_id)).send_status = status

    def reserve(self, phone_e164=FREE_E164, user_id=1, code=123456, purpose=PURPOSE_ONBOARDING):
        """T1 only (no delivery): returns the request id as a string."""
        with patch("app.phone_verification_service.secrets.randbelow", return_value=code), self.Session() as db:
            verification_id, _code, _suppressed = reserve_send(db, user_id, phone_e164, self.clock.now, hmac_key(),
                                                               purpose)
        return str(verification_id)

    def deliver(self, verification_id, phone_e164=FREE_E164, code="123456"):
        deliver_verification(self.engine, self.sms, uuid.UUID(verification_id), phone_e164, code, self.clock)

    def comparable(self, response):
        data = response.json()["data"]
        return (response.status_code, response.json()["code"], set(data), data["expires_in_seconds"],
                data["resend_after_seconds"], response.headers.get("cache-control"),
                sorted(name for name in response.headers if name not in ("content-length", "date")))


class SendAnswerTests(PrivacyTestCase):
    def test_both_send_apis_answer_the_same_for_registered_and_unregistered_numbers(self):
        for label, send in (("onboarding", lambda phone: self.send(phone)),
                            ("profile", lambda phone: self.profile_send(phone))):
            with self.subTest(api=label):
                self.reset_counters()
                outbox = len(self.sms.outbox)
                free = send(FREE_PHONE)
                self.clock.advance(seconds=61)
                registered = send(THIRD_PHONE)
                self.clock.advance(seconds=61)
                self.assertEqual(self.comparable(free), self.comparable(registered))
                self.assertNotEqual(free.json()["data"]["verification_id"], registered.json()["data"]["verification_id"])
                uuid.UUID(registered.json()["data"]["verification_id"])
                self.assertEqual(len(self.sms.outbox), outbox + 1)  # only the free number got an SMS
                self.assertEqual(self.status(free.json()["data"]["verification_id"]), "SENT")
                self.assertEqual(self.status(registered.json()["data"]["verification_id"]), "SUPPRESSED")

    def test_own_number_is_sent_normally(self):
        response = self.profile_send(PHONE)  # user 1's own registered number
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(response.json()["data"]["verification_id"]), "SENT")
        self.assertEqual(self.sms.outbox[-1]["to"], PHONE_E164)

    def test_no_provider_is_the_same_503_for_any_number_and_writes_nothing(self):
        self.app.dependency_overrides[get_sms_provider] = lambda: None
        rows = len(self.rows(PhoneVerification))
        for phone in (FREE_PHONE, THIRD_PHONE):
            with self.subTest(phone=phone):
                self.assert_error(self.send(phone), 503, "SMS_DELIVERY_UNAVAILABLE", {})
        self.assertEqual((len(self.rows(PhoneVerification)), self.counters()), (rows, []))
        self.assertEqual(self.dispatcher.available(), self.dispatcher.capacity)

    def test_delivery_failures_are_internal_and_still_answer_200(self):
        for outcome, status in (("failed", "FAILED"), ("timeout", "UNKNOWN"), ("error", "UNKNOWN")):
            with self.subTest(outcome=outcome):
                self.sms.next_outcome = [outcome]
                response = self.send(FREE_PHONE)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.status(response.json()["data"]["verification_id"]), status)
                self.clock.advance(seconds=61)
        self.assertEqual(self.counters(), [("PHONE", 3), ("USER", 3)])  # all counted


class LimitTests(PrivacyTestCase):
    def test_registered_numbers_are_charged_and_refusals_roll_everything_back(self):
        self.assertEqual(self.send(THIRD_PHONE).status_code, 200)
        self.assertEqual(self.counters(), [("PHONE", 1), ("USER", 1)])
        rows = len(self.rows(PhoneVerification))
        # The resend wait (this account and this number): 429, nothing charged, no row.
        self.assert_error(self.send(THIRD_PHONE), 429, "PHONE_VERIFICATION_RATE_LIMITED")
        self.assert_error(self.send(THIRD_PHONE, user_id=2), 429, "PHONE_VERIFICATION_RATE_LIMITED")
        self.assertEqual((self.counters(), len(self.rows(PhoneVerification))), ([("PHONE", 1), ("USER", 1)], rows))
        # The daily limit, for a registered and a free number alike.
        with self.Session.begin() as db:
            db.query(PhoneVerificationDailyCount).filter_by(scope="USER").update({"send_count": 5})
        self.clock.advance(seconds=61)
        for phone in (THIRD_PHONE, FREE_PHONE):
            with self.subTest(phone=phone):
                self.assert_error(self.send(phone), 429, "PHONE_VERIFICATION_RATE_LIMITED")
        self.assertEqual((self.counters(), len(self.rows(PhoneVerification))), ([("PHONE", 1), ("USER", 5)], rows))

    def test_a_database_error_after_charging_rolls_the_charge_back(self):
        failure = OperationalError("SELECT", {}, Exception("synthetic"))
        with patch.object(service, "registered_to_other_user", side_effect=failure), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.send(THIRD_PHONE), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual((self.counters(), self.dispatcher.available()), ([], self.dispatcher.capacity))


class SlotTests(PrivacyTestCase):
    def test_overload_is_the_same_503_for_any_number_and_writes_nothing(self):
        full = SmsDispatcher(workers=0, queue=0, inline=True)
        self.app.dependency_overrides[get_sms_dispatcher] = lambda: full
        rows = len(self.rows(PhoneVerification))
        for phone in (FREE_PHONE, THIRD_PHONE):
            with self.subTest(phone=phone):
                self.assert_error(self.send(phone), 503, "SMS_DELIVERY_UNAVAILABLE", {})
                self.assert_error(self.profile_send(phone), 503, "SMS_DELIVERY_UNAVAILABLE", {})
        self.assertEqual((len(self.rows(PhoneVerification)), self.counters()), (rows, []))

    def test_every_path_releases_its_slot_exactly_once(self):
        capacity = self.dispatcher.capacity
        paths = {
            "sent": lambda: self.send(FREE_PHONE),
            "suppressed": lambda: self.send(THIRD_PHONE),
            "provider error": lambda: (self.sms.next_outcome.append("error"), self.send(FREE_PHONE))[1],
            "429": lambda: (self.send(FREE_PHONE), self.send(FREE_PHONE))[1],
            "409 no Step 1": lambda: self.profile_send(FREE_PHONE, user_id=3),
        }
        for name, call in paths.items():
            with self.subTest(path=name):
                self.clock.advance(seconds=61)
                self.reset_counters()
                call()
                self.assertEqual(self.dispatcher.available(), capacity)
        with patch.object(service, "_claim_for_delivery", side_effect=RuntimeError("job crash")):
            self.clock.advance(seconds=61)
            self.assertEqual(self.send(FREE_PHONE).status_code, 200)  # the job failed after the answer
        self.assertEqual(self.dispatcher.available(), capacity)
        with patch.object(self.dispatcher, "submit", side_effect=RuntimeError("shutting down")):
            self.clock.advance(seconds=61)
            response = self.send(FREE_PHONE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.status(response.json()["data"]["verification_id"]), "PENDING")
        self.assertEqual(self.dispatcher.available(), capacity)
        with self.Session() as db:
            change_to_age_review(db, 1, self.clock.now)
        self.clock.advance(seconds=61)
        self.assert_error(self.send(FREE_PHONE), 403, "AGE_REVIEW_REQUIRED")
        self.assertEqual(self.dispatcher.available(), capacity)

    def test_a_slot_is_freed_once_even_when_a_job_raises_a_base_exception(self):
        dispatcher = SmsDispatcher(workers=1, queue=0, inline=True)
        slot = dispatcher.try_acquire()
        self.assertIsNone(dispatcher.try_acquire())  # capacity 1 is taken

        def interrupted():
            raise SystemExit("interrupted")

        with self.assertRaises(SystemExit):
            dispatcher.submit(interrupted, slot)
        slot.release()  # the request path releases too: no double free
        slot.release()
        self.assertEqual(dispatcher.available(), 1)


class DeliveryTests(PrivacyTestCase):
    def test_delivery_rechecks_the_request_right_before_sending(self):
        sent = len(self.sms.outbox)
        expired = self.reserve()
        self.clock.advance(seconds=301)
        self.deliver(expired)
        self.assertEqual(self.status(expired), "FAILED")
        self.clock.advance(seconds=61)
        older = self.reserve()
        self.clock.advance(seconds=61)
        newer = self.reserve(code=222222)
        self.deliver(older)  # replaced by a newer request
        self.assertEqual(self.status(older), "FAILED")
        self.deliver(newer, code="222222")
        self.assertEqual(self.status(newer), "SENT")
        self.clock.advance(seconds=61)
        inactive = self.reserve(code=333333)
        with self.Session() as db:
            change_to_age_review(db, 1, self.clock.now)
        self.deliver(inactive, code="333333")
        self.assertEqual(self.status(inactive), "FAILED")
        gone = str(uuid.uuid4())
        self.deliver(gone)  # deleted (e.g. sign-up exit): nothing happens
        self.assertEqual(len(self.sms.outbox), sent + 1)  # only the newer one was sent

    def test_a_request_is_delivered_at_most_once_and_never_resent(self):
        verification_id = self.reserve()
        self.deliver(verification_id)
        self.deliver(verification_id)  # a duplicate dispatch
        self.assertEqual([message["request_key"] for message in self.sms.outbox].count(verification_id), 1)
        self.clock.advance(seconds=61)
        unknown = self.reserve(code=444444)
        self.sms.next_outcome = ["timeout"]
        self.deliver(unknown, code="444444")
        self.deliver(unknown, code="444444")
        self.assertEqual((self.status(unknown), self.sms.lookups), ("UNKNOWN", []))
        self.assertNotIn(unknown, [message["request_key"] for message in self.sms.outbox])


class ConfirmTests(PrivacyTestCase):
    def request_in(self, status, code=123456):
        self.clock.advance(seconds=61)
        self.reset_counters()
        verification_id = self.reserve(code=code)
        self.set_status(verification_id, status)
        return verification_id

    def test_a_wrong_code_answers_the_same_in_every_delivery_state(self):
        for status in ("SENT", "UNKNOWN", "SENDING", "PENDING", "FAILED", "SUPPRESSED"):
            with self.subTest(status=status):
                verification_id = self.request_in(status)
                for attempt in range(5):
                    self.assert_error(self.confirm(verification_id, "999999"), 422, "VERIFICATION_CODE_INVALID")
                self.assert_error(self.confirm(verification_id, "999999"), 429, "VERIFICATION_ATTEMPTS_EXCEEDED")
        verification_id = self.request_in("SUPPRESSED")
        self.clock.advance(seconds=300)
        self.assert_error(self.confirm(verification_id, "123456"), 410, "VERIFICATION_EXPIRED")

    def test_a_matching_code_verifies_only_requests_that_may_have_been_delivered(self):
        for status, verified in (("SENT", True), ("UNKNOWN", True), ("SENDING", True),
                                 ("PENDING", False), ("FAILED", False), ("SUPPRESSED", False)):
            with self.subTest(status=status):
                verification_id = self.request_in(status)
                response = self.confirm(verification_id, "123456")
                if verified:
                    self.assertEqual(response.status_code, 200, response.text)
                else:
                    self.assert_error(response, 422, "VERIFICATION_CODE_INVALID")
        self.assertEqual(self.sms.lookups, [])

    def test_saves_refuse_requests_that_were_never_sent_even_if_marked_verified(self):
        for status in ("PENDING", "FAILED", "SUPPRESSED"):
            with self.subTest(status=status):
                verification_id = self.request_in(status)
                with self.Session.begin() as db:  # as if a verification were recorded by mistake
                    row = db.get(PhoneVerification, uuid.UUID(verification_id))
                    row.verified_at, row.verified_valid_until = self.clock.now, self.clock.now + timedelta(minutes=15)
                with self.Session.begin() as db:  # user 1 must not have Step 1 for the onboarding save
                    db.query(VolunteerOnboardingBasic).filter_by(user_id=1).delete()
                self.assert_error(self.save(verification_id, user_id=1), 409, "PHONE_VERIFICATION_REQUIRED")
                self.assertIsNone(self.basic(1))
                self.clock.advance(seconds=61)
                self.complete_step1(phone=PHONE, user_id=1)  # restore for the next round

    def test_profile_patch_refuses_requests_that_were_never_sent_even_if_marked_verified(self):
        for status in ("PENDING", "FAILED", "SUPPRESSED"):
            with self.subTest(status=status):
                self.clock.advance(seconds=61)
                self.reset_counters()
                verification_id = self.reserve(purpose=PURPOSE_PROFILE)
                self.set_status(verification_id, status)
                with self.Session.begin() as db:  # as if a verification were recorded by mistake
                    row = db.get(PhoneVerification, uuid.UUID(verification_id))
                    row.verified_at, row.verified_valid_until = self.clock.now, self.clock.now + timedelta(minutes=15)
                body = {"expected_profile_version": self.version(), "phone_number": FREE_PHONE,
                        "phone_verification_id": verification_id}
                self.assert_error(self.patch_profile(body), 409, "PHONE_VERIFICATION_REQUIRED")
                self.assertEqual(self.basic(1).phone_e164, PHONE_E164)
                self.assertIsNone(self.proof(verification_id).consumed_at)

    def test_a_consumed_profile_proof_is_refused_again_for_the_same_number(self):
        verification_id = self.profile_verified(FREE_PHONE)
        body = {"expected_profile_version": self.version(), "phone_number": FREE_PHONE,
                "phone_verification_id": verification_id}
        self.assertEqual(self.patch_profile(body).status_code, 200)
        self.assertIsNotNone(self.proof(verification_id).consumed_at)
        with self.Session() as db:  # the proof is still the latest and inside its 15 minutes
            with self.assertRaises(ContractError) as refused:
                service_profile.usable_profile_proof(db, 1, uuid.UUID(verification_id), FREE_E164, self.clock.now)
        self.assertEqual((refused.exception.status_code, refused.exception.code), (409, "PHONE_VERIFICATION_REQUIRED"))


class LateResultTests(PrivacyTestCase):
    def claimed(self, code=123456):
        self.clock.advance(seconds=61)
        self.reset_counters()
        verification_id = self.reserve(code=code)
        with self.Session() as db:
            self.assertTrue(service._claim_for_delivery(db, uuid.UUID(verification_id), self.clock.now))
        return verification_id

    def test_a_late_provider_result_never_undoes_a_verification(self):
        for outcome in ("SENT", "FAILED", "UNKNOWN"):
            with self.subTest(outcome=outcome):
                verification_id = self.claimed()
                self.assertEqual(self.status(verification_id), "SENDING")
                self.assertEqual(self.confirm(verification_id, "123456").status_code, 200)
                with self.Session() as db:
                    before = db.get(PhoneVerification, uuid.UUID(verification_id))
                    kept = (before.verified_at, before.verified_valid_until, before.consumed_at, before.attempt_count)
                    service._record_result(db, uuid.UUID(verification_id), outcome, self.clock.now)
                with self.Session() as db:
                    after = db.get(PhoneVerification, uuid.UUID(verification_id))
                    self.assertEqual(after.send_status, "SENT")
                    self.assertEqual((after.verified_at, after.verified_valid_until, after.consumed_at,
                                      after.attempt_count), kept)
        # The proof of the last round is still usable for a save.
        body = {"expected_profile_version": 1, "nickname": "늦은응답"}
        self.assertEqual(self.patch_profile(body).status_code, 200)
        with self.Session.begin() as db:
            db.query(VolunteerOnboardingBasic).filter_by(user_id=1).delete()
        self.assertEqual(self.save(verification_id, user_id=1).status_code, 200)

    def test_a_request_left_sending_by_a_crash_still_verifies_and_is_cleaned_up_later(self):
        crashed = self.claimed(code=555555)  # the process died during the provider call
        self.assertEqual(self.confirm(crashed, "555555").status_code, 200)
        unverified = self.claimed(code=666666)
        self.assertEqual(self.status(unverified), "SENDING")
        self.clock.advance(seconds=301)
        with self.Session() as db:
            purge_phone_verifications(db, self.clock.now, apply=True)
        with self.Session() as db:
            self.assertIsNone(db.get(PhoneVerification, uuid.UUID(unverified)))


class StorageTests(PrivacyTestCase):
    def test_the_code_is_never_stored_or_logged(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        with patch("app.phone_verification_service.secrets.randbelow", return_value=987654):
            sent = self.send(FREE_PHONE).json()["data"]["verification_id"]
            self.clock.advance(seconds=61)
            suppressed = self.send(THIRD_PHONE).json()["data"]["verification_id"]
        self.confirm(sent, "987654")
        self.confirm(suppressed, "987654")
        with self.Session() as db:
            for verification_id in (sent, suppressed):
                row = db.get(PhoneVerification, uuid.UUID(verification_id))
                values = " ".join(str(getattr(row, column.name)) for column in PhoneVerification.__table__.columns)
                self.assertNotIn("987654", values)
        text = " ".join(record.getMessage() for record in records)
        for secret in ("987654", "4444", "7777", "5555", "6666", FREE_E164, THIRD_E164):
            self.assertNotIn(secret, text)

    def test_cleanup_removes_expired_suppressed_pending_and_sending_requests(self):
        ids = []
        for status in ("SUPPRESSED", "PENDING", "SENDING"):
            self.clock.advance(seconds=61)
            self.reset_counters()
            ids.append(self.reserve())
            self.set_status(ids[-1], status)
        self.clock.advance(seconds=301)
        with self.Session() as db:
            purge_phone_verifications(db, self.clock.now, apply=True)
        with self.Session() as db:
            self.assertEqual([db.get(PhoneVerification, uuid.UUID(value)) for value in ids], [None, None, None])
