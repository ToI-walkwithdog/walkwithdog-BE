"""Reservation history and home queries through the public API."""
from datetime import timedelta
import unittest

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    DogWalkSetting, Shelter, Survey, VolunteerExperienceChallenge, VolunteerOnboardingBasic,
    VolunteerOnboardingExperience, Walk, WalkRequest,
)
from walk_record_test_support import WalkFixture
from walk_request_test_support import END, NOW, START


class ReservationManagementTests(WalkFixture, unittest.TestCase):
    def setUp(self):
        self.setup_walk_api(create_engine("sqlite://", poolclass=StaticPool,
                                         connect_args={"check_same_thread": False}))
        self.request_clock.return_value = NOW

    def reservations(self, user=1, **filters):
        return self.client.get("/walk-requests/my", params=filters, headers=self.auth(user))

    def booking(self, request_id, user=1):
        return self.client.get(f"/walk-requests/{request_id}", headers=self.auth(user))

    def add_walk(self, request_id, status):
        with self.Session.begin() as db:
            db.add(Walk(walk_request_id=request_id, status=status, started_at=START,
                        ended_at=None if status == "IN_PROGRESS" else START))

    def test_mixed_filter_rejected_and_past_paginates_after_filtering(self):
        rejected = self.seed_request(status="REJECTED")
        cancelled = self.seed_request(status="CANCELLED")
        overdue = self.seed_request(start=NOW - timedelta(hours=2), end=NOW - timedelta(hours=1))
        self.seed_request(user=2, status="CANCELLED")
        self.assertEqual([row["id"] for row in self.reservations(status_group="PAST").json()], [cancelled, rejected])
        self.assertEqual([row["id"] for row in self.reservations(status_group="PAST", limit=1, offset=1).json()],
                         [rejected])
        pending = self.reservations(status="PENDING").json()
        self.assertEqual([(row["id"], row["can_cancel"]) for row in pending], [(overdue, False)])
        for filters in ({"status": "APPROVED", "tab": "ALL"}, {"status": "REJECTED", "status_group": "PAST"},
                        {"tab": "REJECTED", "status_group": "PAST"}):
            with self.subTest(filters=filters):
                response = self.reservations(**filters)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"]["code"], "RESERVATION_FILTER_CONFLICT")

    def test_approved_status_includes_finished_while_legacy_tab_excludes_them(self):
        approved = self.seed_request(status="APPROVED", dog=1)
        completed = self.seed_request(status="APPROVED", dog=2)
        interrupted = self.seed_request(status="APPROVED", dog=8)
        self.add_walk(completed, "COMPLETED")
        self.add_walk(interrupted, "INTERRUPTED")
        rows = self.reservations(status="APPROVED").json()
        self.assertEqual([(row["id"], row["display_status"]) for row in rows],
                         [(interrupted, "COMPLETED"), (completed, "COMPLETED"), (approved, "APPROVED")])
        self.assertEqual([row["id"] for row in self.reservations(tab="APPROVED").json()], [approved])
        self.assertEqual([row["id"] for row in self.reservations(tab="COMPLETED").json()], [interrupted, completed])
        inbox = self.client.get("/shelters/1/walk-requests?status=APPROVED", headers=self.auth(3))
        self.assertEqual([row["id"] for row in inbox.json()], [approved])

    def test_common_detail_applies_visit_privacy_and_preserves_rejection(self):
        approved = self.seed_request(status="APPROVED", dog=1)
        pending = self.seed_request(user=2, dog=2)
        rejected = self.seed_request(dog=8)
        self.assertEqual(self.act(rejected, "REJECT", rejection_reason_code="SCHEDULE_CONFLICT").status_code, 200)
        with self.Session.begin() as db:
            shelter = db.get(Shelter, 1)
            shelter.address, shelter.phone = "Synthetic address", "010-0000-0000"
            db.query(DogWalkSetting).filter_by(dog_id=1).one().max_walk_minutes = 40
            db.delete(db.get(VolunteerOnboardingExperience, 2))
            db.add(Survey(user_id=2, dog_experience="current_owner", walk_frequency="daily"))
        before = {
            model: self.rows(model)
            for model in (VolunteerOnboardingBasic, VolunteerOnboardingExperience,
                          VolunteerExperienceChallenge, Survey, WalkRequest)
        }
        expected_applicant = {
            "id": 1,
            "nickname": "사용자1",
            "name": None,
            "age": None,
            "dog_experience": None,
            "has_experience": False,
            "experienced_breed": None,
            "walk_frequency": None,
        }

        owner = self.booking(approved).json()
        self.assertEqual(owner["applicant"], expected_applicant)
        self.assertEqual(owner["shelter"], {"id": 1, "name": owner["shelter_name"]})
        self.assertEqual(owner["confirmed_visit"], {"address": "Synthetic address", "phone": "010-0000-0000"})
        self.assertEqual(owner["max_walk_minutes"], 40)
        self.assertEqual(owner["actions"], {"can_cancel": True, "can_enter_shelter_code": True})
        for staff in (3, 5):
            review = self.booking(approved, user=staff).json()
            self.assertEqual(review["applicant"], expected_applicant)
            self.assertIsNone(review["confirmed_visit"])
            self.assertTrue(review["can_cancel"])
            self.assertEqual(review["actions"], {"can_cancel": False, "can_enter_shelter_code": False})
        for other in (2, 4):
            self.assertEqual(self.booking(approved, user=other).status_code, 403)
        self.assertEqual(self.booking(999999).status_code, 404)
        listed = self.reservations().json()[0]
        self.assertNotIn("confirmed_visit", listed)
        self.assertNotIn("applicant", listed)
        waiting = self.booking(pending, user=2).json()
        self.assertEqual(waiting["applicant"], {
            **expected_applicant, "id": 2, "nickname": "사용자2", "has_experience": None,
        })
        self.assertIsNone(waiting["confirmed_visit"])
        self.assertIsNone(waiting["max_walk_minutes"])
        self.assertEqual(waiting["actions"], {"can_cancel": True, "can_enter_shelter_code": False})
        denied = self.booking(rejected).json()
        self.assertEqual(denied["rejection_reason_code"], "SCHEDULE_CONFLICT")
        self.assertTrue(denied["rejection_reason"])
        self.assertIsNone(denied["confirmed_visit"])
        self.advance(0)
        self.assertTrue(self.booking(approved).json()["actions"]["can_enter_shelter_code"])
        self.advance(3600)
        expired = self.booking(approved).json()
        self.assertIsNone(expired["confirmed_visit"])
        self.assertEqual(expired["actions"], {"can_cancel": False, "can_enter_shelter_code": False})
        self.assertEqual({model: self.rows(model) for model in before}, before)

    def test_cancel_saves_role_reason_and_time_without_rewriting_review(self):
        for actor, role, extra in (
            (1, "USER", {"cancel_reason_code": "SCHEDULE_CHANGED"}),
            (3, "SHELTER", {"cancel_reason_code": "CANNOT_VISIT"}),
            (5, "ADMIN", {"cancel_reason_code": None}),
        ):
            with self.subTest(role=role):
                request_id = self.seed_request(status="APPROVED")
                original = self.booking(request_id).json()
                response = self.act(request_id, "CANCEL", user=actor, **extra)
                self.assertEqual(response.status_code, 200, response.text)
                cancelled = response.json()
                expected = {**extra, "cancelled_actor_role": role, "cancelled_by": actor, "cancelled_at": original["reviewed_at"]}
                for field, value in expected.items():
                    self.assertEqual(cancelled[field], value)
                self.assertEqual(cancelled["reviewed_by"], 3)
                self.assertEqual(cancelled["reviewed_at"], original["reviewed_at"])
                self.assertTrue(cancelled["cancelled_after_approval"])
                duplicate = self.act(request_id, "CANCEL", user=1, cancel_reason_code="CANNOT_VISIT")
                self.assertEqual((duplicate.status_code, duplicate.json()["detail"]["code"]),
                                 (409, "REQUEST_ALREADY_CANCELLED"))
                recovered = self.booking(request_id).json()
                self.assertEqual({field: recovered[field] for field in expected}, expected)
                self.assertEqual(recovered["actions"], {"can_cancel": False, "can_enter_shelter_code": False})
                self.assertIsNone(recovered["confirmed_visit"])
        old = self.booking(self.seed_request(status="CANCELLED")).json()
        self.assertTrue(all(old[key] is None for key in expected))

    def test_cancel_enforces_permission_fields_time_and_every_walk_status(self):
        pending = self.seed_request()
        self.assertEqual(self.act(pending, "CANCEL", user=2).status_code, 403)
        self.assertEqual(self.act(pending, "CANCEL", user=4).status_code, 403)
        self.assertEqual(self.act(pending, "CANCEL", user=1, cancel_reason_code="OTHER").status_code, 422)
        self.assertEqual(self.act(pending, "APPROVE", cancel_reason_code=None).status_code, 422)
        self.assertEqual(self.act(pending, "REJECT", rejection_reason="Fixture", cancel_reason_code=None).status_code, 422)
        for dog, status in zip((2, 3, 8, 9), ("IN_PROGRESS", "COMPLETED", "INTERRUPTED", "CANCELLED")):
            with self.subTest(walk=status):
                linked = self.seed_request(status="APPROVED", dog=dog)
                self.add_walk(linked, status)
                self.assertFalse(self.booking(linked).json()["can_cancel"])
                response = self.act(linked, "CANCEL", user=1)
                self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "WALK_RECORD_EXISTS"))
        self.advance(-1)
        self.assertTrue(self.booking(pending).json()["can_cancel"])
        self.advance(0)
        self.assertFalse(self.booking(pending).json()["can_cancel"])
        response = self.act(pending, "CANCEL", user=1)
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "CANCEL_TIME_EXPIRED"))

    def test_home_filters_before_limit_and_recovers_the_same_reservation(self):
        eligible = self.seed_request(status="APPROVED")
        elapsed = self.seed_request(status="APPROVED", dog=2, start=START - timedelta(hours=1), end=START)
        self.seed_request(status="APPROVED", dog=8, start=END, end=END + timedelta(hours=1))
        self.seed_request(status="PENDING", dog=9)
        self.seed_request(user=2, status="APPROVED", dog=2)
        self.advance(-1)
        self.assertEqual([row["id"] for row in self.reservations(startable_only="true", limit=1).json()], [elapsed])
        self.advance(0)
        candidates = self.reservations(startable_only="true", limit=1).json()
        self.assertEqual([row["id"] for row in candidates], [eligible])
        self.assertEqual(self.client.get("/walks/active", headers=self.auth()).status_code, 404)
        started = self.start(eligible)
        self.assertEqual(started.status_code, 201, started.text)
        active = self.client.get("/walks/active", headers=self.auth()).json()
        self.assertEqual((active["walk_id"], active["walk_request_id"]), (started.json()["id"], eligible))
        self.assertEqual(self.reservations(startable_only="true", limit=1).json(), [])
        with self.Session() as db:
            self.assertEqual(db.get(WalkRequest, eligible).status, "APPROVED")
