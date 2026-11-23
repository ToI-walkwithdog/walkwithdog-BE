"""Acceptance tests for F-13 MyPage Walk Reservations (SC-10)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import timedelta
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from walk_record_test_support import START, WalkFixture


class WalkReservationsF13Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)

    # 1. Shelter approval/rejection and walk completion reflected across 5 MyPage tabs
    def test_mypage_tabs_reflect_shelter_approval_rejection_and_walk_completion(self):
        # 1) PENDING request (dog=1, +7d)
        req_pending_id = self.seed_request(
            user=1, dog=1, status="PENDING",
            start=START + timedelta(days=7), end=START + timedelta(days=7, hours=1),
        )

        # 2) APPROVED request, walk not started (dog=2, +14d)
        req_approved_id = self.seed_request(
            user=1, dog=2, status="PENDING",
            start=START + timedelta(days=14), end=START + timedelta(days=14, hours=1),
        )
        r_approve = self.client.patch(
            f"/walk-requests/{req_approved_id}",
            headers=self.auth(3),
            json={"action": "APPROVE"},
        )
        self.assertEqual(r_approve.status_code, 200, r_approve.text)
        self.assertEqual(r_approve.json()["status"], "APPROVED")

        # 3) REJECTED request (dog=8, +21d)
        req_rejected_id = self.seed_request(
            user=1, dog=8, status="PENDING",
            start=START + timedelta(days=21), end=START + timedelta(days=21, hours=1),
        )
        r_reject = self.client.patch(
            f"/walk-requests/{req_rejected_id}",
            headers=self.auth(3),
            json={"action": "REJECT", "rejection_reason_code": "SCHEDULE_CONFLICT"},
        )
        self.assertEqual(r_reject.status_code, 200, r_reject.text)
        self.assertEqual(r_reject.json()["status"], "REJECTED")

        # 4) COMPLETED request: APPROVED -> walk started and ended (dog=9, START)
        walk_completed = self.begin(user=1, dog=9)
        req_completed_id = walk_completed["walk_request_id"]
        self.advance(20 * 60)
        self.assertEqual(self.end(walk_completed["id"], user=1).status_code, 200)

        # ALL tab
        r_all = self.client.get("/walk-requests/my", params={"tab": "ALL"}, headers=self.auth(1))
        self.assertEqual(r_all.status_code, 200)
        all_items = {item["id"]: item for item in r_all.json()}
        self.assertEqual(set(all_items.keys()), {req_pending_id, req_approved_id, req_rejected_id, req_completed_id})
        self.assertEqual(all_items[req_pending_id]["display_status"], "PENDING")
        self.assertTrue(all_items[req_pending_id]["can_cancel"])
        self.assertEqual(all_items[req_approved_id]["display_status"], "APPROVED")
        self.assertTrue(all_items[req_approved_id]["can_cancel"])
        self.assertEqual(all_items[req_rejected_id]["display_status"], "REJECTED")
        self.assertFalse(all_items[req_rejected_id]["can_cancel"])
        self.assertEqual(all_items[req_completed_id]["display_status"], "COMPLETED")
        self.assertFalse(all_items[req_completed_id]["can_cancel"])
        self.assertIsNotNone(all_items[req_pending_id]["shelter_name"])

        # PENDING tab
        r_pending = self.client.get("/walk-requests/my", params={"tab": "PENDING"}, headers=self.auth(1))
        self.assertEqual([i["id"] for i in r_pending.json()], [req_pending_id])

        # APPROVED tab (excludes completed walks)
        r_approved = self.client.get("/walk-requests/my", params={"tab": "APPROVED"}, headers=self.auth(1))
        self.assertEqual([i["id"] for i in r_approved.json()], [req_approved_id])

        # REJECTED tab
        r_rejected = self.client.get("/walk-requests/my", params={"tab": "REJECTED"}, headers=self.auth(1))
        self.assertEqual([i["id"] for i in r_rejected.json()], [req_rejected_id])
        self.assertEqual(r_rejected.json()[0]["rejection_reason_code"], "SCHEDULE_CONFLICT")

        # COMPLETED tab
        r_completed = self.client.get("/walk-requests/my", params={"tab": "COMPLETED"}, headers=self.auth(1))
        self.assertEqual([i["id"] for i in r_completed.json()], [req_completed_id])

    # 2. User can cancel PENDING and APPROVED requests before walk starts
    def test_user_can_cancel_pending_and_approved_requests_before_walk_starts(self):
        req_pending = self.seed_request(
            user=1, dog=1, status="PENDING",
            start=START + timedelta(days=7), end=START + timedelta(days=7, hours=1),
        )
        req_approved = self.seed_request(
            user=1, dog=2, status="APPROVED",
            start=START + timedelta(days=14), end=START + timedelta(days=14, hours=1),
        )

        r_cancel_pending = self.client.patch(
            f"/walk-requests/{req_pending}",
            headers=self.auth(1),
            json={"action": "CANCEL"},
        )
        self.assertEqual(r_cancel_pending.status_code, 200, r_cancel_pending.text)
        self.assertEqual(r_cancel_pending.json()["status"], "CANCELLED")
        self.assertFalse(r_cancel_pending.json()["can_cancel"])

        r_cancel_approved = self.client.patch(
            f"/walk-requests/{req_approved}",
            headers=self.auth(1),
            json={"action": "CANCEL"},
        )
        self.assertEqual(r_cancel_approved.status_code, 200, r_cancel_approved.text)
        self.assertEqual(r_cancel_approved.json()["status"], "CANCELLED")
        self.assertTrue(r_cancel_approved.json()["cancelled_after_approval"])
        self.assertFalse(r_cancel_approved.json()["can_cancel"])

    # 3. Exception: cannot cancel once walk has started or finished (409)
    def test_cannot_cancel_once_walk_has_started_or_finished(self):
        walk = self.begin(user=1, dog=1)
        req_id = walk["walk_request_id"]

        # IN_PROGRESS -> 409
        r_in_progress = self.client.patch(
            f"/walk-requests/{req_id}",
            headers=self.auth(1),
            json={"action": "CANCEL"},
        )
        self.assertEqual(r_in_progress.status_code, 409)

        # COMPLETED -> 409
        self.advance(15 * 60)
        self.assertEqual(self.end(walk["id"], user=1).status_code, 200)
        r_completed = self.client.patch(
            f"/walk-requests/{req_id}",
            headers=self.auth(1),
            json={"action": "CANCEL"},
        )
        self.assertEqual(r_completed.status_code, 409)

    # 4. Role isolation: other user cannot cancel reservation (403)
    def test_other_user_cannot_cancel_reservation(self):
        req_id = self.seed_request(
            user=1, dog=1, status="PENDING",
            start=START + timedelta(days=7), end=START + timedelta(days=7, hours=1),
        )
        r_other = self.client.patch(
            f"/walk-requests/{req_id}",
            headers=self.auth(2),
            json={"action": "CANCEL"},
        )
        self.assertEqual(r_other.status_code, 403)


if __name__ == "__main__":
    unittest.main()
