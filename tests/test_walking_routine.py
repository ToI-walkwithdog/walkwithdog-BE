"""Onboarding Step 3 walking routine (Issue #44) on isolated SQLite with synthetic accounts and regions.

Concurrency, row locks and the migration file run on PostgreSQL in test_signup_postgres.py.
"""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app import walking_routine_service
    from app.auth import UNUSABLE_PASSWORD, token_digest
    from app.database import Base, get_db
    from app.models import (DOG_SIZE_CODES, TIME_SLOT_CODES, WALKING_DURATION_CODES, AuthSession, User,
                            UserPledgeAcknowledgement, UserRole, VolunteerOnboardingBasic,
                            VolunteerOnboardingExperience, VolunteerOnboardingRoutine, VolunteerRoutineRegion,
                            VolunteerRoutineTimeSlot)
    from app.onboarding_common import first_incomplete_step, get_clock
    from app.region_models import LEVEL_SIDO, LEVEL_SIGUNGU, LEVEL_SPECIAL_SELECTABLE, Region
    from app.routers import onboarding_routine

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
PLEDGE_VERSION = "pledge-test-v1"  # test-only value
NOW = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)
PATH = "/users/me/onboarding/walking-routine"
FULL = {"region_codes": ["11680", "11110"], "available_time_slot_codes": ["WEEKDAY_MORNING", "WEEKEND_AFTERNOON"],
        "walking_duration_code": "BETWEEN_30_AND_60_MIN", "comfortable_dog_size_code": "MEDIUM"}
# Synthetic region master: active SIDO and SIGUNGU, 세종 전체, an inactive SIGUNGU and an
# active SIGUNGU under an inactive SIDO.
REGIONS = (("11", "서울특별시", LEVEL_SIDO, None, True), ("11110", "종로구", LEVEL_SIGUNGU, "11", True),
           ("11680", "강남구", LEVEL_SIGUNGU, "11", True), ("11990", "폐지구", LEVEL_SIGUNGU, "11", False),
           ("26", "부산광역시", LEVEL_SIDO, None, True), ("26350", "해운대구", LEVEL_SIGUNGU, "26", True),
           ("36", "세종특별자치시", LEVEL_SIDO, None, True),
           ("36_SPECIAL_ALL", "세종특별자치시 전체", LEVEL_SPECIAL_SELECTABLE, "36", True),
           ("27", "대구광역시", LEVEL_SIDO, None, False), ("27110", "중구", LEVEL_SIGUNGU, "27", True))

