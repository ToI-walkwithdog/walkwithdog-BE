"""Issue #95: private memo API, optimistic versions and read boundaries."""
from datetime import timedelta
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from test_walk_handover import ISSUE, HandoverFixture
from walk_request_test_support import START
from app.models import User, UserRole, Walk
from app.routers import shelter_incidents, shelter_live_walks, walk_maps
from app.walk_models import WalkPrivateMemo


MEMO = "산책 중에 물을 마셨다."


class PrivateMemoFixture(HandoverFixture):
    def setup_private_memo_api(self, engine):
        self.setup_handover_api(engine)
        self.enterContext(patch("app.walk_review_service.utc_now", side_effect=self.record_clock))

    def put_memo(self, memo=MEMO, version=0, *, walk_id=None, user=1, body=None):
        walk_id = self.walk_id if walk_id is None else walk_id
        payload = {"memo": memo, "expected_version": version} if body is None else body
        return self.client.put(f"/walks/{walk_id}/private-memo", headers=self.auth(user), json=payload)

    def finish_ready_walk(self):
        self.advance(60)
        ended = self.finish()
        accepted = self.handover()
        self.assertEqual(accepted.status_code, 201, accepted.text)
        return ended

    def seed_legacy_walk(self, status="COMPLETED", *, days_ago=7):
        start = START - timedelta(days=days_ago)
        request_id = self.seed_request(status="APPROVED", start=start, end=start + timedelta(hours=1))
        with self.Session.begin() as db:
            walk = Walk(walk_request_id=request_id, status=status, started_at=start,
                        ended_at=start + timedelta(minutes=10), end_reason="USER" if status == "COMPLETED" else None)
            db.add(walk)
            db.flush()
            return walk.id


class WalkPrivateMemoTests(PrivateMemoFixture, unittest.TestCase):
    def setUp(self):
        self.setup_private_memo_api(create_engine("sqlite://", connect_args={"check_same_thread": False},
                                                  poolclass=StaticPool))

    def test_save_update_and_replay_use_server_versions_and_timestamps(self):
        self.finish_ready_walk()
        self.assertIsNone(self.summary().json()["private_memo"])
        self.assertEqual(self.put_memo(version=1).status_code, 409)
        self.assertEqual(self.rows(WalkPrivateMemo), [])

        self.advance(120)
        created = self.put_memo("  " + MEMO + "\n")
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["cache-control"], "no-store")
        original = created.json()
        self.assertEqual(original, {
            "walk_id": self.walk_id, "memo": MEMO, "version": 1,
            "created_at": "2030-01-07T00:02:00Z", "updated_at": "2030-01-07T00:02:00Z",
        })
        summary = self.summary()
        self.assertEqual(summary.headers["cache-control"], "no-store")
        self.assertEqual(summary.json()["private_memo"], {k: v for k, v in original.items() if k != "walk_id"})

        self.advance(180)
        replay = self.put_memo()
        self.assertEqual((replay.status_code, replay.json()), (200, original))
        self.assertEqual(self.put_memo("새 버전", version=0).status_code, 409)
        updated = self.put_memo("새 버전", version=1)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json(), dict(original, memo="새 버전", version=2, updated_at="2030-01-07T00:03:00Z"))
        self.advance(240)
        retry = self.put_memo("새 버전", version=1)
        self.assertEqual((retry.status_code, retry.json()), (200, updated.json()))
        self.assertEqual(self.put_memo(version=1).status_code, 409)
        self.assertEqual(self.summary().json()["private_memo"]["version"], 2)
        with self.Session() as db:
            self.assertEqual(db.query(WalkPrivateMemo).one().author_user_id, 1)

    def test_validation_trim_unicode_length_and_empty_input_preserve_existing_memo(self):
        self.finish_ready_walk()
        created = self.put_memo(" 가 ")
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["memo"], "가")
        content = "가" * 500 + "\n" + "나" * 499
        updated = self.put_memo("\t " + content + " \n", version=1)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["memo"], content)
        before = self.rows(WalkPrivateMemo)

        invalid = [
            {}, {"memo": MEMO}, {"expected_version": 2},
            {"memo": " \t\n", "expected_version": 2},
            {"memo": "가" * 1001, "expected_version": 2},
            {"memo": 10, "expected_version": 2},
            {"memo": "가\x00나", "expected_version": 2},
            {"memo": MEMO, "expected_version": 2, "author_user_id": 2},
        ]
        invalid.extend({"memo": MEMO, "expected_version": version} for version in (-1, "2", 2.0, True, None))
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.put_memo(body=payload).status_code, 422)
        self.assertEqual(self.rows(WalkPrivateMemo), before)

    def test_policy_rejects_active_cancelled_and_unconfirmed_and_allows_issue_and_legacy(self):
        self.assertEqual(self.put_memo().status_code, 409)
        self.advance(60)
        self.finish(user=3, status="INTERRUPTED")
        self.assertEqual(self.put_memo().status_code, 409)
        self.assertEqual(self.handover(body=ISSUE).status_code, 201)
        self.assertEqual(self.put_memo().status_code, 201)

        legacy = self.seed_legacy_walk()
        self.assertEqual(self.summary(legacy).json()["handover_status"], "NOT_REQUIRED")
        self.assertEqual(self.put_memo(walk_id=legacy).status_code, 201)
        cancelled = self.seed_legacy_walk(status="CANCELLED", days_ago=14)
        self.assertEqual(self.put_memo(walk_id=cancelled).status_code, 409)
        self.assertEqual(len(self.rows(WalkPrivateMemo)), 2)

    def test_only_owner_can_write_or_read_memo_and_shared_responses_omit_it(self):
        for router in (walk_maps.router, shelter_live_walks.router, shelter_incidents.router):
            self.app.include_router(router)
        self.finish_ready_walk()
        secret = "private_memo_test_only_314"
        saved = self.put_memo(secret)
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(self.put_memo(user=0).status_code, 401)
        self.assertEqual(self.put_memo(walk_id=10000).status_code, 404)
        for user in (2, 3, 4, 5):
            with self.subTest(user=user):
                for denied in (self.put_memo(user=user), self.summary(user=user)):
                    self.assertEqual(denied.status_code, 403, denied.text)
                    self.assertNotIn(secret, denied.text)

        history_and_detail = [
            ("/walks/history", 1), (f"/walks/{self.walk_id}", 1), (f"/walks/{self.walk_id}", 3),
            (f"/walks/{self.walk_id}", 5), ("/users/1/walks", 1), ("/dogs/1/map", 2),
            ("/shelters/me/walks", 3), (f"/shelters/me/walks/{self.walk_id}", 3),
            ("/shelters/me/incidents", 3), (f"/walks/{self.walk_id}/review", 1),
        ]
        for path, user in history_and_detail:
            with self.subTest(path=path, user=user):
                response = self.client.get(path, headers=self.auth(user))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(secret, response.text)
                self.assertNotIn('"private_memo"', response.text)
        self.assertEqual(self.summary().json()["private_memo"]["memo"], secret)

        with self.Session.begin() as db:
            db.get(User, 1).role = UserRole.ADMIN
        promoted = self.summary()
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertIsNone(promoted.json()["private_memo"])
        self.assertNotIn(secret, promoted.text)
        self.assertEqual(self.put_memo().status_code, 403)
