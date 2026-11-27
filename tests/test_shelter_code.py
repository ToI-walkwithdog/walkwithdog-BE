"""Issue #87: shelter start code issue, verification and its use by POST /walks (SQLite)."""
from datetime import timedelta
import logging
import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

from walk_record_test_support import WalkFixture
from walk_request_test_support import START
from app.models import VolunteerOnboardingRoutine, Walk
from app.routers import shelter_codes
from app.shelter_code_models import ShelterCodeAttempt, ShelterCodeIssuance

TEST_KEY = "synthetic-shelter-code-key-for-tests-only-0123456789"


class ShelterCodeFixture(WalkFixture):
    def setup_code_api(self, engine):
        self.enterContext(patch.dict(os.environ, {"SHELTER_CODE_HMAC_KEY": TEST_KEY}))
        self.setup_walk_api(engine)
        self.app.include_router(shelter_codes.router)

    def issue(self, request_id, user=3):
        return self.client.post(f"/walk-requests/{request_id}/shelter-code", headers=self.auth(user))

    def verify(self, request_id, code, user=1, **kwargs):
        body = kwargs.get("body", {"shelter_code": code})
        return self.client.post(f"/walk-requests/{request_id}/shelter-code/verify", headers=self.auth(user), json=body)

    def issued_code(self, request_id, user=3):
        response = self.issue(request_id, user)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]["shelter_code"]

    def contract(self, response, status, code):
        self.assertEqual((response.status_code, response.json().get("code")), (status, code), response.text)

    def start_code(self, response, code):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], code)

    def failures(self, request_id, user=1):
        with self.Session() as db:
            row = db.get(ShelterCodeAttempt, (request_id, user))
            return None if row is None else (row.failure_count, row.locked_until is not None)

    def issuances(self, request_id):
        with self.Session() as db:
            return db.query(ShelterCodeIssuance).filter_by(walk_request_id=request_id).order_by(ShelterCodeIssuance.id).all()

    @staticmethod
    def wrong(code):
        return f"{(int(code) + 1) % 10 ** 6:06d}"


