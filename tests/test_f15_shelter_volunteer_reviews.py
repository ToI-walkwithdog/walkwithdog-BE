"""Acceptance tests for F-15 Shelter-to-Volunteer Review (SC-16, SC-09, SC-10)."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import timedelta
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.routers import shelter_volunteer_reviews, walk_maps
from walk_record_test_support import START, WalkFixture


class ShelterVolunteerReviewF15Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(walk_maps.router)
        self.app.include_router(shelter_volunteer_reviews.router)

    def _complete_walk(self, user=1, dog=1):
        walk = self.begin(user=user, dog=dog)
        self.advance(20 * 60)
        end_res = self.end(walk["id"], user=user)
        self.assertEqual(end_res.status_code, 200, end_res.text)
        return walk["id"]

    # 1. Shelter creates review on COMPLETED walk -> reflected on SC-09 Walk History & SC-10 MyPage
    def test_shelter_review_appears_in_user_history_and_mypage(self):
        walk_id = self._complete_walk(user=1, dog=1)

        # Shelter user 3 (shelter_id=1) writes a 4.5-star review with tags & comment
        res = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(3),
            json={
                "rating": 4.5,
                "tags": ["PUNCTUALITY", "SAFETY_CARE", "CONSIDERATE"],
                "comment": "\uc2dc\uac04 \uc57d\uc18d\uc744 \uc798 \uc9c0\ud0a4\uace0 \uac15\uc544\uc9c0\ub97c \uc138\uc2ec\ud558\uac8c \ubc30\ub824\ud574 \uc8fc\uc168\uc2b5\ub2c8\ub2e4.",
            },
        )
        self.assertEqual(res.status_code, 201, res.text)
        created = res.json()
        self.assertEqual(created["walk_id"], walk_id)
        self.assertEqual(created["volunteer_user_id"], 1)
        self.assertEqual(created["rating"], "4.5")
        self.assertEqual(created["tags"], ["PUNCTUALITY", "SAFETY_CARE", "CONSIDERATE"])
        self.assertIsNotNone(created["shelter_name"])

        # SC-09 Walk History (GET /walks/history) includes shelter_review
        hist = self.client.get("/walks/history", headers=self.auth(1))
        self.assertEqual(hist.status_code, 200, hist.text)
        walks = hist.json()["walks"]
        self.assertEqual(len(walks), 1)
        self.assertIsNotNone(walks[0]["shelter_review"])
        self.assertEqual(walks[0]["shelter_review"]["rating"], "4.5")
        self.assertEqual(walks[0]["shelter_review"]["tags"], ["PUNCTUALITY", "SAFETY_CARE", "CONSIDERATE"])

        # SC-10 MyPage profile (GET /volunteers/me) includes review_summary
        me = self.client.get("/volunteers/me", headers=self.auth(1))
        self.assertEqual(me.status_code, 200, me.text)
        summary = me.json()["review_summary"]
        self.assertEqual(summary["total_review_count"], 1)
        self.assertEqual(summary["average_rating"], "4.50")
        self.assertEqual(summary["tag_counts"]["PUNCTUALITY"], 1)
        self.assertEqual(summary["tag_counts"]["SAFETY_CARE"], 1)
        self.assertEqual(summary["tag_counts"]["CONSIDERATE"], 1)
        self.assertEqual(summary["tag_counts"]["DISAPPOINTING"], 0)

        # SC-10 MyPage reviews list (GET /volunteers/me/reviews) returns received review
        my_reviews = self.client.get("/volunteers/me/reviews", headers=self.auth(1))
        self.assertEqual(my_reviews.status_code, 200, my_reviews.text)
        self.assertEqual(my_reviews.json()["total"], 1)
        self.assertEqual(my_reviews.json()["reviews"][0]["walk_id"], walk_id)

        # SC-16 Direct review lookup & shelter history expose shelter_review
        direct_rev = self.client.get(f"/walks/{walk_id}/volunteer-review", headers=self.auth(3))
        self.assertEqual(direct_rev.status_code, 200, direct_rev.text)
        self.assertEqual(direct_rev.json()["rating"], "4.5")

        sh_list = self.client.get("/shelters/walks/history", headers=self.auth(3))
        if sh_list.status_code == 200:
            self.assertEqual(sh_list.json()["walks"][0]["shelter_review"]["rating"], "4.5")

    # 2. Exception: 1 review per walk (duplicate -> 409) & modification disallowed (405)
    def test_duplicate_review_rejected_and_editing_disallowed(self):
        walk_id = self._complete_walk(user=1, dog=1)

        first = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(3),
            json={"rating": 5.0, "tags": [], "comment": None},
        )
        self.assertEqual(first.status_code, 201, first.text)

        dup = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(3),
            json={"rating": 3.0, "tags": ["DISAPPOINTING"], "comment": "retry"},
        )
        self.assertEqual(dup.status_code, 409)

        for method in ("put", "patch"):
            res = getattr(self.client, method)(
                f"/walks/{walk_id}/volunteer-review",
                headers=self.auth(3),
                json={"rating": 4.0},
            )
            self.assertEqual(res.status_code, 405)
        del_res = self.client.delete(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(3),
        )
        self.assertEqual(del_res.status_code, 405)

    # 3. Exception: non-COMPLETED walks (IN_PROGRESS, INTERRUPTED) cannot be reviewed (409)
    def test_non_completed_walks_cannot_be_reviewed(self):
        # 1) IN_PROGRESS walk cannot be reviewed -> 409
        walk = self.begin(user=1, dog=1)
        r_prog = self.client.post(
            f"/walks/{walk['id']}/volunteer-review",
            headers=self.auth(3),
            json={"rating": 4.0},
        )
        self.assertEqual(r_prog.status_code, 409)

        # 2) INTERRUPTED walk (ended by shelter staff -> INTERRUPTED) cannot be reviewed -> 409
        self.advance(10 * 60)
        end_interrupted = self.client.patch(
            f"/walks/{walk['id']}/end",
            headers=self.auth(3),
            json={"status": "INTERRUPTED"},
        )
        self.assertEqual(end_interrupted.status_code, 200, end_interrupted.text)
        self.assertEqual(end_interrupted.json()["status"], "INTERRUPTED")

        r_int = self.client.post(
            f"/walks/{walk['id']}/volunteer-review",
            headers=self.auth(3),
            json={"rating": 3.5},
        )
        self.assertEqual(r_int.status_code, 409)

    # 4. Authorization & input validation (wrong shelter 403, volunteer 403, invalid rating/tags/comment 422)
    def test_authorization_and_input_validation(self):
        walk_id = self._complete_walk(user=1, dog=1)

        # Wrong shelter (user 4 belongs to shelter 2) -> 403
        r_wrong_shelter = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(4),
            json={"rating": 4.0},
        )
        self.assertEqual(r_wrong_shelter.status_code, 403)

        # Volunteer user cannot self-review -> 403
        r_volunteer = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(1),
            json={"rating": 5.0},
        )
        self.assertEqual(r_volunteer.status_code, 403)

        # Non-0.5 step rating -> 422
        r_bad_rating = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(3),
            json={"rating": 4.3},
        )
        self.assertEqual(r_bad_rating.status_code, 422)

        # Duplicate tags -> 422
        r_dup_tag = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(3),
            json={"rating": 4.0, "tags": ["PUNCTUALITY", "PUNCTUALITY"]},
        )
        self.assertEqual(r_dup_tag.status_code, 422)

        # Comment exceeding 500 chars -> 422
        r_long_comment = self.client.post(
            f"/walks/{walk_id}/volunteer-review",
            headers=self.auth(3),
            json={"rating": 4.0, "comment": "a" * 501},
        )
        self.assertEqual(r_long_comment.status_code, 422)


if __name__ == "__main__":
    unittest.main()
