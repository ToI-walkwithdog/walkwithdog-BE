"""Onboarding Step 2 dog experience (Issue #42) on isolated SQLite with synthetic accounts."""
from datetime import date, datetime, timedelta, timezone
import os
import unicodedata
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import dog_experience_service
    from app.auth import UNUSABLE_PASSWORD, token_digest
    from app.database import Base, get_db
    from app.models import (AuthSession, User, UserPledgeAcknowledgement, UserRole, VolunteerExperienceChallenge,
                            VolunteerOnboardingBasic, VolunteerOnboardingExperience)
    from app.onboarding_common import first_incomplete_step, get_clock
    from app.routers import onboarding_experience

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PLEDGE_VERSION = "pledge-test-v1"  # test-only value
NOW = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)
PATH = "/users/me/onboarding/dog-experience"
FULL = {"has_experience": True, "experienced_breeds_text": "믹스견, 푸들", "dog_size_code": "MEDIUM",
        "walking_frequency": "WEEKLY_3_4", "perceived_difficulty": "NORMAL",
        "challenge_codes": ["LEASH_PULLING", "OTHER_DOG_REACTION"]}


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class DogExperienceTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION})
        env.start()
        self.addCleanup(env.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.clock = FakeClock(NOW)
        app = FastAPI()
        app.include_router(onboarding_experience.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_clock] = lambda: self.clock
        self.client = TestClient(app)
        with self.Session.begin() as db:
            # 1 ready for Step 2, 2 without Step 1, 3 without pledge, 4 shelter, 5 admin, 6 age review
            for user_id, role, status in ((1, UserRole.USER, "ACTIVE"), (2, UserRole.USER, "ACTIVE"),
                                          (3, UserRole.USER, "ACTIVE"), (4, UserRole.SHELTER, "ACTIVE"),
                                          (5, UserRole.ADMIN, "ACTIVE"), (6, UserRole.USER, "AGE_REVIEW_REQUIRED")):
                db.add(User(id=user_id, login_id=f"user{user_id:02d}", nickname=f"사용자{user_id}",
                            password=UNUSABLE_PASSWORD, role=role, account_status=status))
            db.flush()
            for user_id in range(1, 7):
                db.add(AuthSession(token_hash=token_digest(f"token-{user_id}"), user_id=user_id,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
                if user_id != 3:
                    db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION, acknowledged_at=NOW))
            db.add(VolunteerOnboardingBasic(user_id=1, full_name="김민지", birth_date=date(2002, 12, 10),
                                            phone_e164="+821012345678", phone_verified_at=NOW, completed_at=NOW,
                                            updated_at=NOW))

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def put(self, body, user_id=1):
        headers = {"Authorization": f"Bearer token-{user_id}"} if user_id else {}
        return self.client.put(PATH, json=body, headers=headers)

    def get(self, user_id=1):
        headers = {"Authorization": f"Bearer token-{user_id}"} if user_id else {}
        return self.client.get(PATH, headers=headers)

    def assert_error(self, response, status, code, data=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["code"], code, response.text)
        if data is not None:
            self.assertEqual(response.json()["data"], data)

    def rows(self):
        with self.Session() as db:
            return (db.query(VolunteerOnboardingExperience).count(), db.query(VolunteerExperienceChallenge).count())

    # Saving and reading

    def test_no_experience_alone_completes_step2(self):
        before = self.get()
        self.assertEqual(before.json()["data"], {"step2_completed": False, "experience": None,
                                                 "next_onboarding_step": "PET_EXPERIENCE"})
        self.assertEqual(before.headers["cache-control"], "no-store")
        saved = self.put({"has_experience": False})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {"code": "OK", "message": "반려견 경험 입력이 완료되었습니다.", "data": {
            "step2_completed": True, "completed_at": NOW.isoformat(), "next_onboarding_step": "WALKING_ROUTINE"}})
        self.assertEqual(self.get().json()["data"]["experience"], {
            "has_experience": False, "experienced_breeds_text": None, "dog_size_code": None,
            "walking_frequency": None, "perceived_difficulty": None, "challenge_codes": [],
            "completed_at": NOW.isoformat()})
        self.assertEqual(self.rows(), (1, 0))
        with self.Session() as db:
            self.assertEqual(first_incomplete_step(db, db.get(User, 1)), "WALKING_ROUTINE")

    def test_full_experience_is_stored_and_read_back(self):
        body = {**FULL, "experienced_breeds_text": "  " + unicodedata.normalize("NFD", "믹스견, 푸들") + " "}
        self.assertEqual(self.put(body).status_code, 200)
        data = self.get().json()["data"]
        self.assertEqual(data["experience"], {**{k: v for k, v in FULL.items()}, "completed_at": NOW.isoformat()})
        self.assertEqual(data["next_onboarding_step"], "WALKING_ROUTINE")
        self.assertEqual(self.rows(), (1, 2))

    def test_allowed_detail_values(self):
        cases = [{"challenge_codes": []}, {"challenge_codes": ["NONE"]}, {"challenge_codes": None},
                 {"experienced_breeds_text": "모름"}, {"experienced_breeds_text": "가" * 200},
                 {"challenge_codes": ["LEASH_PULLING", "BARKING_EXCITEMENT", "OTHER_DOG_REACTION"]}]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.assertEqual(dog_experience_service.validate_experience({**FULL, **changes})["has_experience"], True)
        self.assertEqual(self.put({**FULL, "challenge_codes": ["NONE"]}).status_code, 200)

    def test_same_answer_again_keeps_the_first_time_and_other_answers_get_409(self):
        self.assertEqual(self.put(FULL).status_code, 200)
        self.clock.now = NOW + timedelta(hours=1)
        reordered = {**FULL, "challenge_codes": list(reversed(FULL["challenge_codes"]))}
        again = self.put(reordered)
        self.assertEqual((again.status_code, again.json()["data"]["completed_at"]), (200, NOW.isoformat()))
        for changes in ({"dog_size_code": "LARGE"}, {"challenge_codes": ["NONE"]}, {"walking_frequency": "DAILY"}):
            with self.subTest(changes=changes):
                self.assert_error(self.put({**FULL, **changes}), 409, "DOG_EXPERIENCE_ALREADY_COMPLETED", {})
        self.assert_error(self.put({"has_experience": False}), 409, "DOG_EXPERIENCE_ALREADY_COMPLETED")
        self.assertEqual(self.rows(), (1, 2))

    # Access order

    def test_previous_steps_come_before_the_body(self):
        for user_id, step in ((3, "PLEDGE"), (2, "BASIC_INFORMATION")):
            with self.subTest(step=step):
                for response in (self.get(user_id), self.put(FULL, user_id), self.put({}, user_id)):
                    self.assert_error(response, 409, "PREVIOUS_STEP_NOT_COMPLETED", {"required_step": step})
        self.assertEqual(self.get(2).json()["message"], "기본 정보 입력을 먼저 완료해 주세요.")
        self.assertEqual(self.rows(), (0, 0))

    def test_roles_status_and_authentication(self):
        for user_id in (4, 5):
            for response in (self.get(user_id), self.put(FULL, user_id)):
                self.assert_error(response, 403, "ONBOARDING_NOT_ALLOWED", {})
        for response in (self.get(6), self.put(FULL, 6)):
            self.assert_error(response, 403, "AGE_REVIEW_REQUIRED", {})
        for response in (self.get(None), self.put(FULL, None)):
            self.assert_error(response, 401, "AUTHENTICATION_REQUIRED", {})

    # Validation

    def test_validation_errors_in_order(self):
        cases = [
            ({}, "EXPERIENCE_REQUIRED", {"field": "has_experience"}),
            ({"has_experience": "true"}, "EXPERIENCE_REQUIRED", {"field": "has_experience"}),
            ({"has_experience": None}, "EXPERIENCE_REQUIRED", {"field": "has_experience"}),
            ({**FULL, "experienced_breeds_text": None}, "EXPERIENCED_BREED_REQUIRED", {"field": "experienced_breeds_text"}),
            ({**FULL, "experienced_breeds_text": "   "}, "EXPERIENCED_BREED_REQUIRED", {"field": "experienced_breeds_text"}),
            ({**FULL, "experienced_breeds_text": "가" * 201}, "INVALID_EXPERIENCED_BREED", {"field": "experienced_breeds_text"}),
            ({**FULL, "experienced_breeds_text": 12}, "INVALID_EXPERIENCED_BREED", {"field": "experienced_breeds_text"}),
            ({**FULL, "dog_size_code": None}, "DOG_SIZE_REQUIRED", {"field": "dog_size_code"}),
            ({**FULL, "dog_size_code": "HUGE"}, "INVALID_DOG_SIZE", {"field": "dog_size_code"}),
            ({**FULL, "dog_size_code": ["SMALL"]}, "INVALID_DOG_SIZE", {"field": "dog_size_code"}),
            ({**FULL, "dog_size_code": 1}, "INVALID_DOG_SIZE", {"field": "dog_size_code"}),
            ({**FULL, "dog_size_code": "small"}, "INVALID_DOG_SIZE", {"field": "dog_size_code"}),
            ({**FULL, "walking_frequency": None}, "WALKING_FREQUENCY_REQUIRED", {"field": "walking_frequency"}),
            ({**FULL, "walking_frequency": "HOURLY"}, "INVALID_WALKING_FREQUENCY", {"field": "walking_frequency"}),
            ({**FULL, "walking_frequency": "RARELY", "perceived_difficulty": None}, "DIFFICULTY_REQUIRED",
             {"field": "perceived_difficulty"}),
            ({**FULL, "perceived_difficulty": "IMPOSSIBLE"}, "INVALID_DIFFICULTY", {"field": "perceived_difficulty"}),
            ({**FULL, "challenge_codes": ["BITING"]}, "INVALID_CHALLENGE", {"field": "challenge_codes"}),
            ({**FULL, "challenge_codes": ["LEASH_PULLING", "LEASH_PULLING"]}, "INVALID_CHALLENGE", {"field": "challenge_codes"}),
            ({**FULL, "challenge_codes": "NONE"}, "INVALID_CHALLENGE", {"field": "challenge_codes"}),
            ({**FULL, "challenge_codes": ["NONE", "LEASH_PULLING"]}, "CHALLENGE_NONE_CONFLICT", {"field": "challenge_codes"}),
            # The first missing field in order is reported.
            ({"has_experience": True}, "EXPERIENCED_BREED_REQUIRED", {"field": "experienced_breeds_text"}),
        ]
        for body, code, data in cases:
            with self.subTest(body=body):
                self.assert_error(self.put(body), 422, code, data)
        for raw in (b"", b"[]", b"not json"):
            with self.subTest(raw=raw):
                response = self.client.put(PATH, content=raw, headers={"Authorization": "Bearer token-1",
                                                                      "Content-Type": "application/json"})
                self.assert_error(response, 422, "EXPERIENCE_REQUIRED", {"field": "has_experience"})
        self.assertEqual(self.rows(), (0, 0))

    def test_no_experience_with_details_is_refused(self):
        for field, value in (("experienced_breeds_text", "푸들"), ("dog_size_code", "SMALL"),
                             ("walking_frequency", "DAILY"), ("perceived_difficulty", "EASY"),
                             ("challenge_codes", ["NONE"])):
            with self.subTest(field=field):
                self.assert_error(self.put({"has_experience": False, field: value}), 422,
                                  "EXPERIENCE_DETAILS_NOT_ALLOWED", {})
        # Empty values are not details.
        empty = {"has_experience": False, "experienced_breeds_text": "", "dog_size_code": None, "challenge_codes": []}
        self.assertEqual(self.put(empty).status_code, 200)

    def test_old_dog_size_codes_field_is_ignored_for_now(self):
        # Open item in Issue #42: reject or ignore. Current behavior: unknown fields are ignored.
        body = {**FULL, "dog_size_codes": ["SMALL", "LARGE"]}
        self.assertEqual(self.put(body).status_code, 200)
        self.assertEqual(self.get().json()["data"]["experience"]["dog_size_code"], "MEDIUM")

    # Transactions and constraints

    def test_challenge_row_failure_rolls_back_the_main_row(self):
        def broken_row(user_id, challenge_code):
            return VolunteerExperienceChallenge(user_id=user_id, challenge_code="BROKEN")  # fails the CHECK

        with patch("app.dog_experience_service.VolunteerExperienceChallenge", side_effect=broken_row), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.put(FULL), 500, "INTERNAL_SERVER_ERROR", {})
        self.assertEqual(self.rows(), (0, 0))
        self.assertEqual(self.put(FULL).status_code, 200)

    def test_status_change_before_the_lock_refuses_the_save(self):
        original = dog_experience_service.lock_user

        def changed_meanwhile(db, user_id, **kwargs):
            with self.Session.begin() as other:
                other.get(User, user_id).account_status = "AGE_REVIEW_REQUIRED"
            return original(db, user_id, **kwargs)

        with patch("app.dog_experience_service.lock_user", side_effect=changed_meanwhile):
            self.assert_error(self.put(FULL), 403, "AGE_REVIEW_REQUIRED", {})
        self.assertEqual(self.rows(), (0, 0))

    def test_database_checks_reject_inconsistent_rows(self):
        bad_rows = [dict(has_experience=True, experienced_breeds_text="푸들", dog_size_code=None,
                         walking_frequency="DAILY", perceived_difficulty="EASY"),
                    dict(has_experience=False, experienced_breeds_text=None, dog_size_code="SMALL",
                         walking_frequency=None, perceived_difficulty=None),
                    dict(has_experience=True, experienced_breeds_text="푸들", dog_size_code="HUGE",
                         walking_frequency="DAILY", perceived_difficulty="EASY")]
        for values in bad_rows:
            with self.subTest(values=values), self.assertRaises(IntegrityError), self.Session.begin() as db:
                db.add(VolunteerOnboardingExperience(user_id=1, completed_at=NOW, updated_at=NOW, **values))


if __name__ == "__main__":
    unittest.main()