READY, NO_STEP2, NO_STEP1, NO_PLEDGE, SHELTER, ADMIN, AGE_REVIEW = 1, 2, 3, 4, 5, 6, 7


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class WalkingRoutineTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": PLEDGE_VERSION})
        env.start()
        self.addCleanup(env.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.clock = FakeClock(NOW)
        app = FastAPI()
        app.include_router(onboarding_routine.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_clock] = lambda: self.clock
        self.client = TestClient(app)
        with self.Session.begin() as db:
            for code, name, level, parent, active in REGIONS:
                db.add(Region(region_code=code, region_name=name, region_level=level, parent_region_code=parent,
                              is_active=active))
        accounts = {READY: (UserRole.USER, "ACTIVE", 3), NO_STEP2: (UserRole.USER, "ACTIVE", 2),
                    NO_STEP1: (UserRole.USER, "ACTIVE", 1), NO_PLEDGE: (UserRole.USER, "ACTIVE", 0),
                    SHELTER: (UserRole.SHELTER, "ACTIVE", 3), ADMIN: (UserRole.ADMIN, "ACTIVE", 3),
                    AGE_REVIEW: (UserRole.USER, "AGE_REVIEW_REQUIRED", 3)}
        for user_id, (role, status, steps) in accounts.items():
            self.add_user(user_id, role=role, status=status, steps=steps)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def add_user(self, user_id, *, role=UserRole.USER, status="ACTIVE", steps=3):
        """steps: 0 none, 1 pledge, 2 pledge and Step 1, 3 pledge, Step 1 and Step 2."""
        with self.Session.begin() as db:
            db.add(User(id=user_id, login_id=f"user{user_id:02d}", nickname=f"사용자{user_id}",
                        password=UNUSABLE_PASSWORD, role=role, account_status=status))
            db.flush()
            db.add(AuthSession(token_hash=token_digest(f"token-{user_id}"), user_id=user_id,
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            if steps >= 1:
                db.add(UserPledgeAcknowledgement(user_id=user_id, pledge_version=PLEDGE_VERSION, acknowledged_at=NOW))
            if steps >= 2:
                db.add(VolunteerOnboardingBasic(user_id=user_id, full_name="김민지", birth_date=date(2002, 12, 10),
                                                phone_e164=f"+8210000{user_id:04d}", phone_verified_at=NOW,
                                                completed_at=NOW, updated_at=NOW))
            if steps >= 3:
                db.add(VolunteerOnboardingExperience(user_id=user_id, has_experience=False, completed_at=NOW,
                                                     updated_at=NOW))

    def headers(self, user_id):
        return {"Authorization": f"Bearer token-{user_id}"} if user_id else {}

    def put(self, body, user_id=READY):
        return self.client.put(PATH, json=body, headers=self.headers(user_id))

    def get(self, user_id=READY):
        return self.client.get(PATH, headers=self.headers(user_id))

    def assert_error(self, response, status, code, data=None, message=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["code"], code, response.text)
        if data is not None:
            self.assertEqual(response.json()["data"], data)
        if message is not None:
            self.assertEqual(response.json()["message"], message)

    def rows(self, user_id=None):
        with self.Session() as db:
            counts = []
            for model in (VolunteerOnboardingRoutine, VolunteerRoutineRegion, VolunteerRoutineTimeSlot):
                query = db.query(model)
                if user_id is not None:
                    query = query.filter(model.user_id == user_id)
                counts.append(query.count())
            return tuple(counts)

    # Saving and reading

    def test_read_before_and_after_saving(self):
        before = self.get()
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.json(), {"code": "OK", "message": "산책 루틴 정보를 조회했습니다.", "data": {
            "step3_completed": False, "routine": None, "next_onboarding_step": "WALKING_ROUTINE"}})
        self.assertEqual(before.headers["cache-control"], "no-store")
        self.assertEqual(self.rows(), (0, 0, 0))  # reading writes nothing

        saved = self.put(FULL)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {"code": "OK", "message": "산책 루틴 입력이 완료되었습니다.", "data": {
            "step3_completed": True, "completed_at": NOW.isoformat(), "next_onboarding_step": None}})
        self.assertEqual(self.rows(), (1, 2, 2))

        after = self.get()
        self.assertEqual(after.json()["data"], {"step3_completed": True, "next_onboarding_step": None, "routine": {
            "region_codes": ["11110", "11680"], "available_time_slot_codes": ["WEEKDAY_MORNING", "WEEKEND_AFTERNOON"],
            "walking_duration_code": "BETWEEN_30_AND_60_MIN", "comfortable_dog_size_code": "MEDIUM",
            "completed_at": NOW.isoformat()}})
        with self.Session() as db:
            self.assertIsNone(first_incomplete_step(db, db.get(User, READY)))

    def test_region_and_time_slot_counts_and_kinds(self):
        cases = (
            (["26350"], ["WEEKEND_MORNING"]),                                   # one region, one slot
            (["11110", "26350", "36_SPECIAL_ALL"], list(TIME_SLOT_CODES)),       # three SIDOs, 세종 전체, four slots
            (["36_SPECIAL_ALL"], ["WEEKDAY_AFTERNOON", "WEEKDAY_MORNING"]),
        )
        for offset, (regions, slots) in enumerate(cases):
            user_id = 20 + offset
            self.add_user(user_id)
            with self.subTest(regions=regions, slots=slots):
                body = {**FULL, "region_codes": regions, "available_time_slot_codes": slots}
                response = self.put(body, user_id)
                self.assertEqual(response.status_code, 200, response.text)
                routine = self.get(user_id).json()["data"]["routine"]
                self.assertEqual(routine["region_codes"], sorted(regions))
                self.assertEqual(routine["available_time_slot_codes"],
                                 [code for code in TIME_SLOT_CODES if code in slots])
                self.assertEqual(self.rows(user_id), (1, len(regions), len(slots)))

    def test_manual_choice_alone_completes_the_onboarding(self):
        # No location lookup exists or is needed: one region picked from the list is enough.
        response = self.put({**FULL, "region_codes": ["11110"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["data"]["next_onboarding_step"])

    def test_unknown_fields_are_ignored_and_never_stored(self):
        body = {**FULL, "user_id": NO_STEP2, "completed_at": "2020-01-01T00:00:00+00:00", "source": "GPS",
                "latitude": 37.5, "longitude": 127.0}
        self.assertEqual(self.put(body).status_code, 200)
        with self.Session() as db:
            routine = db.query(VolunteerOnboardingRoutine).one()
            self.assertEqual(routine.user_id, READY)
            self.assertEqual(routine.completed_at.replace(tzinfo=timezone.utc), NOW)
        self.assertEqual(set(VolunteerOnboardingRoutine.__table__.columns.keys()),
                         {"user_id", "walking_duration_code", "comfortable_dog_size_code", "completed_at", "updated_at"})

    def test_same_answer_again_keeps_the_first_time_and_a_different_one_is_409(self):
        self.assertEqual(self.put(FULL).status_code, 200)
        self.clock.now = NOW + timedelta(hours=1)
        reordered = {**FULL, "region_codes": list(reversed(FULL["region_codes"])),
                     "available_time_slot_codes": list(reversed(FULL["available_time_slot_codes"]))}
        again = self.put(reordered)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["data"], {"step3_completed": True, "completed_at": NOW.isoformat(),
                                                "next_onboarding_step": None})
        for change in ({"region_codes": ["11110"]}, {"available_time_slot_codes": list(TIME_SLOT_CODES)},
                       {"walking_duration_code": "OVER_60_MIN"}, {"comfortable_dog_size_code": "SMALL"}):
            with self.subTest(change=change):
                self.assert_error(self.put({**FULL, **change}), 409, "WALKING_ROUTINE_ALREADY_COMPLETED", {},
                                  "산책 루틴 입력이 이미 완료되었습니다.")
        self.assertEqual(self.rows(), (1, 2, 2))
        self.assertEqual(self.get().json()["data"]["routine"]["walking_duration_code"], "BETWEEN_30_AND_60_MIN")

    # 422 rules (Notion codes, messages and data)

    def test_invalid_bodies_get_the_first_rule_in_order(self):
        def without(*fields):
            return {key: value for key, value in FULL.items() if key not in fields}

        regions = {"field": "region_codes"}
        slots = {"field": "available_time_slot_codes"}
        duration = {"field": "walking_duration_code"}
        size = {"field": "comfortable_dog_size_code"}
        cases = (
            (without("region_codes"), "REGIONS_REQUIRED", regions, "산책 지역을 한 개 이상 선택해 주세요."),
            ({**FULL, "region_codes": []}, "REGIONS_REQUIRED", regions, None),
            ({**FULL, "region_codes": None}, "REGIONS_REQUIRED", regions, None),
            ({**FULL, "region_codes": ["11110", "11680", "26350", "36_SPECIAL_ALL"]}, "REGION_LIMIT_EXCEEDED",
             {"field": "region_codes", "max": 3}, "산책 지역은 최대 3개까지 선택할 수 있습니다."),
            ({**FULL, "region_codes": ["11110", "11110"]}, "INVALID_REGION_CODE", regions, "산책 지역을 확인해 주세요."),
            ({**FULL, "region_codes": "11110"}, "INVALID_REGION_CODE", regions, None),
            ({**FULL, "region_codes": [11110]}, "INVALID_REGION_CODE", regions, None),
            ({**FULL, "region_codes": [""]}, "INVALID_REGION_CODE", regions, None),
            ({**FULL, "region_codes": ["1" * 21]}, "INVALID_REGION_CODE", regions, None),
            (without("available_time_slot_codes"), "TIME_SLOTS_REQUIRED", slots, "산책 가능한 시간대를 선택해 주세요."),
            ({**FULL, "available_time_slot_codes": []}, "TIME_SLOTS_REQUIRED", slots, None),
            ({**FULL, "available_time_slot_codes": [*TIME_SLOT_CODES, "WEEKDAY_MORNING"]}, "INVALID_TIME_SLOT", slots,
             "산책 시간대를 확인해 주세요."),
            ({**FULL, "available_time_slot_codes": ["WEEKDAY_MORNING", "WEEKDAY_MORNING"]}, "INVALID_TIME_SLOT",
             slots, None),
            ({**FULL, "available_time_slot_codes": ["WEEKDAY_EVENING"]}, "INVALID_TIME_SLOT", slots, None),
            ({**FULL, "available_time_slot_codes": ["weekday_morning"]}, "INVALID_TIME_SLOT", slots, None),
            (without("walking_duration_code"), "DURATION_REQUIRED", duration, "산책 가능 시간을 선택해 주세요."),
            ({**FULL, "walking_duration_code": " "}, "DURATION_REQUIRED", duration, None),
            ({**FULL, "walking_duration_code": "UNDER_30_MIN"}, "INVALID_DURATION", duration, "산책 시간을 확인해 주세요."),
            ({**FULL, "walking_duration_code": "30"}, "INVALID_DURATION", duration, None),
            ({**FULL, "walking_duration_code": 30}, "INVALID_DURATION", duration, None),
            (without("comfortable_dog_size_code"), "DOG_SIZE_REQUIRED", size, "반려견 크기를 선택해 주세요."),
            ({**FULL, "comfortable_dog_size_code": "XLARGE"}, "INVALID_DOG_SIZE", size, "반려견 크기를 확인해 주세요."),
            ({**FULL, "comfortable_dog_size_code": ["SMALL"]}, "INVALID_DOG_SIZE", size, None),
            # Presence and counts of every field come before duplicates and codes (Issue #44 order).
            ({**FULL, "region_codes": ["11110", "11110"], "walking_duration_code": None}, "DURATION_REQUIRED",
             duration, None),
            ({**FULL, "available_time_slot_codes": ["BAD"], "comfortable_dog_size_code": ""}, "DOG_SIZE_REQUIRED",
             size, None),
            ({**FULL, "region_codes": ["99999"], "walking_duration_code": "BAD"}, "INVALID_DURATION", duration, None),
            ([], "REGIONS_REQUIRED", regions, None),
            ({}, "REGIONS_REQUIRED", regions, None),
        )
        for body, code, data, message in cases:
            with self.subTest(body=body):
                self.assert_error(self.put(body), 422, code, data, message)
        no_body = self.client.put(PATH, headers=self.headers(READY))
        self.assert_error(no_body, 422, "REGIONS_REQUIRED", regions)
        broken = self.client.put(PATH, content=b"{", headers={**self.headers(READY), "Content-Type": "application/json"})
        self.assert_error(broken, 422, "REGIONS_REQUIRED", regions)
        self.assertEqual(self.rows(), (0, 0, 0))

    def test_region_master_refusals(self):
        # Inactive, a SIDO-level code (also 세종's own SIDO code), unknown, under an inactive SIDO.
        for codes in (["11990"], ["11"], ["36"], ["99999"], ["27110"], ["11110", "11990"]):
            with self.subTest(codes=codes):
                self.assert_error(self.put({**FULL, "region_codes": codes}), 422, "INVALID_REGION_CODE",
                                  {"field": "region_codes"}, "산책 지역을 확인해 주세요.")
        self.assertEqual(self.rows(), (0, 0, 0))
        with self.Session.begin() as db:
            db.get(Region, "11680").is_active = False  # a region sync deactivated a chosen code
        self.assert_error(self.put(FULL), 422, "INVALID_REGION_CODE")
        self.assertEqual(self.rows(), (0, 0, 0))

    # Earlier steps, roles and sessions

    def test_earlier_steps_are_required_for_reading_and_saving(self):
        expected = {NO_STEP2: ("PET_EXPERIENCE", "이전 단계를 먼저 완료해 주세요."),
                    NO_STEP1: ("BASIC_INFORMATION", "기본 정보 입력을 먼저 완료해 주세요."),
                    NO_PLEDGE: ("PLEDGE", "최초 산책 서약을 먼저 완료해 주세요.")}
        for user_id, (step, message) in expected.items():
            for name, call in (("GET", lambda: self.get(user_id)), ("PUT", lambda: self.put(FULL, user_id)),
                               ("PUT invalid", lambda: self.put({}, user_id))):
                with self.subTest(user=user_id, call=name):
                    self.assert_error(call(), 409, "PREVIOUS_STEP_NOT_COMPLETED", {"required_step": step}, message)
        self.assertEqual(self.rows(), (0, 0, 0))

    def test_other_roles_and_age_review_accounts_get_403_and_missing_sessions_401(self):
        for user_id in (SHELTER, ADMIN, AGE_REVIEW):
            for name, call in (("GET", lambda: self.get(user_id)), ("PUT", lambda: self.put(FULL, user_id)),
                               ("PUT invalid", lambda: self.put({}, user_id))):
                with self.subTest(user=user_id, call=name):
                    self.assert_error(call(), 403, "ONBOARDING_NOT_ALLOWED", {}, "사용자 정보 입력 권한이 없습니다.")
        for name, call in (("GET", lambda: self.get(None)), ("PUT", lambda: self.put(FULL, None))):
            with self.subTest(call=name):
                self.assert_error(call(), 401, "AUTHENTICATION_REQUIRED")
        with self.Session.begin() as db:
            db.get(AuthSession, token_digest(f"token-{READY}")).expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1))
        self.assert_error(self.get(), 401, "SESSION_EXPIRED")
        self.assert_error(self.put(FULL), 401, "SESSION_EXPIRED")
        self.assertEqual(self.rows(), (0, 0, 0))

    def test_status_change_after_the_route_check_refuses_the_save(self):
        original = walking_routine_service.lock_user

        def changed_meanwhile(db, user_id, **kwargs):
            with self.Session.begin() as other:  # a Step 1 birth date change commits first
                other.get(User, user_id).account_status = "AGE_REVIEW_REQUIRED"
            return original(db, user_id, **kwargs)

        with patch("app.walking_routine_service.lock_user", side_effect=changed_meanwhile):
            response = self.put(FULL)
        self.assert_error(response, 403, "ONBOARDING_NOT_ALLOWED")
        self.assertEqual(self.rows(), (0, 0, 0))

    def test_a_failed_link_row_rolls_everything_back(self):
        def broken_slot(user_id, time_slot_code):
            return VolunteerRoutineTimeSlot(user_id=user_id, time_slot_code="BROKEN")

        with patch("app.walking_routine_service.VolunteerRoutineTimeSlot", side_effect=broken_slot), \
                self.assertLogs("app.api_contract", "ERROR"):
            response = self.put(FULL)
        self.assert_error(response, 500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.rows(), (0, 0, 0))
        self.assertEqual(self.put(FULL).status_code, 200)

    # Step order and codes

    def test_first_incomplete_step_reports_the_earliest_missing_step(self):
        def routine(db, user_id):
            db.add(VolunteerOnboardingRoutine(user_id=user_id, walking_duration_code="UP_TO_30_MIN",
                                              comfortable_dog_size_code="SMALL", completed_at=NOW, updated_at=NOW))

        # A routine row stored without the earlier steps never hides them.
        with self.Session.begin() as db:
            for user_id in (NO_STEP2, NO_STEP1, NO_PLEDGE, READY, AGE_REVIEW):
                routine(db, user_id)
        with self.Session() as db:
            steps = {user_id: first_incomplete_step(db, db.get(User, user_id))
                     for user_id in (NO_STEP2, NO_STEP1, NO_PLEDGE, READY, AGE_REVIEW, SHELTER)}
        self.assertEqual(steps, {NO_STEP2: "PET_EXPERIENCE", NO_STEP1: "BASIC_INFORMATION", NO_PLEDGE: "PLEDGE",
                                 READY: None, AGE_REVIEW: "AGE_REVIEW", SHELTER: None})

    def test_duration_codes_and_the_30_and_60_minute_boundaries(self):
        self.assertEqual(WALKING_DURATION_CODES, ("UP_TO_30_MIN", "BETWEEN_30_AND_60_MIN", "OVER_60_MIN"))
        self.assertEqual(DOG_SIZE_CODES, ("SMALL", "MEDIUM", "LARGE"))
        boundaries = {1: "UP_TO_30_MIN", 29.9: "UP_TO_30_MIN", 30: "UP_TO_30_MIN", 30.01: "BETWEEN_30_AND_60_MIN",
                      31: "BETWEEN_30_AND_60_MIN", 59: "BETWEEN_30_AND_60_MIN", 59.99: "BETWEEN_30_AND_60_MIN",
                      60: "OVER_60_MIN", 61: "OVER_60_MIN", 180: "OVER_60_MIN"}
        for minutes, code in boundaries.items():
            with self.subTest(minutes=minutes):
                self.assertEqual(walking_routine_service.walking_duration_code(minutes), code)
        for invalid in (0, -1, True, "30", None, float("nan")):
            with self.subTest(minutes=invalid), self.assertRaises(ValueError):
                walking_routine_service.walking_duration_code(invalid)
        # Every code the boundary function returns is accepted by the API and the database.
        for offset, code in enumerate(WALKING_DURATION_CODES):
            self.add_user(30 + offset)
            response = self.put({**FULL, "walking_duration_code": code}, 30 + offset)
            self.assertEqual(response.status_code, 200, response.text)
        with self.assertRaises(IntegrityError), self.Session.begin() as db:
            db.add(VolunteerOnboardingRoutine(user_id=READY, walking_duration_code="UNDER_30_MIN",
                                              comfortable_dog_size_code="SMALL", completed_at=NOW, updated_at=NOW))

    def test_database_checks_reject_unknown_codes(self):
        bad = ({"walking_duration_code": "UNDER_30_MIN"}, {"walking_duration_code": "ANY"},
               {"comfortable_dog_size_code": "XLARGE"})
        for values in bad:
            row = {"walking_duration_code": "UP_TO_30_MIN", "comfortable_dog_size_code": "SMALL", **values}
            with self.subTest(values=values), self.assertRaises(IntegrityError), self.Session.begin() as db:
                db.add(VolunteerOnboardingRoutine(user_id=READY, completed_at=NOW, updated_at=NOW, **row))
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingRoutine(user_id=READY, walking_duration_code="OVER_60_MIN",
                                              comfortable_dog_size_code="LARGE", completed_at=NOW, updated_at=NOW))
        with self.assertRaises(IntegrityError), self.Session.begin() as db:
            db.add(VolunteerRoutineTimeSlot(user_id=READY, time_slot_code="WEEKDAY_EVENING"))

    def test_the_old_duration_code_is_absent_from_the_implementation(self):
        sources = [*sorted((ROOT / "app").rglob("*.py")), ROOT / "migrations" / "026_onboarding_walking_routine.sql"]
        found = [str(path.relative_to(ROOT)) for path in sources if "UNDER_30_MIN" in path.read_text(encoding="utf-8")]
        self.assertEqual(found, [])
        manifest = json.loads((ROOT / "migrations" / "manifest.json").read_text(encoding="utf-8"))
        entry = next(item for item in manifest["migrations"] if item["file"] == "026_onboarding_walking_routine.sql")
        self.assertEqual(entry["requires"], ["022_regions.sql", "025_onboarding_dog_experience.sql"])


if __name__ == "__main__":
    unittest.main()
