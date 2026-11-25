"""Issue #110 on a real, isolated PostgreSQL: migration 037, one number requested by several
accounts at once, no transaction held during the provider call, the users ->
phone_verifications lock order of the delivery job, and a late provider result racing a
verification, and the daily number limit across accounts with its rollback. Also the mock SMS
test mode with a fixed code (Issue #124). Synthetic accounts, numbers and codes only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
from threading import Barrier, Event
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import test_onboarding_basic_info as step1_module
from test_onboarding_basic_info import PLEDGE_VERSION, START
from test_phone_verification_privacy import FREE_E164, FREE_PHONE, PrivacyTestCase
from test_user_profile import THIRD_E164, THIRD_PHONE
from test_user_profile_postgres import IsolatedSchema
from app.models import PhoneVerification, PhoneVerificationDailyCount, UserPledgeAcknowledgement
from app import age_review_service
from app.api_contract import ContractError
import app.phone_verification_service as service
from app.phone_verification_service import hmac_key, phone_subject
from app.sms_provider import MockSmsProvider, get_sms_provider

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "037_phone_verification_suppressed.sql"
WAIT = 8


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class SuppressedMigrationPostgresTests(IsolatedSchema, unittest.TestCase):
    def run_migration(self, engine):
        connection = engine.raw_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

    def test_037_allows_suppressed_keeps_rows_and_reruns_as_no_op(self):
        engine = self.isolated_engine()
        with engine.begin() as connection:  # the 024 definition of the status, with rows
            connection.exec_driver_sql(
                "CREATE TABLE phone_verifications (id UUID PRIMARY KEY, send_status VARCHAR(16) NOT NULL,"
                " CONSTRAINT ck_phone_verifications_send_status"
                " CHECK (send_status IN ('PENDING', 'SENDING', 'SENT', 'UNKNOWN', 'FAILED')))")
            for status in ("SENT", "FAILED"):
                connection.execute(sql_text("INSERT INTO phone_verifications VALUES (:id, :status)"),
                                   {"id": uuid.uuid4(), "status": status})
        self.run_migration(engine)
        self.run_migration(engine)
        with engine.begin() as connection:
            statuses = sorted(connection.exec_driver_sql("SELECT send_status FROM phone_verifications").scalars())
            self.assertEqual(statuses, ["FAILED", "SENT"])
            definition = connection.exec_driver_sql(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_phone_verifications_send_status'"
            ).scalars().all()
            self.assertEqual(len(definition), 1)
            self.assertIn("SUPPRESSED", definition[0])
            connection.execute(sql_text("INSERT INTO phone_verifications VALUES (:id, 'SUPPRESSED')"), {"id": uuid.uuid4()})
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.execute(sql_text("INSERT INTO phone_verifications VALUES (:id, 'BOGUS')"), {"id": uuid.uuid4()})


@unittest.skipUnless(TEST_URL, "격리 PostgreSQL이 필요합니다")
class PrivacyPostgresTests(IsolatedSchema, PrivacyTestCase):
    def setUp(self):
        engine = self.isolated_engine()
        with patch.object(step1_module, "create_engine", lambda *args, **kwargs: engine):
            super().setUp()

    def test_one_number_requested_by_two_accounts_at_once_is_accepted_once(self):
        with self.Session.begin() as db:  # user 3 pledges too: users 1 and 3 race, user 2 owns THIRD_PHONE
            db.add(UserPledgeAcknowledgement(user_id=3, pledge_version=PLEDGE_VERSION, acknowledged_at=START))
        senders = (1, 3)
        for phone in (FREE_PHONE, THIRD_PHONE):  # unregistered, registered
            with self.subTest(phone=phone):
                self.clock.advance(seconds=61)
                self.reset_counters()
                before = len(self.rows(PhoneVerification))
                barrier = Barrier(2)

                def send(user_id):
                    barrier.wait(WAIT)
                    return self.send(phone, user_id=user_id)

                with ThreadPoolExecutor(2) as pool:
                    responses = list(pool.map(send, senders))
                codes = [response.status_code for response in responses]
                self.assertEqual(sorted(codes), [200, 429], [r.text for r in responses])
                winner, loser_id = responses[codes.index(200)], senders[codes.index(429)]
                with self.Session() as db:
                    phone_rows = db.query(PhoneVerificationDailyCount).filter_by(scope="PHONE").all()
                    self.assertEqual([row.send_count for row in phone_rows], [1])  # committed once
                    self.assertIsNone(db.query(PhoneVerificationDailyCount)  # the refusal rolled back
                                      .filter_by(scope="USER", subject=str(loser_id)).one_or_none())
                self.assertEqual(len(self.rows(PhoneVerification)), before + 1)
                self.assertEqual(self.status(winner.json()["data"]["verification_id"]),
                                 "SUPPRESSED" if phone == THIRD_PHONE else "SENT")

    # Daily number limit across accounts (Issue #110 follow-up). The resend wait is already met or
    # there is no recent request, so only the daily limit can refuse; the refusal must roll back.

    def grant_pledge(self, user_id):
        with self.Session.begin() as db:
            db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION, acknowledged_at=START))

    def kst_day(self):
        return (self.clock.now + timedelta(hours=9)).date()

    def set_counter(self, scope, subject, count):
        with self.Session.begin() as db:
            db.add(PhoneVerificationDailyCount(scope=scope, subject=subject, kst_date=self.kst_day(), send_count=count))

    def counter(self, scope, subject):
        with self.Session() as db:
            row = db.query(PhoneVerificationDailyCount).filter_by(scope=scope, subject=subject,
                                                                  kst_date=self.kst_day()).one_or_none()
            return None if row is None else row.send_count

    def seconds_to_kst_midnight(self):
        """Expected Retry-After from the fixed clock, computed here rather than by the service."""
        local = self.clock.now.astimezone(timezone(timedelta(hours=9)))
        midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), local.tzinfo)
        return max(1, math.ceil((midnight - self.clock.now).total_seconds()))

    def assert_daily_refusal(self, response):
        expected = self.seconds_to_kst_midnight()
        self.assert_error(response, 429, "PHONE_VERIFICATION_RATE_LIMITED", {"retry_after_seconds": expected})
        self.assertEqual(response.headers["retry-after"], str(expected))
        self.assertGreater(expected, service.RESEND_WAIT_SECONDS)  # not the resend-wait refusal

    def test_the_number_daily_limit_refuses_another_account_after_the_resend_wait_and_rolls_back(self):
        self.grant_pledge(3)
        for phone, e164, accepted_status in ((FREE_PHONE, FREE_E164, "SENT"), (THIRD_PHONE, THIRD_E164, "SUPPRESSED")):
            with self.subTest(phone=phone):
                self.clock.advance(seconds=61)
                self.reset_counters()
                number = phone_subject(hmac_key(), e164)
                self.set_counter("PHONE", number, 4)
                self.set_counter("USER", "3", 2)  # the refused account already sent twice today
                outbox = len(self.sms.outbox)
                accepted = self.send(phone, user_id=1)
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertEqual(self.status(accepted.json()["data"]["verification_id"]), accepted_status)
                self.assertEqual(len(self.sms.outbox), outbox + (1 if accepted_status == "SENT" else 0))
                self.clock.advance(seconds=61)  # both resend waits are met
                rows, outbox = len(self.rows(PhoneVerification)), len(self.sms.outbox)
                self.assert_daily_refusal(self.send(phone, user_id=3))
                self.assertEqual((self.counter("PHONE", number), self.counter("USER", "3"), self.counter("USER", "1")),
                                 (5, 2, 1))  # the refused account's existing count was not increased
                self.assertEqual((len(self.rows(PhoneVerification)), len(self.sms.outbox)), (rows, outbox))
                self.assertEqual(self.dispatcher.available(), self.dispatcher.capacity)

    def test_an_exhausted_number_refuses_concurrent_accounts_without_charging_them(self):
        self.grant_pledge(3)
        for phone, e164 in ((FREE_PHONE, FREE_E164), (THIRD_PHONE, THIRD_E164)):
            with self.subTest(phone=phone):
                self.clock.advance(seconds=61)  # no recent request of either account or the number
                self.reset_counters()
                number = phone_subject(hmac_key(), e164)
                self.set_counter("PHONE", number, 5)
                self.set_counter("USER", "1", 1)
                self.set_counter("USER", "3", 1)
                rows, outbox = len(self.rows(PhoneVerification)), len(self.sms.outbox)
                barrier = Barrier(2)

                def send(user_id):
                    barrier.wait(WAIT)
                    return self.send(phone, user_id=user_id)

                with ThreadPoolExecutor(2) as pool:
                    responses = list(pool.map(send, (1, 3)))
                for response in responses:
                    self.assert_daily_refusal(response)
                self.assertEqual((self.counter("PHONE", number), self.counter("USER", "1"), self.counter("USER", "3")),
                                 (5, 1, 1))
                self.assertEqual((len(self.rows(PhoneVerification)), len(self.sms.outbox)), (rows, outbox))
                self.assertEqual(self.dispatcher.available(), self.dispatcher.capacity)

    def test_no_transaction_is_held_while_the_provider_is_called(self):
        checked = []
        engine = self.engine

        class CheckingProvider(MockSmsProvider):
            def send(inner, *, to_e164, text: str, request_key):
                with engine.connect() as other:  # NOWAIT fails at once if the job still held the row
                    row = other.execute(sql_text("SELECT send_status FROM phone_verifications WHERE id = :id"
                                                 " FOR UPDATE NOWAIT"), {"id": uuid.UUID(request_key)}).scalar()
                    checked.append(row)
                    other.rollback()
                return super().send(to_e164=to_e164, text=text, request_key=request_key)

        provider = CheckingProvider()
        self.app.dependency_overrides[get_sms_provider] = lambda: provider
        self.assertEqual(self.send(FREE_PHONE).status_code, 200)
        self.assertEqual(checked, ["SENDING"])

    def test_the_delivery_check_follows_the_users_then_request_lock_order(self):
        verification_id = self.reserve(code=123456)
        reached, release = Event(), Event()
        original = service.is_latest_for_user

        def held(db, verification):
            result = original(db, verification)
            if not reached.is_set() and verification.send_status == "PENDING":
                reached.set()  # the job holds users FOR SHARE and the request FOR UPDATE here
                if not release.wait(WAIT):
                    raise AssertionError("not released")
            return result

        def exit_account():  # sign-up exit: users FOR UPDATE, then deletes the requests
            with self.Session() as db:
                try:
                    age_review_service.delete_account(db, 1)
                except ContractError as refused:
                    return refused.code
            return "DELETED"

        with patch.object(service, "is_latest_for_user", held), ThreadPoolExecutor(4) as pool:
            job = pool.submit(self.deliver, verification_id, FREE_E164, "123456")
            self.assertTrue(reached.wait(WAIT))
            self.clock.advance(seconds=61)
            new_send = pool.submit(self.send, FREE_PHONE)  # users FOR UPDATE: waits for the job
            confirm = pool.submit(self.confirm, verification_id, "123456")  # request FOR UPDATE: waits
            exited = pool.submit(exit_account)  # users FOR UPDATE: waits as well
            with self.assertRaises(TimeoutError):
                new_send.result(timeout=0.5)
            with self.assertRaises(TimeoutError):
                exited.result(timeout=0.1)
            release.set()
            job.result(timeout=WAIT)
            sent, confirmed, exit_code = (new_send.result(timeout=WAIT), confirm.result(timeout=WAIT),
                                          exited.result(timeout=WAIT))
        self.assertEqual(sent.status_code, 200, sent.text)  # no deadlock, no lock timeout
        self.assertIn(confirmed.status_code, (200, 410), confirmed.text)  # verified, or replaced by the new send
        self.assertEqual(exit_code, "AGE_REVIEW_NOT_REQUIRED")  # an ACTIVE account: refused after waiting

    def test_a_late_provider_result_racing_a_verification_keeps_it(self):
        verification_id = self.reserve(code=123456)
        with self.Session() as db:
            self.assertTrue(service._claim_for_delivery(db, uuid.UUID(verification_id), self.clock.now))
        Session = sessionmaker(bind=self.engine)
        verifier = Session()
        try:
            row = (verifier.query(PhoneVerification).filter_by(id=uuid.UUID(verification_id))
                   .with_for_update().one())  # a confirm in progress holds the row
            row.verified_at, row.verified_valid_until = self.clock.now, self.clock.now + service.VERIFIED_SAVE_WINDOW
            verifier.flush()
            with ThreadPoolExecutor(1) as pool:
                def record():
                    with Session() as db:
                        service._record_result(db, uuid.UUID(verification_id), "FAILED", self.clock.now)

                late = pool.submit(record)
                with self.assertRaises(TimeoutError):
                    late.result(timeout=0.5)  # waits for the row
                verifier.commit()
                late.result(timeout=WAIT)
        finally:
            verifier.close()
        with self.Session() as db:
            stored = db.get(PhoneVerification, uuid.UUID(verification_id))
            self.assertEqual(stored.send_status, "SENT")
            self.assertIsNotNone(stored.verified_at)

    def test_a_number_registered_right_after_a_real_send_cannot_be_saved(self):
        with self.Session.begin() as db:
            db.query(service.VolunteerOnboardingBasic).filter_by(user_id=1).delete()
        first = self.send(FREE_PHONE, user_id=1)  # not registered yet: really sent
        first_code = self.last_code()
        self.clock.advance(seconds=61)
        self.assertEqual(self.status(first.json()["data"]["verification_id"]), "SENT")
        # Meanwhile another account verifies and registers the same number.
        with self.Session.begin() as db:
            db.query(service.VolunteerOnboardingBasic).filter_by(user_id=2).delete()
        self.save(self.verified(FREE_PHONE, user_id=2), user_id=2)
        verification_id = first.json()["data"]["verification_id"]
        self.assertEqual(self.confirm(verification_id, first_code, user_id=1).status_code, 200)  # owns the number
        self.assert_error(self.save(verification_id, user_id=1), 409, "PHONE_ALREADY_REGISTERED")
        with self.Session() as db:
            owners = db.query(service.VolunteerOnboardingBasic).filter_by(phone_e164=FREE_E164).count()
        self.assertEqual(owners, 1)

    # Mock SMS test mode (Issue #124): a synthetic fixed code, test-range numbers only.

    def test_the_fixed_code_mode_sends_confirms_and_saves_test_numbers_only(self):
        self.grant_pledge(3)
        fixed = {"SMS_PROVIDER": "mock", "APP_ENV": "test", "SMS_MOCK_FIXED_CODE": "246810"}  # synthetic
        test_number, test_e164 = "010-0000-1001", "+821000001001"
        with patch.dict(os.environ, fixed):
            rows = len(self.rows(PhoneVerification))
            self.assert_error(self.send(FREE_PHONE, user_id=3), 422, "INVALID_PHONE_NUMBER", {"field": "phone_number"})
            self.assertEqual((len(self.rows(PhoneVerification)), self.counters()), (rows, []))
            # Free test number: sent, verified with the fixed code and saved.
            verification_id = self.send(test_number, user_id=3).json()["data"]["verification_id"]
            self.assertEqual(self.status(verification_id), "SENT")
            self.assertEqual(self.confirm(verification_id, "246810", user_id=3).status_code, 200)
            self.assertEqual(self.save(verification_id, user_id=3).status_code, 200)
            # The same number is now registered: another account's request is SUPPRESSED, never verified.
            self.clock.advance(seconds=61)
            outbox = len(self.sms.outbox)
            suppressed = self.send(test_number, user_id=1)
            self.assertEqual(suppressed.status_code, 200, suppressed.text)
            suppressed_id = suppressed.json()["data"]["verification_id"]
            self.assertEqual((self.status(suppressed_id), len(self.sms.outbox)), ("SUPPRESSED", outbox))
            self.assert_error(self.confirm(suppressed_id, "246810", user_id=1), 422, "VERIFICATION_CODE_INVALID")
        with self.Session() as db:
            owners = db.query(service.VolunteerOnboardingBasic).filter_by(phone_e164=test_e164).all()
            self.assertEqual([owner.user_id for owner in owners], [3])
            self.assertEqual(db.get(PhoneVerification, uuid.UUID(suppressed_id)).attempt_count, 1)
        self.assertEqual(self.dispatcher.available(), self.dispatcher.capacity)
