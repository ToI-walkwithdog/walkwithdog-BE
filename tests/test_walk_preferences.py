"""Issue #105: my page walk preferences (GET/PATCH /users/me/walk-preferences) on isolated
SQLite with the injected clock. Synthetic accounts only."""
from datetime import timedelta
import logging
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as OrmSession

from test_onboarding_basic_info import START, Step1TestCase
from app.account_status import change_to_age_review
from app.models import (User, VolunteerOnboardingRoutine, VolunteerRoutineRegion, VolunteerRoutineTimeSlot,
                        VolunteerWalkPreference)
from app.routers import walk_preferences
import app.walk_preference_service as walk_preference_service
from app.walking_routine_service import read_routine

PREFERENCES = "/users/me/walk-preferences"
UNSET = {"duration_code": None, "size_code": None}
NONE_SOURCES = {"duration_code": "NONE", "size_code": "NONE"}


class PreferenceTestCase(Step1TestCase):
    def setUp(self):
        super().setUp()
        self.app.include_router(walk_preferences.router)

    def get_preferences(self, user_id=1):
        return self.call("GET", PREFERENCES, user_id)

    def patch_preferences(self, body, user_id=1):
        return self.call("PATCH", PREFERENCES, user_id, json=body)

    def stored(self, user_id=1):
        with self.Session() as db:
            return db.get(VolunteerWalkPreference, user_id)

    def preference_rows(self):
        with self.Session() as db:
            return db.query(VolunteerWalkPreference).count()

    def complete_step3(self, user_id=1, duration="BETWEEN_30_AND_60_MIN", size="MEDIUM"):
        """A completed onboarding Step 3 (rows as #44 stores them; foreign keys are not enforced here)."""
        with self.Session.begin() as db:
            db.add(VolunteerOnboardingRoutine(user_id=user_id, walking_duration_code=duration,
                                              comfortable_dog_size_code=size, completed_at=START - timedelta(days=1),
                                              updated_at=START - timedelta(days=1)))
            db.flush()
            db.add_all([VolunteerRoutineRegion(user_id=user_id, region_code="11680"),
                        VolunteerRoutineRegion(user_id=user_id, region_code="26350"),
                        VolunteerRoutineTimeSlot(user_id=user_id, time_slot_code="WEEKDAY_MORNING")])

    def data(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        return response.json()["data"]


class PreferenceReadTests(PreferenceTestCase):
    def test_unconfigured_account_without_step3_has_no_values_and_reading_writes_nothing(self):
        data = self.data(self.get_preferences())
        self.assertEqual(data, {"configured": False, "version": 0, "preferences": None,
                                "effective_preferences": UNSET, "sources": NONE_SOURCES})
        self.assertEqual(self.preference_rows(), 0)

    def test_each_field_falls_back_to_the_completed_step3_value_on_its_own(self):
        self.complete_step3()
        data = self.data(self.get_preferences())
        self.assertEqual((data["configured"], data["preferences"]), (False, None))
        self.assertEqual(data["effective_preferences"], {"duration_code": "BETWEEN_30_AND_60_MIN", "size_code": "MEDIUM"})
        self.assertEqual(data["sources"], {"duration_code": "ONBOARDING", "size_code": "ONBOARDING"})
        self.assertEqual(self.preference_rows(), 0)  # Step 3 values are never copied
        saved = self.data(self.patch_preferences({"expected_version": 0, "size_code": "LARGE"}))
        self.assertEqual(saved["preferences"], {"duration_code": None, "size_code": "LARGE",
                                                "updated_at": START.isoformat()})
        self.assertEqual(saved["effective_preferences"], {"duration_code": "BETWEEN_30_AND_60_MIN", "size_code": "LARGE"})
        self.assertEqual(saved["sources"], {"duration_code": "ONBOARDING", "size_code": "PREFERENCE"})
        self.assertEqual(self.data(self.get_preferences())["effective_preferences"], saved["effective_preferences"])

    def test_roles_status_and_no_pledge_or_onboarding_requirement(self):
        self.assert_error(self.call("GET", PREFERENCES, None), 401, "AUTHENTICATION_REQUIRED")
        body = {"expected_version": 0, "size_code": "SMALL"}
        for user_id in (4, 5):  # shelter, admin
            self.assert_error(self.get_preferences(user_id), 403, "PREFERENCE_NOT_ALLOWED")
            self.assert_error(self.patch_preferences(body, user_id), 403, "PREFERENCE_NOT_ALLOWED")
            self.assert_error(self.patch_preferences({"bad": True}, user_id), 403, "PREFERENCE_NOT_ALLOWED")  # before 422
        with self.Session() as db:
            change_to_age_review(db, 2, START)
        self.assert_error(self.get_preferences(2), 403, "AGE_REVIEW_REQUIRED")
        self.assert_error(self.patch_preferences(body, 2), 403, "AGE_REVIEW_REQUIRED")
        # user 3 has no pledge and no onboarding step: reading and saving are allowed.
        before = self.call("GET", "/users/me", 3).json()["data"]
        self.assertEqual(self.data(self.patch_preferences(body, 3))["version"], 1)
        self.assertEqual(self.call("GET", "/users/me", 3).json()["data"], before)  # onboarding state unchanged
        self.assertEqual(self.preference_rows(), 1)

    def test_an_account_sees_only_its_own_preferences(self):
        self.data(self.patch_preferences({"expected_version": 0, "duration_code": "OVER_60_MIN"}, 1))
        other = self.data(self.get_preferences(2))
        self.assertEqual((other["configured"], other["version"], other["effective_preferences"]), (False, 0, UNSET))


class PreferencePatchTests(PreferenceTestCase):
    def test_first_save_every_code_and_one_field_changes(self):
        first = self.data(self.patch_preferences({"expected_version": 0, "duration_code": "UP_TO_30_MIN",
                                                  "size_code": "SMALL"}))
        self.assertEqual((first["configured"], first["version"], first["changed_fields"]),
                         (True, 1, ["duration_code", "size_code"]))
        version = 1
        for field, codes in (("duration_code", ("BETWEEN_30_AND_60_MIN", "OVER_60_MIN", "UP_TO_30_MIN")),
                             ("size_code", ("MEDIUM", "LARGE", "SMALL"))):
            for code in codes:
                with self.subTest(field=field, code=code):
                    self.clock.advance(minutes=1)
                    before = self.stored()
                    data = self.data(self.patch_preferences({"expected_version": version, field: code}))
                    version += 1
                    self.assertEqual((data["version"], data["changed_fields"], data["preferences"][field]),
                                     (version, [field], code))
                    other = "size_code" if field == "duration_code" else "duration_code"
                    self.assertEqual(getattr(self.stored(), other), getattr(before, other))  # kept when not sent
                    self.assertEqual(data["preferences"]["updated_at"], self.clock.now.isoformat())

    def test_same_values_are_a_no_op_and_keep_version_and_time(self):
        self.data(self.patch_preferences({"expected_version": 0, "duration_code": "OVER_60_MIN", "size_code": "LARGE"}))
        self.clock.advance(hours=1)
        for body in ({"expected_version": 1, "duration_code": "OVER_60_MIN"},
                     {"expected_version": 1, "duration_code": "OVER_60_MIN", "size_code": "LARGE"}):
            with self.subTest(body=body):
                data = self.data(self.patch_preferences(body))
                self.assertEqual((data["version"], data["changed_fields"], data["preferences"]["updated_at"]),
                                 (1, [], START.isoformat()))

    def test_a_stale_version_is_refused_even_with_the_same_values(self):
        self.assert_error(self.patch_preferences({"expected_version": 1, "size_code": "SMALL"}),
                          409, "PREFERENCE_VERSION_MISMATCH")  # nothing saved yet: only 0 creates
        self.assertEqual(self.preference_rows(), 0)
        self.data(self.patch_preferences({"expected_version": 0, "size_code": "SMALL"}))
        for body in ({"expected_version": 0, "size_code": "SMALL"}, {"expected_version": 0, "size_code": "LARGE"},
                     {"expected_version": 2, "size_code": "LARGE"}):
            with self.subTest(body=body):
                self.assert_error(self.patch_preferences(body), 409, "PREFERENCE_VERSION_MISMATCH")
        self.assertEqual((self.stored().version, self.stored().size_code), (1, "SMALL"))

    def test_payload_rules(self):
        self.data(self.patch_preferences({"expected_version": 0, "duration_code": "UP_TO_30_MIN", "size_code": "SMALL"}))
        for body, code in (
            ({"expected_version": 1}, "INVALID_PREFERENCE_PAYLOAD"),  # no field to change
            ({}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"duration_code": "OVER_60_MIN"}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": 1, "duration_code": None}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": 1, "size_code": None}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": 1, "size_code": "LARGE", "region_code": "11680"}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": 1, "size_code": "LARGE", "region_codes": ["11680"]}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": 1, "size_code": "LARGE", "available_time_slot_codes": []}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": True, "size_code": "LARGE"}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": "1", "size_code": "LARGE"}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": 1.5, "size_code": "LARGE"}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": -1, "size_code": "LARGE"}, "INVALID_PREFERENCE_PAYLOAD"),
            ({"expected_version": 1, "duration_code": ""}, "INVALID_DURATION"),
            ({"expected_version": 1, "duration_code": "UNDER_30_MIN"}, "INVALID_DURATION"),
            ({"expected_version": 1, "duration_code": 30}, "INVALID_DURATION"),
            ({"expected_version": 1, "duration_code": "30"}, "INVALID_DURATION"),
            ({"expected_version": 1, "duration_code": "up_to_30_min"}, "INVALID_DURATION"),
            ({"expected_version": 1, "size_code": ""}, "INVALID_DOG_SIZE"),
            ({"expected_version": 1, "size_code": "소형견"}, "INVALID_DOG_SIZE"),
            ({"expected_version": 1, "size_code": ["SMALL"]}, "INVALID_DOG_SIZE"),
        ):
            with self.subTest(body=body):
                self.assert_error(self.patch_preferences(body), 422, code)
        self.assert_error(self.call("PATCH", PREFERENCES, 1, json=[1]), 422, "INVALID_PREFERENCE_PAYLOAD")
        self.assert_error(self.call("PATCH", PREFERENCES, 1), 422, "INVALID_PREFERENCE_PAYLOAD")
        stored = self.stored()
        self.assertEqual((stored.version, stored.duration_code, stored.size_code, stored.updated_at.replace(tzinfo=None)),
                         (1, "UP_TO_30_MIN", "SMALL", START.replace(tzinfo=None)))

    def test_onboarding_records_and_status_are_never_changed(self):
        self.complete_step3(size="SMALL")
        with self.Session() as db:
            routine_before = read_routine(db, db.get(User, 1))
            user = db.get(User, 1)
            user_before = (user.profile_version, user.account_status, user.nickname)
        me_before = self.call("GET", "/users/me", 1).json()["data"]
        self.data(self.patch_preferences({"expected_version": 0, "duration_code": "OVER_60_MIN", "size_code": "LARGE"}))
        self.data(self.patch_preferences({"expected_version": 1, "size_code": "MEDIUM"}))
        with self.Session() as db:
            self.assertEqual(read_routine(db, db.get(User, 1)), routine_before)
            user = db.get(User, 1)
            self.assertEqual((user.profile_version, user.account_status, user.nickname), user_before)
        self.assertEqual(self.call("GET", "/users/me", 1).json()["data"], me_before)

    def test_database_errors_roll_back_and_save_nothing(self):
        failure = OperationalError("COMMIT", {}, Exception("synthetic commit failure"))
        with patch.object(OrmSession, "commit", side_effect=failure), self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.patch_preferences({"expected_version": 0, "size_code": "SMALL"}),
                              500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.preference_rows(), 0)
        # A lock timeout, deadlock or serialization failure under the users lock: the same 500.
        lock_timeout = OperationalError("SELECT", {}, Exception("canceling statement due to lock timeout"))
        with patch.object(walk_preference_service, "current_preference", side_effect=lock_timeout), \
                self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.patch_preferences({"expected_version": 0, "size_code": "SMALL"}),
                              500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.preference_rows(), 0)
        self.assertEqual(self.data(self.patch_preferences({"expected_version": 0, "size_code": "SMALL"}))["version"], 1)

    def test_a_primary_key_collision_on_first_save_is_a_version_conflict(self):
        # Any other integrity error (no row behind it) is not hidden as 409.
        other = IntegrityError("INSERT", {}, Exception("synthetic check violation"))
        with patch.object(OrmSession, "flush", side_effect=other), self.assertLogs("app.api_contract", "ERROR"):
            self.assert_error(self.patch_preferences({"expected_version": 0, "size_code": "SMALL"}),
                              500, "INTERNAL_SERVER_ERROR")
        self.assertEqual(self.preference_rows(), 0)
        self.data(self.patch_preferences({"expected_version": 0, "size_code": "SMALL"}))
        # As if another first save committed right after this request read "no row".
        with patch.object(walk_preference_service, "current_preference", return_value=None):
            self.assert_error(self.patch_preferences({"expected_version": 0, "size_code": "LARGE"}),
                              409, "PREFERENCE_VERSION_MISMATCH")
        self.assertEqual((self.stored().version, self.stored().size_code), (1, "SMALL"))

    def test_logs_carry_field_names_only(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.data(self.patch_preferences({"expected_version": 0, "duration_code": "OVER_60_MIN", "size_code": "LARGE"}))
        text = " ".join(record.getMessage() for record in records)
        for value in ("OVER_60_MIN", "LARGE", "token-1"):
            self.assertNotIn(value, text)
        audit = [record for record in records if getattr(record, "audit_event", None) == "WALK_PREFERENCES_UPDATED"]
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0].audit_user_id, audit[0].audit_fields, audit[0].audit_at),
                         (1, ["duration_code", "size_code"], START.isoformat()))
