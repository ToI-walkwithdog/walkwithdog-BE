"""Acceptance tests for F-12 Walk History (SC-09, SC-16)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.models import Walk, WalkRequest
from app.routers import shelter_incidents, shelter_live_walks, walk_maps, walk_reviews
from walk_record_test_support import START, WalkFixture


class WalkHistoryF12Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        existing_paths = {getattr(route, "path", None) for route in self.app.routes}
        if "/shelters/me/incidents" not in existing_paths:
            self.app.include_router(shelter_incidents.router)
        if "/shelters/me/live-walks" not in existing_paths:
            self.app.include_router(shelter_live_walks.router)
        if "/users/{user_id}/walks" not in existing_paths:
            self.app.include_router(walk_maps.router)
        if "/walks/{walk_id}/summary" not in existing_paths:
            self.app.include_router(walk_reviews.router)

    def _clone_walk_with_status(
        self,
        template_walk_id: int,
        status: str,
        end_reason: str | None,
        offset_minutes: int,
    ) -> int:
        with self.Session.begin() as db:
            base_walk = db.get(Walk, template_walk_id)
            base_req = db.get(WalkRequest, base_walk.walk_request_id)
            req_kwargs = {
                col.name: getattr(base_req, col.name)
                for col in WalkRequest.__table__.columns
                if col.name not in ("id", "created_at", "updated_at")
            }
            req_kwargs["requested_start_at"] = base_req.requested_start_at + timedelta(minutes=offset_minutes)
            req_kwargs["requested_end_at"] = base_req.requested_end_at + timedelta(minutes=offset_minutes)
            new_req = WalkRequest(**req_kwargs)
            db.add(new_req)
            db.flush()

            started_at = START + timedelta(minutes=offset_minutes)
            ended_at = None if status == "IN_PROGRESS" else started_at + timedelta(minutes=25)
            new_walk = Walk(
                walk_request_id=new_req.id,
                status=status,
                end_reason=end_reason,
                started_at=started_at,
                ended_at=ended_at,
                total_distance_m=1200 if status != "IN_PROGRESS" else None,
                duration_seconds=1500 if status != "IN_PROGRESS" else None,
            )
            db.add(new_walk)
            db.flush()
            return new_walk.id

    # 1. 예외 처리: 이력이 없으면 200 OK와 빈 배열 반환
    def test_empty_history_returns_empty_list_for_user_and_shelter(self):
        r_user = self.client.get("/walks/history", headers=self.auth(1))
        self.assertEqual(r_user.status_code, 200)
        self.assertEqual(r_user.json()["total"], 0)
        self.assertEqual(r_user.json()["walks"], [])

        r_shelter = self.client.get("/shelters/me/walks", headers=self.auth(3))
        self.assertEqual(r_shelter.status_code, 200)
        self.assertEqual(r_shelter.json()["total"], 0)
        self.assertEqual(r_shelter.json()["walks"], [])

    # 2. COMPLETED, INTERRUPTED, AUTO_TIMEOUT 포함 및 started_at DESC 정렬 (IN_PROGRESS 제외)
    def test_history_includes_completed_interrupted_auto_timeout_and_orders_by_started_at_desc(self):
        walk_completed = self.begin(user=1, dog=1)
        w1_id = walk_completed["id"]
        self.advance(20 * 60)
        self.assertEqual(self.end(w1_id, user=1).status_code, 200)

        w2_id = self._clone_walk_with_status(
            template_walk_id=w1_id,
            status="INTERRUPTED",
            end_reason="STAFF",
            offset_minutes=60,
        )
        w3_id = self._clone_walk_with_status(
            template_walk_id=w1_id,
            status="INTERRUPTED",
            end_reason="AUTO_TIMEOUT",
            offset_minutes=120,
        )
        w_active_id = self._clone_walk_with_status(
            template_walk_id=w1_id,
            status="IN_PROGRESS",
            end_reason=None,
            offset_minutes=180,
        )

        # 사용자 이력 목록 (SC-09)
        r_user = self.client.get("/walks/history", headers=self.auth(1))
        self.assertEqual(r_user.status_code, 200)
        user_body = r_user.json()
        self.assertEqual(user_body["total"], 3)
        self.assertEqual([w["walk_id"] for w in user_body["walks"]], [w3_id, w2_id, w1_id])
        self.assertNotIn(w_active_id, [w["walk_id"] for w in user_body["walks"]])
        self.assertEqual(user_body["walks"][0]["end_reason"], "AUTO_TIMEOUT")
        self.assertEqual(user_body["walks"][1]["status"], "INTERRUPTED")
        self.assertTrue(user_body["walks"][2]["counted_as_volunteer_record"])
        self.assertFalse(user_body["walks"][0]["counted_as_volunteer_record"])

        # 보호소 이력 목록 (SC-16)
        r_shelter = self.client.get("/shelters/me/walks", headers=self.auth(3))
        self.assertEqual(r_shelter.status_code, 200)
        shelter_body = r_shelter.json()
        self.assertEqual(shelter_body["total"], 3)
        self.assertEqual([w["walk_id"] for w in shelter_body["walks"]], [w3_id, w2_id, w1_id])
        self.assertEqual(shelter_body["walks"][0]["end_reason"], "AUTO_TIMEOUT")
        self.assertIn("dog", shelter_body["walks"][0])
        self.assertIn("walker", shelter_body["walks"][0])

    # 3. 상세 조회 시 경로 지도, 행동마커, 평가 및 보호소 '사용자 신고' 탭 연동 검증
    def test_walk_history_detail_shows_route_markers_review_and_shelter_incidents(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # +3m: add behavior marker
        self.advance(180)
        r_marker = self.marker(
            walk_id,
            user=1,
            seconds=180,
            marker_type="BARKING",
            latitude=37.6587,
            longitude=126.8325,
            accuracy_m=10.0,
        )
        self.assertEqual(r_marker.status_code, 201, r_marker.text)

        # +4m: report incident
        self.advance(240)
        r_inc = self.incident(
            walk_id,
            user=1,
            seconds=240,
            incident_type="INJURY",
            description="\ubc1c\ubc14\ub2e5 \ucc30\uacfc\uc0c1 \ud655\uc778",
            latitude=37.6589,
            longitude=126.8328,
        )
        self.assertEqual(r_inc.status_code, 201, r_inc.text)

        # +5m: upload 2 GPS points
        self.advance(300)
        r_pts = self.upload(
            walk_id,
            [
                self.point(sequence=0, seconds=0, latitude=37.6584, longitude=126.8320, accuracy_m=8.0),
                self.point(sequence=1, seconds=300, latitude=37.6590, longitude=126.8330, accuracy_m=8.0),
            ],
            user=1,
        )
        self.assertEqual(r_pts.status_code, 200, r_pts.text)

        # +25m: end walk and submit review
        self.advance(1500)
        self.assertEqual(self.end(walk_id, user=1).status_code, 200)
        r_rev = self.client.put(
            f"/walks/{walk_id}/review",
            headers=self.auth(1),
            json={"rating": 5, "condition_tags": [], "comment": "활발하고 잘 걸었어요"},
        )
        self.assertIn(r_rev.status_code, (200, 201))

        # 사용자 상세 조회 (기존 GET /walks/{walk_id} & GET /walks/{walk_id}/summary)
        r_detail = self.client.get(f"/walks/{walk_id}", headers=self.auth(1))
        self.assertEqual(r_detail.status_code, 200)
        self.assertEqual(len(r_detail.json()["points"]), 2)
        self.assertEqual(len(r_detail.json()["markers"]), 1)

        r_summary = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertEqual(r_summary.status_code, 200)
        summary = r_summary.json()
        self.assertEqual(len(summary["points"]), 2)
        self.assertEqual(summary["summary"]["marker_count"], 1)
        self.assertEqual(summary["review"]["rating"], 5)

        # 보호소 이력 목록·상세 조회 (SC-16)
        r_sh_list = self.client.get("/shelters/me/walks", headers=self.auth(3))
        self.assertEqual(r_sh_list.status_code, 200)
        sh_item = r_sh_list.json()["walks"][0]
        self.assertEqual(sh_item["marker_count"], 1)
        self.assertEqual(sh_item["incident_count"], 1)
        self.assertEqual(sh_item["rating"], 5)
        self.assertTrue(sh_item["has_review"])

        r_sh_detail = self.client.get(f"/shelters/me/walks/{walk_id}", headers=self.auth(3))
        self.assertEqual(r_sh_detail.status_code, 200)
        sh_detail = r_sh_detail.json()
        self.assertEqual(len(sh_detail["points"]), 2)
        self.assertEqual(len(sh_detail["markers"]), 1)
        self.assertEqual(len(sh_detail["incidents"]), 1)
        self.assertEqual(sh_detail["review"]["rating"], 5)

        # 보호소 '사용자 신고' 탭 (기존 GET /shelters/me/incidents)
        r_sh_incidents = self.client.get("/shelters/me/incidents", headers=self.auth(3))
        self.assertEqual(r_sh_incidents.status_code, 200)
        self.assertEqual(len(r_sh_incidents.json()), 1)
        self.assertEqual(r_sh_incidents.json()[0]["walk_id"], walk_id)

    # 4. 권한 분리: 일반 사용자는 보호소 이력 접근 불가, 보호소 계정은 사용자 전용 이력 접근 불가
    def test_role_isolation_blocks_unauthorized_history_access(self):
        r_user_to_shelter = self.client.get("/shelters/me/walks", headers=self.auth(1))
        self.assertEqual(r_user_to_shelter.status_code, 403)

        r_shelter_to_user = self.client.get("/walks/history", headers=self.auth(3))
        self.assertEqual(r_shelter_to_user.status_code, 403)


if __name__ == "__main__":
    unittest.main()
