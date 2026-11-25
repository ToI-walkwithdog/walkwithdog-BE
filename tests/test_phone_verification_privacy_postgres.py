"""Issue #110 on a real, isolated PostgreSQL: migration 037, one number requested by several
accounts at once, no transaction held during the provider call, the users ->
phone_verifications lock order of the delivery job, and a late provider result racing a
verification. Synthetic accounts and numbers only."""
from concurrent.futures import ThreadPoolExecutor
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
from test_user_profile import THIRD_PHONE
from test_user_profile_postgres import IsolatedSchema
from app.models import PhoneVerification, PhoneVerificationDailyCount, UserPledgeAcknowledgement
from app import age_review_service
from app.api_contract import ContractError
import app.phone_verification_service as service
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
