"""Issue #103: owner profile (GET/PATCH /users/me/profile) and the profile phone-change
verification, on isolated SQLite with the injected clock and the mock SMS provider."""
from datetime import timedelta
import logging
import re
from unittest.mock import patch
import uuid

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as OrmSession

from test_onboarding_basic_info import OTHER_E164, OTHER_PHONE, PHONE, START, Step1TestCase
from app.account_status import change_to_age_review
from app.models import PhoneVerification, User, VolunteerOnboardingBasic
from app.routers import profile

PROFILE = "/users/me/profile"
PROFILE_SEND, PROFILE_CONFIRM = PROFILE + "/phone-verifications", PROFILE + "/phone-verifications/confirm"
THIRD_PHONE, THIRD_E164 = "010-5555-6666", "+821055556666"


def iso(value):
    return value.isoformat()


class ProfileTestCase(Step1TestCase):
    def setUp(self):
        super().setUp()
        self.app.include_router(profile.router)

    def get_profile(self, user_id=1):
        return self.call("GET", PROFILE, user_id)

    def patch_profile(self, body, user_id=1):
        return self.call("PATCH", PROFILE, user_id, json=body)

    def version(self, user_id=1):
        return self.user(user_id).profile_version

    def complete_step1(self, phone=PHONE, user_id=1):
        verification_id = self.verified(phone, user_id)
        self.assertEqual(self.save(verification_id, user_id).status_code, 200)
        return verification_id

    def profile_send(self, phone=OTHER_PHONE, user_id=1):
        return self.call("POST", PROFILE_SEND, user_id, json={"phone_number": phone})

    def profile_confirm(self, verification_id, code, user_id=1):
        return self.call("POST", PROFILE_CONFIRM, user_id, json={"verification_id": verification_id, "code": code})

    def profile_verified(self, phone=OTHER_PHONE, user_id=1):
        self.clock.advance(seconds=61)  # the shared resend wait
        sent = self.profile_send(phone, user_id)
        self.assertEqual(sent.status_code, 200, sent.text)
        verification_id = sent.json()["data"]["verification_id"]
        confirmed = self.profile_confirm(verification_id, self.last_code(), user_id)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        return verification_id

    def basic(self, user_id=1):
        with self.Session() as db:
            return db.get(VolunteerOnboardingBasic, user_id)

    def proof(self, verification_id):
        with self.Session() as db:
            return db.get(PhoneVerification, uuid.UUID(verification_id))


class ProfileReadTests(ProfileTestCase):
    def test_profile_before_and_after_step1_matches_users_me(self):
        response = self.get_profile()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.json()["data"], {"nickname": "봉사자1", "full_name": None, "phone_number": None,
                                                   "phone_verified_at": None, "profile_version": 0})
        self.complete_step1()
        data = self.get_profile().json()["data"]
        self.assertEqual((data["full_name"], data["phone_number"], data["profile_version"]),
                         ("김민지", "010-1234-5678", 1))
        self.assertEqual(data["phone_verified_at"], iso(START))
        self.assertEqual(self.call("GET", "/users/me", 1).json()["data"]["nickname"], data["nickname"])

    def test_step1_first_save_adds_one_version_and_its_replay_none(self):
        verification_id = self.complete_step1()
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.save(verification_id).status_code, 200)  # same request again
        self.assertEqual(self.version(), 1)

    def test_roles_status_and_no_pledge_requirement(self):
        self.assert_error(self.call("GET", PROFILE, None), 401, "AUTHENTICATION_REQUIRED")
        for user_id in (4, 5):  # shelter, admin
            self.assert_error(self.get_profile(user_id), 403, "PROFILE_UPDATE_NOT_ALLOWED")
            self.assert_error(self.profile_send(user_id=user_id), 403, "PROFILE_UPDATE_NOT_ALLOWED")
        with self.Session() as db:
            change_to_age_review(db, 2, START)
        self.assert_error(self.get_profile(2), 403, "AGE_REVIEW_REQUIRED")
        self.assert_error(self.patch_profile({"expected_profile_version": 0, "nickname": "새이름"}, 2),
                          403, "AGE_REVIEW_REQUIRED")
        # user 3 has no pledge: reading and a nickname change are still allowed (approved #103).
        self.assertEqual(self.get_profile(3).status_code, 200)
        changed = self.patch_profile({"expected_profile_version": 0, "nickname": "서약전"}, 3)
        self.assertEqual(changed.status_code, 200, changed.text)

    def test_profile_route_precedes_the_public_profile_route(self):
        from app.main import app
        paths = [getattr(route, "path", "") for route in app.routes]
        self.assertLess(paths.index("/users/me/profile"), paths.index("/users/{user_id}/profile"))


