"""Issue #93: request validation, permissions, status policy and signed-in recovery."""
from datetime import timedelta
import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from walk_record_test_support import WalkFixture
from walk_request_test_support import START
from app.models import User, VolunteerProfile, Walk
from app.routers import walk_reviews
from app.walk_models import WalkHandover


COMPLETE = dict(handover_result="COMPLETE", dog_returned=True, equipment_returned=True, condition_shared=True)
ISSUE = dict(COMPLETE, handover_result="ISSUE", equipment_returned=False, issue_description="물품 분실")


class HandoverFixture(WalkFixture):
    def setup_handover_api(self, engine):
        self.setup_walk_api(engine)
        self.app.include_router(walk_reviews.router)
        self.walk_id = self.begin()["id"]

    def handover(self, walk_id=None, user=1, body=None):
        walk_id = self.walk_id if walk_id is None else walk_id
        return self.client.post(f"/walks/{walk_id}/handover", headers=self.auth(user),
                                json=COMPLETE if body is None else body)

    def summary(self, walk_id=None, user=1):
        walk_id = self.walk_id if walk_id is None else walk_id
        return self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(user))

    def history(self, user=1, **query):
        return self.client.get("/walks/history", headers=self.auth(user), params=query)

    def finish(self, walk_id=None, user=1, **body):
        walk_id = self.walk_id if walk_id is None else walk_id
        ended = self.end(walk_id, user=user, **body)
        self.assertEqual(ended.status_code, 200, ended.text)
        return ended.json()

    def handover_rows(self):
        with self.Session() as db:
            return db.query(WalkHandover).order_by(WalkHandover.id).all()


