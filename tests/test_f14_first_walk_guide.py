"""Acceptance tests for F-14 First Walk Guide (SC-03 Home & SC-04 Guide)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import timedelta
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.models import Survey
from app.routers import walk_guides
from walk_record_test_support import START, WalkFixture


class FirstWalkGuideF14Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(walk_guides.router)

    # 1. Home -> First Walk Guide 4 slides (handling, prohibited actions, waste disposal, report button)
    def test_get_first_walk_guide_returns_four_safety_slides(self):
        res = self.client.get("/walk-guides/first-walk", headers=self.auth(1))
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()

        self.assertEqual(body["total_slides"], 4)
        self.assertEqual(len(body["slides"]), 4)
        self.assertEqual(
            [s["category"] for s in body["slides"]],
            ["HANDLING", "PROHIBITED_ACTIONS", "WASTE_DISPOSAL", "EMERGENCY_REPORT"],
        )
        self.assertEqual(
            [s["title"] for s in body["slides"]],
            [
                "\uac15\uc544\uc9c0 \ub2e4\ub8e8\ub294 \ubc95",
                "\uae08\uc9c0 \ud589\ub3d9",
                "\ubc30\ubcc0 \ucc98\ub9ac",
                "\uc2e0\uace0 \ubc84\ud2bc \uc704\uce58",
            ],
        )
        for idx, slide in enumerate(body["slides"], start=1):
            self.assertEqual(slide["step"], idx)
            self.assertTrue(len(slide["tips"]) >= 2)
        self.assertFalse(body["is_completed"])
        self.assertIsNone(body["completed_at"])

    # 2. Tapping confirm on the last slide marks guide completed idempotently
    def test_complete_first_walk_guide_marks_completed_and_is_idempotent(self):
        r1 = self.client.post("/walk-guides/first-walk/complete", headers=self.auth(1))
        self.assertEqual(r1.status_code, 200, r1.text)
        body1 = r1.json()
        self.assertTrue(body1["is_completed"])
        self.assertIsNotNone(body1["completed_at"])
        self.assertFalse(body1["should_recommend_guide"])

        # Subsequent GET reflects completion
        r_get = self.client.get("/walk-guides/first-walk", headers=self.auth(1))
        self.assertEqual(r_get.status_code, 200)
        self.assertTrue(r_get.json()["is_completed"])
        self.assertEqual(r_get.json()["completed_at"], body1["completed_at"])

        # Subsequent POST preserves original completed_at timestamp
        self.advance(600)
        r2 = self.client.post("/walk-guides/first-walk/complete", headers=self.auth(1))
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["completed_at"], body1["completed_at"])

    # 3. Exception rule: user with no dog experience & no prior reservation gets recommendation before reading
    def test_recommends_guide_for_inexperienced_user_before_first_reservation(self):
        with self.Session.begin() as db:
            db.add(Survey(user_id=1, dog_experience="NONE"))
            db.add(Survey(user_id=2, dog_experience="EXPERIENCED"))

        # User 1: no dog experience + not completed + no prior reservation -> recommend True
        r_u1_before = self.client.get("/walk-guides/first-walk/status", headers=self.auth(1))
        self.assertEqual(r_u1_before.status_code, 200)
        self.assertTrue(r_u1_before.json()["should_recommend_guide"])
        self.assertIsNotNone(r_u1_before.json()["recommendation_message"])

        # User 2: has dog experience -> recommend False
        r_u2 = self.client.get("/walk-guides/first-walk/status", headers=self.auth(2))
        self.assertEqual(r_u2.status_code, 200)
        self.assertFalse(r_u2.json()["should_recommend_guide"])
        self.assertIsNone(r_u2.json()["recommendation_message"])

        # User 1 completes guide -> recommend becomes False
        r_done = self.client.post("/walk-guides/first-walk/complete", headers=self.auth(1))
        self.assertEqual(r_done.status_code, 200)
        self.assertFalse(r_done.json()["should_recommend_guide"])
        self.assertIsNone(r_done.json()["recommendation_message"])

    # 4. Recommendation turns off once user already has an active/completed reservation, and booking is never blocked
    def test_recommendation_clears_after_first_reservation_without_blocking_request(self):
        with self.Session.begin() as db:
            db.add(Survey(user_id=1, dog_experience="\uc5c6\uc74c"))

        # Before first reservation: recommended
        r_before = self.client.get("/walk-guides/first-walk/status", headers=self.auth(1))
        self.assertTrue(r_before.json()["should_recommend_guide"])

        # Creating a walk request is allowed (not blocked with 409)
        r_apply = self.client.post(
            "/walk-requests",
            headers=self.auth(1),
            json={
                "dog_id": 1,
                "requested_start_at": (START + timedelta(days=7)).isoformat(),
                "requested_end_at": (START + timedelta(days=7, hours=1)).isoformat(),
            },
        )
        self.assertEqual(r_apply.status_code, 201, r_apply.text)

        # After first reservation exists: no longer "before first reservation" -> should_recommend_guide False
        r_after = self.client.get("/walk-guides/first-walk/status", headers=self.auth(1))
        self.assertFalse(r_after.json()["should_recommend_guide"])


if __name__ == "__main__":
    unittest.main()