class ProfilePatchTests(ProfileTestCase):
    def test_nickname_and_name_changes_versions_and_no_ops(self):
        self.complete_step1()
        self.assert_error(self.patch_profile({"expected_profile_version": 0, "nickname": "산책왕"}),
                          409, "PROFILE_VERSION_MISMATCH")
        response = self.patch_profile({"expected_profile_version": 1, "nickname": " 산책왕 ", "full_name": "김민준"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual((data["nickname"], data["full_name"], data["profile_version"], data["changed_fields"]),
                         ("산책왕", "김민준", 2, ["nickname", "full_name"]))
        self.assertEqual(self.call("GET", "/users/me", 1).json()["data"]["nickname"], "산책왕")
        basic = self.basic()
        self.assertEqual((basic.birth_date.isoformat(), basic.phone_e164), ("2002-12-10", "+821012345678"))
        same = self.patch_profile({"expected_profile_version": 2, "nickname": "산책왕", "full_name": "김민준",
                                   "phone_number": "01012345678"})
        self.assertEqual((same.status_code, same.json()["data"]["changed_fields"]), (200, []))
        self.assertEqual(self.version(), 2)

    def test_name_or_phone_without_step1_is_refused_and_nothing_is_saved(self):
        self.assert_error(self.patch_profile({"expected_profile_version": 0, "nickname": "바뀐이름", "full_name": "김민지"}, 3),
                          409, "PROFILE_BASIC_INFO_REQUIRED")
        self.assertEqual(self.user(3).nickname, "미서약")
        self.assertIsNone(self.basic(3))
        self.assert_error(self.profile_send(user_id=2), 409, "PROFILE_BASIC_INFO_REQUIRED")

    def test_payload_rules(self):
        self.complete_step1()
        for body, code in (
            ({"expected_profile_version": 1, "nickname": ""}, "NICKNAME_REQUIRED"),
            ({"expected_profile_version": 1, "nickname": None}, "NICKNAME_REQUIRED"),
            ({"expected_profile_version": 1, "nickname": "ab"}, "INVALID_NICKNAME_LENGTH"),
            ({"expected_profile_version": 1, "nickname": "산책 왕"}, "INVALID_NICKNAME_FORMAT"),
            ({"expected_profile_version": 1, "full_name": ""}, "REQUIRED_FIELD"),
            ({"expected_profile_version": 1, "full_name": "가" * 11}, "INVALID_FULL_NAME"),
            ({"expected_profile_version": 1, "phone_number": None}, "PHONE_NUMBER_REQUIRED"),
            ({"expected_profile_version": 1, "phone_number": "02-123-4567"}, "INVALID_PHONE_NUMBER"),
            ({"expected_profile_version": 1, "email": "x@example.com"}, "INVALID_PROFILE_PAYLOAD"),
            ({"nickname": "산책왕"}, "INVALID_PROFILE_PAYLOAD"),
            ({"expected_profile_version": "1"}, "INVALID_PROFILE_PAYLOAD"),
            ({"expected_profile_version": True}, "INVALID_PROFILE_PAYLOAD"),
            ({"expected_profile_version": -1}, "INVALID_PROFILE_PAYLOAD"),
            ({"expected_profile_version": 1, "phone_verification_id": str(uuid.uuid4())}, "INVALID_PROFILE_PAYLOAD"),
            ({"expected_profile_version": 1, "phone_number": OTHER_PHONE, "phone_verification_id": "x"},
             "INVALID_PROFILE_PAYLOAD"),
        ):
            with self.subTest(body=body):
                self.assert_error(self.patch_profile(body), 422, code)
        self.assert_error(self.call("PATCH", PROFILE, 1, json=[1]), 422, "INVALID_PROFILE_PAYLOAD")
        self.assert_error(self.call("PATCH", PROFILE, 1), 422, "INVALID_PROFILE_PAYLOAD")
        self.assertEqual((self.user().nickname, self.version()), ("봉사자1", 1))

    def test_nickname_taken_by_another_account(self):
        self.assertEqual(self.patch_profile({"expected_profile_version": 0, "nickname": "walker"}, 2).status_code, 200)
        for nickname in ("walker", "WALKER", "미서약"):
            with self.subTest(nickname=nickname):
                self.assert_error(self.patch_profile({"expected_profile_version": 0, "nickname": nickname}),
                                  409, "NICKNAME_ALREADY_EXISTS")
        self.assertEqual(self.patch_profile({"expected_profile_version": 0, "nickname": "봉사자1"}).json()["data"]
                         ["changed_fields"], [])  # one's own nickname is not a conflict


class ProfilePhoneChangeTests(ProfileTestCase):
    def test_send_confirm_and_patch_change_the_phone_atomically(self):
        self.complete_step1()
        verification_id = self.profile_verified()
        verified_at = self.clock.now
        self.assertEqual(self.basic().phone_e164, "+821012345678")  # confirming changes nothing
        self.assertEqual(self.get_profile().json()["data"]["phone_number"], "010-1234-5678")
        self.clock.advance(minutes=3)
        response = self.patch_profile({"expected_profile_version": 1, "nickname": "번호바꿈", "phone_number": OTHER_PHONE,
                                       "phone_verification_id": verification_id})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual((data["phone_number"], data["nickname"], data["profile_version"], data["changed_fields"]),
                         ("010-9876-5432", "번호바꿈", 2, ["nickname", "phone_number"]))
        self.assertEqual(data["phone_verified_at"], iso(verified_at))  # the proof's time, not the save time
        self.assertIsNotNone(self.proof(verification_id).consumed_at)
        reused = self.patch_profile({"expected_profile_version": 2, "phone_number": THIRD_PHONE,
                                     "phone_verification_id": verification_id})
        self.assert_error(reused, 409, "PHONE_VERIFICATION_REQUIRED")

    def test_a_missing_or_unusable_proof_saves_nothing(self):
        self.complete_step1(user_id=1)
        self.complete_step1(phone=THIRD_PHONE, user_id=2)
        other_account_proof = self.profile_verified(phone="010-2222-3333", user_id=2)
        profile_proof = self.profile_verified()
        cases = (
            ({}, 409, "PHONE_VERIFICATION_REQUIRED"),
            ({"phone_verification_id": other_account_proof}, 404, "VERIFICATION_NOT_FOUND"),
            ({"phone_verification_id": str(uuid.uuid4())}, 404, "VERIFICATION_NOT_FOUND"),
        )
        for extra, status, code in cases:
            with self.subTest(extra=extra):
                body = {"expected_profile_version": 1, "nickname": "저장안됨", "phone_number": OTHER_PHONE, **extra}
                self.assert_error(self.patch_profile(body), status, code)
        different_number = {"expected_profile_version": 1, "nickname": "저장안됨", "phone_number": "010-4444-7777",
                            "phone_verification_id": profile_proof}
        self.assert_error(self.patch_profile(different_number), 409, "PHONE_VERIFICATION_REQUIRED")
        self.clock.advance(minutes=15)  # the save window after confirming is over
        late = {"expected_profile_version": 1, "nickname": "저장안됨", "phone_number": OTHER_PHONE,
                "phone_verification_id": profile_proof}
        self.assert_error(self.patch_profile(late), 409, "PHONE_VERIFICATION_REQUIRED")
        self.assertEqual((self.user().nickname, self.version(), self.basic().phone_e164),
                         ("봉사자1", 1, "+821012345678"))
        self.assertIsNone(self.proof(profile_proof).consumed_at)

    def test_purposes_cannot_be_used_for_each_other(self):
        self.complete_step1()
        self.clock.advance(seconds=61)
        onboarding = self.send(OTHER_PHONE).json()["data"]["verification_id"]
        self.assert_error(self.profile_confirm(onboarding, self.last_code()), 404, "VERIFICATION_NOT_FOUND")
        self.assertEqual(self.confirm(onboarding, self.last_code()).status_code, 200)
        body = {"expected_profile_version": 1, "phone_number": OTHER_PHONE, "phone_verification_id": onboarding}
        self.assert_error(self.patch_profile(body), 409, "PHONE_VERIFICATION_REQUIRED")
        self.clock.advance(seconds=61)
        profile_request = self.profile_send().json()["data"]["verification_id"]
        self.assert_error(self.confirm(profile_request, self.last_code()), 404, "VERIFICATION_NOT_FOUND")
        with self.Session.begin() as db:  # a verified profile proof offered to Step 1 of another account
            db.add(PhoneVerification(id=uuid.uuid4(), user_id=2, phone_e164="+821077778888", code_digest="0" * 64,
                                     send_status="SENT", provider_request_key=uuid.uuid4(),
                                     expires_at=self.clock.now + timedelta(minutes=5), attempt_count=0,
                                     verified_at=self.clock.now, verified_valid_until=self.clock.now + timedelta(minutes=15),
                                     created_at=self.clock.now, purpose="PROFILE_PHONE_CHANGE"))
        with self.Session() as db:
            proof_id = str(db.query(PhoneVerification.id).filter_by(user_id=2).scalar())
        self.assert_error(self.save(proof_id, user_id=2), 409, "PHONE_VERIFICATION_REQUIRED")
        self.assertIsNone(self.basic(2))

    def test_a_newer_request_of_either_purpose_replaces_the_proof(self):
        self.complete_step1()
        profile_proof = self.profile_verified()
        self.clock.advance(seconds=61)
        self.assertEqual(self.send(THIRD_PHONE).status_code, 200)  # a later onboarding-purpose request
        body = {"expected_profile_version": 1, "phone_number": OTHER_PHONE, "phone_verification_id": profile_proof}
        self.assert_error(self.patch_profile(body), 409, "PHONE_VERIFICATION_REQUIRED")

    def test_resend_wait_and_daily_limit_are_shared_by_both_purposes(self):
        self.complete_step1()  # one onboarding send at START
        self.assert_error(self.profile_send(), 429, "PHONE_VERIFICATION_RATE_LIMITED")
        for _ in range(4):  # four profile sends: five in total today
            self.clock.advance(seconds=61)
            self.assertEqual(self.profile_send(phone=OTHER_PHONE).status_code, 200)
        self.clock.advance(seconds=61)
        self.assert_error(self.profile_send(phone=THIRD_PHONE), 429, "PHONE_VERIFICATION_RATE_LIMITED")
        self.assert_error(self.send(THIRD_PHONE), 429, "PHONE_VERIFICATION_RATE_LIMITED")

    def test_a_number_of_another_account_is_refused(self):
        self.complete_step1(user_id=1)
        self.complete_step1(phone=OTHER_PHONE, user_id=2)
        self.clock.advance(seconds=61)
        self.assert_error(self.profile_send(OTHER_PHONE), 409, "PHONE_ALREADY_REGISTERED")

    def test_a_failed_commit_leaves_the_proof_unconsumed(self):
        self.complete_step1()
        verification_id = self.profile_verified()
        original, calls = OrmSession.commit, []

        def fail_once(session):
            if not calls:
                calls.append(1)
                session.flush()
                raise OperationalError("COMMIT", {}, Exception("synthetic commit failure"))
            return original(session)

        body = {"expected_profile_version": 1, "nickname": "번호바꿈", "phone_number": OTHER_PHONE,
                "phone_verification_id": verification_id}
        with patch.object(OrmSession, "commit", fail_once):
            failed = self.patch_profile(body)
        self.assertEqual(failed.status_code, 500, failed.text)
        self.assertIsNone(self.proof(verification_id).consumed_at)
        self.assertEqual((self.user().nickname, self.version(), self.basic().phone_e164),
                         ("봉사자1", 1, "+821012345678"))
        self.assertEqual(self.patch_profile(body).status_code, 200)

    def test_logs_never_carry_numbers_names_or_codes(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.complete_step1()
        verification_id = self.profile_verified()
        code = self.last_code()
        self.patch_profile({"expected_profile_version": 1, "full_name": "박서연", "phone_number": OTHER_PHONE,
                            "phone_verification_id": verification_id})
        text = " ".join(record.getMessage() for record in records)
        for secret in ("박서연", "김민지", "9876", "5432", "+8210", code, "test-only-hmac-key"):
            self.assertNotIn(secret, text)
        self.assertRegex(text, r"profile updated user_id=1 fields=full_name,phone_number")
