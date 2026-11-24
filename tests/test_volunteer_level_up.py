"""Issue #97: policy configuration and the signed-in evaluation response."""
from datetime import timedelta
import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from test_walk_handover import HandoverFixture
from walk_request_test_support import START
from app.models import VolunteerProfile, Walk
from app.volunteer_level_service import LevelPolicy, get_level_policy, run_pending_evaluations
from app.walk_models import VolunteerLevelEvaluation, VolunteerLevelEvent, WalkHandover


POLICY_VERSION = "level-v1-test"
POLICY = LevelPolicy(POLICY_VERSION, START)
POLICY_ENV = {
    "VOLUNTEER_LEVEL_POLICY_VERSION": POLICY_VERSION,
    "VOLUNTEER_LEVEL_POLICY_START_AT": START.isoformat(),
}


class LevelFixture(HandoverFixture):
    def setup_level_api(self, engine):
        self.setup_handover_api(engine)
        self.enterContext(patch.dict(os.environ, POLICY_ENV))
        self.enterContext(patch("app.volunteer_level_service.utc_now", side_effect=self.record_clock))

    def seed_level_walk(self, day, *, user=1, status="COMPLETED", reason="USER", handover="COMPLETE",
                        required=True, submitted_by=None):
        start = START + timedelta(days=day)
        request = self.seed_request(user=user, status="APPROVED", start=start, end=start + timedelta(hours=1))
        with self.Session.begin() as db:
            ended_at = start + timedelta(minutes=10) if status != "IN_PROGRESS" else None
            walk = Walk(walk_request_id=request, started_at=start, status=status, end_reason=reason,
                        ended_at=ended_at, handover_required=required, duration_seconds=600, total_distance_m=123)
            db.add(walk)
            if handover is not None:
                complete = handover == "COMPLETE"
                walk.handover = WalkHandover(
                    volunteer_user_id=user if submitted_by is None else submitted_by,
                    handover_result=handover, dog_returned=True, equipment_returned=complete, condition_shared=True,
                    issue_description=None if complete else "합성 인계 문제",
                    self_confirmed_at=ended_at if complete else None,
                    issue_reported_at=None if complete else ended_at,
                    level_evaluation_pending_at=ended_at if complete and status == "COMPLETED" and reason == "USER"
                    else None,
                )
            db.flush()
            return walk.id

    def finish_and_handover(self):
        self.advance(60)
        ended = self.finish()
        submitted = self.handover()
        self.assertEqual(submitted.status_code, 201, submitted.text)
        return ended

    def run_level_batch(self, now=None):
        return run_pending_evaluations(self.Session, POLICY, now=now)

    def profile(self, user=1):
        response = self.client.get("/volunteers/me", headers=self.auth(user))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class LevelPolicyTests(unittest.TestCase):
    def test_policy_requires_explicit_complete_utc_configuration(self):
        with patch.dict(os.environ, {"VOLUNTEER_LEVEL_POLICY_VERSION": "", "VOLUNTEER_LEVEL_POLICY_START_AT": ""}):
            self.assertIsNone(get_level_policy())
        with patch.dict(os.environ, POLICY_ENV):
            self.assertEqual(get_level_policy(), POLICY)
        for start in ("", "2030-01-07", "2030-01-07T09:00:00+09:00", "invalid"):
            with self.subTest(start=start), patch.dict(os.environ, dict(POLICY_ENV, VOLUNTEER_LEVEL_POLICY_START_AT=start)):
                with self.assertRaises(ValueError):
                    get_level_policy()


class WalkLevelUpTests(LevelFixture, unittest.TestCase):
    def setUp(self):
        self.setup_level_api(create_engine("sqlite://", connect_args={"check_same_thread": False},
                                            poolclass=StaticPool))

    def test_summary_and_profile_do_not_evaluate_and_first_credit_has_no_event(self):
        self.assertEqual(self.profile()["level"], 1)
        self.advance(60)
        self.finish()
        self.assertEqual(self.summary().json()["level_evaluation_status"], "NOT_ELIGIBLE")
        submitted = self.handover()
        self.assertEqual(submitted.status_code, 201, submitted.text)
        self.assertEqual(self.summary().json()["level_evaluation_status"], "PENDING")
        self.assertEqual(self.profile()["level"], 1)
        self.assertEqual(self.rows(VolunteerLevelEvaluation), [])

        prior = [self.rows(model) for model in (Walk, WalkHandover, VolunteerProfile)]
        self.assertEqual(self.run_level_batch(), 1)
        self.assertEqual(self.run_level_batch(), 0)
        self.assertEqual(len(self.rows(VolunteerLevelEvaluation)), 1)
        self.assertEqual(self.rows(VolunteerLevelEvent), [])
        self.assertEqual([self.rows(model) for model in (Walk, WalkHandover, VolunteerProfile)], prior)
        summary = self.summary().json()
        self.assertEqual(summary["level_evaluation_status"], "COMPLETED")
        self.assertIsNone(summary["level_up_event"])

    def test_only_owner_can_retrieve_a_real_and_stable_level_event(self):
        self.finish_and_handover()
        self.seed_level_walk(1)
        milestone = self.seed_level_walk(2)
        self.advance(3 * 86400)
        self.assertEqual(self.run_level_batch(), 3)
        summary = self.summary(milestone).json()
        self.assertEqual(summary["level_evaluation_status"], "COMPLETED")
        event = summary["level_up_event"]
        self.assertEqual((event["before_level"], event["after_level"]), (1, 2))
        self.assertEqual(event["policy_version"], POLICY_VERSION)
        self.assertTrue(event["earned_at"].endswith("Z"))
        self.assertEqual(self.profile()["level"], 2)
        self.assertEqual(self.summary(milestone).json()["level_up_event"], event)
        for user in (2, 3, 5):
            self.assertEqual(self.summary(milestone, user=user).status_code, 403)
        self.assertNotIn("level_up_event", self.detail(milestone, user=3).json())

    def test_disabled_policy_and_pre_start_scan_do_not_create_rows(self):
        self.finish_and_handover()
        self.assertEqual(self.run_level_batch(now=START - timedelta(seconds=1)), 0)
        with patch.dict(os.environ, {"VOLUNTEER_LEVEL_POLICY_VERSION": "", "VOLUNTEER_LEVEL_POLICY_START_AT": ""}):
            summary = self.summary().json()
            self.assertEqual(summary["level_evaluation_status"], "NOT_ELIGIBLE")
            self.assertIsNone(summary["level_up_event"])
        self.assertEqual(self.rows(VolunteerLevelEvaluation), [])