class ShelterCodeTests(ShelterCodeFixture, unittest.TestCase):
    def setUp(self):
        self.setup_code_api(create_engine("sqlite://", connect_args={"check_same_thread": False},
                                          poolclass=StaticPool))
        self.request_id = self.seed_request(status="APPROVED")

    def test_issue_verify_start_and_recover_the_same_reservation(self):
        issued = self.issue(self.request_id)
        self.contract(issued, 201, "SHELTER_CODE_ISSUED")
        self.assertEqual(issued.headers["cache-control"], "no-store")
        data = issued.json()["data"]
        self.assertEqual(data["request_id"], self.request_id)
        self.assertRegex(data["shelter_code"], r"^[0-9]{6}$")
        self.assertEqual(data["expires_at"], (START + timedelta(minutes=10)).isoformat())
        [row] = self.issuances(self.request_id)
        self.assertNotIn(data["shelter_code"], row.code_digest)
        self.assertEqual(len(row.code_digest), 64)

        verified = self.verify(self.request_id, data["shelter_code"])
        self.contract(verified, 200, "SHELTER_CODE_VERIFIED")
        self.assertEqual(verified.json()["data"], {"request_id": self.request_id, "verified_until": data["expires_at"]})
        self.assertNotIn(data["shelter_code"], verified.text)

        started = self.start(self.request_id, code=False)
        self.assertEqual(started.status_code, 201, started.text)
        self.assertEqual((started.json()["walk_request_id"], started.json()["status"]), (self.request_id, "IN_PROGRESS"))
        [row] = self.issuances(self.request_id)
        self.assertIsNotNone(row.used_at)
        active = self.client.get("/walks/active", headers=self.auth())
        self.assertEqual(active.json()["walk_request_id"], self.request_id)
        detail = self.client.get(f"/walk-requests/{self.request_id}", headers=self.auth())
        self.assertEqual(detail.json()["walk_id"], started.json()["id"])
        self.assertNotIn(data["shelter_code"], detail.text)
        self.assertNotIn('"shelter_code"', detail.text)

    def test_issue_permissions_ids_and_reservation_window(self):
        self.contract(self.client.post(f"/walk-requests/{self.request_id}/shelter-code"), 401, "AUTHENTICATION_REQUIRED")
        for user in (1, 2, 4, 7):  # volunteers, another shelter, a shelter account without a shelter
            self.contract(self.issue(self.request_id, user), 403, "WALK_REQUEST_NOT_ALLOWED")
        self.contract(self.issue(999), 404, "WALK_REQUEST_NOT_FOUND")
        for bad in ("abc", "0", "-1"):
            self.contract(self.issue(bad), 422, "INVALID_WALK_REQUEST_ID")
        for status in ("PENDING", "REJECTED", "CANCELLED"):
            with self.subTest(status=status):
                self.contract(self.issue(self.seed_request(dog=2, status=status)), 409, "WALK_REQUEST_NOT_ELIGIBLE")
        self.advance(-1)
        self.contract(self.issue(self.request_id), 409, "WALK_REQUEST_NOT_ELIGIBLE")  # before the start
        self.advance(3600)
        self.contract(self.issue(self.request_id), 409, "WALK_REQUEST_NOT_ELIGIBLE")  # end is excluded
        self.advance(0)
        by_admin = self.issue(self.request_id, user=5)
        self.contract(by_admin, 201, "SHELTER_CODE_ISSUED")
        self.contract(self.verify(self.request_id, by_admin.json()["data"]["shelter_code"]), 200, "SHELTER_CODE_VERIFIED")
        self.assertEqual(self.start(self.request_id, code=False).status_code, 201)
        self.contract(self.issue(self.request_id), 409, "WALK_REQUEST_NOT_ELIGIBLE")  # a Walk exists
        self.assertEqual(len(self.issuances(self.request_id)), 1)

    def test_expiry_is_capped_by_the_reservation_end(self):
        short = self.seed_request(dog=2, status="APPROVED", start=START, end=START + timedelta(minutes=4))
        issued = self.issue(short)
        self.assertEqual(issued.status_code, 201, issued.text)
        data = issued.json()["data"]
        self.assertEqual(data["expires_at"], (START + timedelta(minutes=4)).isoformat())

    def test_reissue_revokes_the_previous_code_and_its_verification(self):
        first = self.issued_code(self.request_id)
        self.contract(self.verify(self.request_id, first), 200, "SHELTER_CODE_VERIFIED")
        second = self.issued_code(self.request_id)
        rows = self.issuances(self.request_id)
        self.assertEqual([row.revoked_at is not None for row in rows], [True, False])
        self.start_code(self.start(self.request_id, code=False), "WALK_START_CODE_REQUIRED")
        if first != second:
            self.contract(self.verify(self.request_id, first), 422, "SHELTER_CODE_INVALID")
        self.contract(self.verify(self.request_id, second), 200, "SHELTER_CODE_VERIFIED")
        self.assertEqual(self.start(self.request_id, code=False).status_code, 201)

    def test_failure_limit_is_committed_kept_on_reissue_and_reset_after_block_or_success(self):
        code = self.issued_code(self.request_id)
        for count in range(1, 5):
            self.contract(self.verify(self.request_id, self.wrong(code)), 422, "SHELTER_CODE_INVALID")
            self.assertEqual(self.failures(self.request_id), (count, False))
        limited = self.verify(self.request_id, self.wrong(code))
        self.contract(limited, 429, "SHELTER_CODE_RATE_LIMITED")
        self.assertEqual(limited.json()["data"], {"retry_after_seconds": 300})
        self.assertEqual(limited.headers["retry-after"], "300")
        self.assertEqual(self.failures(self.request_id), (5, True))
        self.contract(self.verify(self.request_id, code), 429, "SHELTER_CODE_RATE_LIMITED")  # correct code is blocked
        code = self.issued_code(self.request_id)  # re-issuing never lifts the block
        self.advance(120)
        blocked = self.verify(self.request_id, code)
        self.contract(blocked, 429, "SHELTER_CODE_RATE_LIMITED")
        self.assertEqual(blocked.json()["data"]["retry_after_seconds"], 180)
        self.advance(301)
        self.contract(self.verify(self.request_id, self.wrong(code)), 422, "SHELTER_CODE_INVALID")
        self.assertEqual(self.failures(self.request_id), (1, False))  # a new count after the block
        self.contract(self.verify(self.request_id, code), 200, "SHELTER_CODE_VERIFIED")
        self.assertEqual(self.failures(self.request_id), (0, False))

    def test_format_role_and_reservation_errors_do_not_count(self):
        code = self.issued_code(self.request_id)
        for body in ({"shelter_code": "12345"}, {"shelter_code": "1234567"}, {"shelter_code": "abcdef"},
                     {"shelter_code": 123456}, {"shelter_code": "１２３４５６"}, {"shelter_code": " 12345"}, {}, None, []):
            with self.subTest(body=body):
                self.contract(self.verify(self.request_id, None, body=body), 422, "INVALID_SHELTER_CODE_FORMAT")
        self.contract(self.verify(self.request_id, code, user=2), 403, "WALK_REQUEST_NOT_ALLOWED")
        self.contract(self.verify(self.request_id, code, user=3), 403, "WALK_REQUEST_NOT_ALLOWED")
        self.contract(self.verify(999, code), 404, "WALK_REQUEST_NOT_FOUND")
        self.contract(self.verify("x", code), 422, "INVALID_WALK_REQUEST_ID")
        self.advance(3600)
        self.contract(self.verify(self.request_id, code), 409, "WALK_REQUEST_NOT_ELIGIBLE")
        self.assertIn(self.failures(self.request_id), (None, (0, False)))
        self.assertIsNone(self.failures(self.request_id, user=2))

    def test_a_code_of_another_reservation_is_rejected_and_starts_nothing(self):
        other = self.seed_request(user=2, dog=2, status="APPROVED")
        with patch("app.shelter_code_service.secrets.randbelow", side_effect=[111111, 222222]):
            mine, theirs = self.issued_code(self.request_id), self.issued_code(other)
        self.contract(self.verify(self.request_id, theirs), 422, "SHELTER_CODE_INVALID")
        self.contract(self.verify(other, theirs, user=1), 403, "WALK_REQUEST_NOT_ALLOWED")
        self.start_code(self.start(other, user=2, code=False), "WALK_START_CODE_REQUIRED")
        self.assertEqual(self.failures(self.request_id), (1, False))
        self.contract(self.verify(self.request_id, mine), 200, "SHELTER_CODE_VERIFIED")

    def test_leading_zero_and_repeated_valid_submission(self):
        with patch("app.shelter_code_service.secrets.randbelow", return_value=42):
            code = self.issued_code(self.request_id)
        self.assertEqual(code, "000042")
        self.contract(self.verify(self.request_id, "42"), 422, "INVALID_SHELTER_CODE_FORMAT")
        first = self.verify(self.request_id, code)
        self.advance(60)
        again = self.verify(self.request_id, code)
        self.assertEqual(first.json(), again.json())
        [row] = self.issuances(self.request_id)
        self.assertEqual(row.verified_at.replace(tzinfo=None), START.replace(tzinfo=None))

    def test_expired_or_missing_codes_fail_and_count(self):
        self.contract(self.verify(self.request_id, "123456"), 422, "SHELTER_CODE_INVALID")  # nothing issued
        code = self.issued_code(self.request_id)
        self.advance(600)
        self.contract(self.verify(self.request_id, code), 422, "SHELTER_CODE_INVALID")
        self.assertEqual(self.failures(self.request_id), (2, False))

    def test_start_requires_a_current_unused_verification(self):
        self.start_code(self.start(self.request_id, code=False), "WALK_START_CODE_REQUIRED")
        code = self.issued_code(self.request_id)
        self.start_code(self.start(self.request_id, code=False), "WALK_START_CODE_REQUIRED")  # issued, not verified
        self.verify(self.request_id, code)
        self.advance(600)
        self.start_code(self.start(self.request_id, code=False), "WALK_START_CODE_REQUIRED")  # verification expired
        self.assertEqual(self.rows(Walk), [])
        self.advance(0)
        self.assertEqual(self.start(self.request_id, code=False).status_code, 201)
        self.start_code(self.start(self.request_id, code=False), "WALK_START_ALREADY_STARTED")
        self.assertEqual(len(self.rows(Walk)), 1)
        self.assertEqual([row.used_at is not None for row in self.issuances(self.request_id)], [True])

    def test_cancelled_reservation_cannot_start_with_an_earlier_verification(self):
        self.verify(self.request_id, self.issued_code(self.request_id))
        self.advance(-60)
        self.assertEqual(self.act(self.request_id, "CANCEL", user=1).status_code, 200)
        self.advance(0)
        self.start_code(self.start(self.request_id, code=False), "WALK_START_BOOKING_NOT_ELIGIBLE")
        self.assertEqual(self.rows(Walk), [])

    def test_failed_start_commit_leaves_the_verification_unused(self):
        self.verify(self.request_id, self.issued_code(self.request_id))
        original = OrmSession.commit
        calls = []

        def fail_once(session):
            if not calls:
                calls.append(1)
                session.flush()
                raise OperationalError("COMMIT", {}, Exception("synthetic commit failure"))
            return original(session)

        with patch.object(OrmSession, "commit", fail_once):
            failed = self.start(self.request_id, code=False)
        self.assertEqual(failed.status_code, 500, failed.text)
        self.assertEqual(self.rows(Walk), [])
        self.assertIsNone(self.issuances(self.request_id)[0].used_at)
        self.assertEqual(self.start(self.request_id, code=False).status_code, 201)

    def test_onboarding_gate_still_applies_before_the_code_is_used(self):
        self.verify(self.request_id, self.issued_code(self.request_id))
        with self.Session.begin() as db:
            db.query(VolunteerOnboardingRoutine).filter_by(user_id=1).delete()
        response = self.start(self.request_id, code=False)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.rows(Walk), [])
        self.assertIsNone(self.issuances(self.request_id)[0].used_at)

    def test_missing_key_stores_nothing(self):
        with patch.dict(os.environ, {"SHELTER_CODE_HMAC_KEY": "too-short"}):
            self.contract(self.issue(self.request_id), 500, "INTERNAL_SERVER_ERROR")
            self.contract(self.verify(self.request_id, "123456"), 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.issuances(self.request_id), [])
        self.assertIsNone(self.failures(self.request_id))

    def test_codes_are_never_logged(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        code = self.issued_code(self.request_id)
        self.verify(self.request_id, self.wrong(code))
        self.verify(self.request_id, code)
        with patch.dict(os.environ, {"SHELTER_CODE_HMAC_KEY": ""}):
            self.verify(self.request_id, code)
        text = " ".join(record.getMessage() for record in records)
        self.assertNotIn(code, text)
        self.assertNotIn(self.wrong(code), text)
        self.assertNotIn(TEST_KEY, text)

    def test_openapi_documents_both_routes(self):
        paths = self.app.openapi()["paths"]
        issue = paths["/walk-requests/{request_id}/shelter-code"]["post"]["responses"]
        verify = paths["/walk-requests/{request_id}/shelter-code/verify"]["post"]["responses"]
        self.assertTrue({"201", "401", "403", "404", "409", "422", "500"} <= set(issue))
        self.assertTrue({"200", "401", "403", "404", "409", "422", "429", "500"} <= set(verify))


if __name__ == "__main__":
    unittest.main()