class WalkHandoverTests(HandoverFixture, unittest.TestCase):
    def setUp(self):
        self.setup_handover_api(create_engine("sqlite://", connect_args={"check_same_thread": False},
                                              poolclass=StaticPool))

    def seed_walk(self, day, *, user=1, required=True, status="COMPLETED", reason="USER"):
        start = START + timedelta(days=day)
        request = self.seed_request(user=user, status="APPROVED", start=start, end=start + timedelta(hours=1))
        with self.Session.begin() as db:
            walk = Walk(walk_request_id=request, started_at=start, status=status, end_reason=reason,
                        ended_at=None if status == "IN_PROGRESS" else start + timedelta(minutes=20),
                        handover_required=required)
            db.add(walk)
            db.flush()
            return walk.id

    def test_complete_handover_preserves_end_and_statistics_and_replays_existing_receipt(self):
        self.advance(120)
        self.assertEqual(self.upload(self.walk_id, [self.point(0), self.point(1, 60, longitude=0.001)])
                         .status_code, 200)
        ended = self.finish()
        self.assertEqual(self.detail(self.walk_id).json()["handover_status"], "NOT_CONFIRMED")
        self.assertIsNone(self.detail(self.walk_id).json()["handover"])
        self.assertEqual(self.summary().json()["handover_status"], "NOT_CONFIRMED")
        self.assertEqual(self.handover_rows(), [])
        walk_before = self.rows(Walk)
        profile_before = self.rows(VolunteerProfile)

        self.advance(180)
        created = self.handover()
        self.assertEqual(created.status_code, 201, created.text)
        receipt = created.json()
        self.assertEqual(receipt["handover_status"], "SELF_REPORTED")
        self.assertEqual(receipt["walk_id"], self.walk_id)
        self.assertEqual(receipt["self_confirmed_at"], "2030-01-07T00:03:00Z")
        self.assertIsNone(receipt["issue_reported_at"])
        self.assertNotIn("level_evaluation_pending_at", receipt)

        detail = self.detail(self.walk_id, query="?limit=1&offset=1").json()
        self.assertEqual(detail["walk"], ended)
        self.assertEqual(detail["handover_status"], "SELF_REPORTED")
        self.assertEqual(detail["handover"]["self_confirmed_at"], receipt["self_confirmed_at"])
        self.assertEqual((detail["point_count"], len(detail["points"])), (2, 1))
        self.assertEqual(self.summary().json()["handover_status"], "SELF_REPORTED")
        self.assertNotIn("handover", self.summary().json())
        self.assertEqual(self.history().json()["walks"][0]["handover_status"], "SELF_REPORTED")
        self.assertEqual(self.history(handover_status="NOT_CONFIRMED").json()["total"], 0)

        self.advance(300)
        replay = self.handover(body=dict(COMPLETE, issue_description="  "))
        self.assertEqual((replay.status_code, replay.json()), (200, receipt))
        self.assertEqual(self.handover(body=ISSUE).status_code, 409)
        row, = self.handover_rows()
        self.assertEqual(row.level_evaluation_pending_at, row.self_confirmed_at)
        self.assertEqual(row.volunteer_user_id, 1)
        self.assertEqual(self.rows(Walk), walk_before)
        self.assertEqual(self.rows(VolunteerProfile), profile_before)

    def test_issue_reports_trim_description_and_allow_idempotent_retry(self):
        self.finish()
        description = "가" * 500
        result = self.handover(body=dict(ISSUE, issue_description=" \t" + description + "\n"))
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["handover_status"], "ISSUE_REPORTED")
        self.assertEqual(result.json()["issue_description"], description)
        self.assertIsNone(result.json()["self_confirmed_at"])
        self.assertIsNotNone(result.json()["issue_reported_at"])
        self.assertEqual(self.handover(body=dict(ISSUE, issue_description=description)).json(), result.json())
        self.assertEqual(self.handover(body=COMPLETE).status_code, 409)
        self.assertIsNone(self.handover_rows()[0].level_evaluation_pending_at)

    def test_payload_rejects_coercion_missing_fields_extras_and_invalid_combinations(self):
        self.finish()
        invalid = [
            {}, dict(COMPLETE, dog_returned=1), dict(COMPLETE, condition_shared="true"),
            dict(COMPLETE, acknowledge_auto_timeout=None), dict(COMPLETE, volunteer_user_id=1),
            dict(COMPLETE, equipment_returned=False), dict(COMPLETE, issue_description="설명"),
            dict(ISSUE, equipment_returned=True), dict(ISSUE, issue_description=None),
            dict(ISSUE, issue_description="\t \n"), dict(ISSUE, issue_description="가" * 501),
        ]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.handover(body=body).status_code, 422)
        self.assertEqual(self.handover_rows(), [])

    def test_submission_and_reads_preserve_ownership_and_account_rules(self):
        self.finish()
        self.assertEqual(self.handover(user=0).status_code, 401)
        for user in (2, 3, 4, 5):
            with self.subTest(user=user):
                self.assertEqual(self.handover(user=user).status_code, 403)
        self.assertEqual(self.handover(walk_id=9999).status_code, 404)
        self.assertEqual(self.handover_rows(), [])
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "new-after-start"}):
            self.assertEqual(self.handover().status_code, 201)
        for user in (1, 3, 5):
            self.assertEqual(self.detail(self.walk_id, user=user).json()["handover_status"], "SELF_REPORTED")
        for user in (2, 4):
            self.assertEqual(self.detail(self.walk_id, user=user).status_code, 403)
        self.assertEqual(self.summary(user=3).status_code, 403)
        self.assertEqual(self.handover(user=2).status_code, 403)
        with self.Session.begin() as db:
            db.get(User, 1).account_status = "AGE_REVIEW_REQUIRED"
        self.assertEqual(self.handover().status_code, 403)

    def test_auto_timeout_requires_explicit_acknowledgement_only_for_complete(self):
        another = self.begin(user=6, dog=2)["id"]
        self.advance(3601)
        self.assertEqual(self.handover().status_code, 409)
        self.assertEqual(self.detail(self.walk_id).json()["walk"]["status"], "IN_PROGRESS")
        self.assertEqual(self.finish()["end_reason"], "AUTO_TIMEOUT")
        self.assertEqual(self.finish(another, user=6)["end_reason"], "AUTO_TIMEOUT")
        for body in (COMPLETE, dict(COMPLETE, acknowledge_auto_timeout=False),
                     dict(COMPLETE, acknowledge_auto_timeout="true")):
            self.assertEqual(self.handover(body=body).status_code, 422)
        self.assertEqual(self.handover(body=dict(COMPLETE, acknowledge_auto_timeout=True)).status_code, 201)
        self.assertTrue(self.handover_rows()[0].acknowledge_auto_timeout)
        self.assertEqual(self.handover(another, user=6, body=ISSUE).status_code, 201)
        self.assertTrue(all(row.level_evaluation_pending_at is None for row in self.handover_rows()))

    def test_interrupted_cancelled_and_legacy_walk_policies(self):
        self.advance(30)
        self.assertEqual(self.finish(user=3, status="INTERRUPTED")["end_reason"], "STAFF")
        self.assertEqual(self.handover().status_code, 409)
        self.assertEqual(self.handover(body=ISSUE).status_code, 201)
        legacy = self.seed_walk(1, required=False)
        cancelled = self.seed_walk(2, status="CANCELLED", reason=None)
        for walk_id in (legacy, cancelled):
            for payload in (COMPLETE, ISSUE):
                self.assertEqual(self.handover(walk_id, body=payload).status_code, 409)
        self.assertEqual(self.detail(legacy).json()["handover_status"], "NOT_REQUIRED")
        self.assertIsNone(self.detail(legacy).json()["handover"])
        self.assertEqual(self.summary(legacy).json()["handover_status"], "NOT_REQUIRED")
        self.assertIsNone(self.handover_rows()[0].level_evaluation_pending_at)

    def test_pending_history_filters_before_pagination_and_keeps_existing_filters(self):
        older = self.seed_walk(1)
        newer = self.seed_walk(2, status="INTERRUPTED", reason="STAFF")
        self.seed_walk(3, required=False)
        self.seed_walk(4, user=2)
        self.seed_walk(5, status="CANCELLED", reason=None)
        reported = self.seed_walk(6)
        self.assertEqual(self.handover(reported, body=ISSUE).status_code, 201)

        for offset, expected in enumerate((newer, older)):
            response = self.history(handover_status="NOT_CONFIRMED", limit=1, offset=offset)
            self.assertEqual(response.status_code, 200, response.text)
            history = response.json()
            self.assertEqual((history["total"], history["offset"]), (2, offset))
            self.assertEqual([item["walk_id"] for item in history["walks"]], [expected])
            self.assertEqual(history["walks"][0]["handover_status"], "NOT_CONFIRMED")
        past_end = self.history(handover_status="NOT_CONFIRMED", limit=1, offset=2).json()
        self.assertEqual((past_end["total"], past_end["walks"]), (2, []))
        filtered = self.history(handover_status="NOT_CONFIRMED", status="INTERRUPTED", end_reason="STAFF").json()
        self.assertEqual([item["walk_id"] for item in filtered["walks"]], [newer])
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(self.history(handover_status="SELF_REPORTED").status_code, 422)
        self.assertEqual(self.history(user=0, handover_status="NOT_CONFIRMED").status_code, 401)
        self.assertEqual(self.history(user=3, handover_status="NOT_CONFIRMED").status_code, 403)
        self.assertEqual(len(self.handover_rows()), 1)
