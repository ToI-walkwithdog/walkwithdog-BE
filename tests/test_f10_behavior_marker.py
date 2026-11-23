"""Acceptance tests for F-10 behavior markers (SC-07 walking screen)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import timedelta
from unittest.mock import patch
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.models import Walk, WalkMarker
from app.routers import shelter_incidents, shelter_live_walks, walk_maps, walk_reviews
from walk_record_test_support import START, WalkFixture

LAT, LON = 37.5665, 126.9780


class BehaviorMarkerF10Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(shelter_incidents.router)
        self.app.include_router(shelter_live_walks.router)
        self.app.include_router(walk_reviews.router)
        self.app.include_router(walk_maps.router)

    def behavior(self, wid, marker_type="POOP", user=1, seconds=0, **fields):
        """Marker request; coordinates are only sent when given (they are optional in F-10)."""
        body = {"marker_type": marker_type, "recorded_at": (START + timedelta(seconds=seconds)).isoformat(), **fields}
        return self.client.post(f"/walks/{wid}/markers", headers=self.auth(user), json=body)

    def walking(self, with_point=False):
        walk = self.begin()
        if with_point:
            self.advance(5)
            self.assertEqual(self.upload(walk["id"], [self.point(0, 5, LAT, LON)]).status_code, 200)
        return walk["id"]

    # 1. 행동 선택 4종 저장
    def test_four_behavior_types_are_saved_with_current_location(self):
        walk_id = self.walking()
        for marker_type, memo in (("POOP", None), ("CROSSWALK_WAIT", None), ("SNIFF", None), ("OTHER", "낙엽 먹음")):
            response = self.behavior(walk_id, marker_type, latitude=LAT, longitude=LON, memo=memo)
            self.assertEqual(response.status_code, 201, response.text)
            data = response.json()
            self.assertEqual((data["marker_type"], data["memo"], data["location_source"]), (marker_type, memo, "CURRENT"))
            self.assertEqual((data["latitude"], data["longitude"]), ("37.566500", "126.978000"))
            self.assertEqual(data["walk_id"], walk_id)
        self.assertEqual(len(self.rows(WalkMarker)), 4)

    def test_legacy_marker_types_are_still_accepted(self):
        walk_id = self.walking()
        for marker_type in ("DOG_ENCOUNTER", "PERSON_ENCOUNTER", "BARKING"):
            self.assertEqual(self.behavior(walk_id, marker_type, latitude=LAT, longitude=LON, memo="관찰").status_code, 201)

    # 2. 기타 선택 시 내용이 비면 저장 불가
    def test_other_requires_non_blank_content(self):
        walk_id = self.walking()
        for extra in ({}, {"memo": None}, {"memo": ""}, {"memo": "   "}, {"memo": "\n\t"}):
            response = self.behavior(walk_id, "OTHER", latitude=LAT, longitude=LON, **extra)
            self.assertEqual(response.status_code, 422, extra)
        self.assertEqual(self.rows(WalkMarker), [])
        self.assertEqual(self.behavior(walk_id, "OTHER", latitude=LAT, longitude=LON, memo="가" * 101).status_code, 422)
        self.assertEqual(self.behavior(walk_id, "OTHER", latitude=LAT, longitude=LON, memo="가" * 100).status_code, 201)
        saved = self.behavior(walk_id, "OTHER", latitude=LAT, longitude=LON, memo="  쓰레기 줍기  ")
        self.assertEqual((saved.status_code, saved.json()["memo"]), (201, "쓰레기 줍기"))

    def test_blank_memo_is_dropped_for_other_types(self):
        walk_id = self.walking()
        response = self.behavior(walk_id, "POOP", latitude=LAT, longitude=LON, memo="   ")
        self.assertEqual((response.status_code, response.json()["memo"]), (201, None))

    def test_database_rejects_blank_other_and_unknown_location_source(self):
        walk_id = self.walking()
        for fields in ({"marker_type": "OTHER", "memo": "  "}, {"marker_type": "OTHER", "memo": None},
                       {"marker_type": "POOP", "location_source": "GUESS"}):
            with self.Session() as db, self.assertRaises(IntegrityError):
                db.add(WalkMarker(walk_id=walk_id, latitude=0, longitude=0, recorded_at=START, **fields))
                db.commit()

    def test_invalid_inputs_are_rejected(self):
        walk_id = self.walking()
        for extra in ({"marker_type": "UNKNOWN"}, {"walk_id": 999}, {"user_id": 2}, {"latitude": 91, "longitude": LON},
                      {"latitude": LAT}, {"longitude": LON}, {"latitude": LAT, "longitude": LON, "accuracy_m": -1},
                      {"latitude": LAT, "longitude": LON, "accuracy_m": True}, {"memo": 123}):
            fields = {"marker_type": "POOP", **extra}
            self.assertEqual(self.behavior(walk_id, **fields).status_code, 422, extra)
        self.assertEqual(self.behavior(walk_id, "POOP", latitude=LAT, longitude=LON, recorded_at=None).status_code, 422)
        self.assertEqual(self.rows(WalkMarker), [])

    # 3. GPS 신호가 약하면 마지막 위치에 저장
    def test_missing_coordinates_use_last_known_location(self):
        walk_id = self.walking(with_point=True)
        self.advance(20)
        response = self.behavior(walk_id, "POOP", seconds=20)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual((data["latitude"], data["longitude"], data["location_source"]), ("37.566500", "126.978000", "LAST_KNOWN"))
        self.assertTrue(data["recorded_at"].startswith((START + timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S")))

    def test_weak_accuracy_uses_last_known_and_boundary_keeps_current(self):
        walk_id = self.walking(with_point=True)
        self.advance(30)
        weak = self.behavior(walk_id, "SNIFF", seconds=30, latitude=37.6, longitude=127.0, accuracy_m=120.5)
        self.assertEqual((weak.json()["latitude"], weak.json()["longitude"], weak.json()["location_source"]),
                         ("37.566500", "126.978000", "LAST_KNOWN"))
        for accuracy in (50, 10, None):
            fields = {} if accuracy is None else {"accuracy_m": accuracy}
            good = self.behavior(walk_id, "SNIFF", seconds=30, latitude=37.6, longitude=127.0, **fields)
            self.assertEqual((good.status_code, good.json()["latitude"], good.json()["location_source"]),
                             (201, "37.600000", "CURRENT"), accuracy)

    def test_accuracy_threshold_comes_from_environment(self):
        walk_id = self.walking(with_point=True)
        self.advance(30)
        with patch.dict(os.environ, {"WALK_MARKER_MAX_ACCURACY_M": "20"}):
            response = self.behavior(walk_id, "POOP", seconds=30, latitude=37.6, longitude=127.0, accuracy_m=30)
        self.assertEqual(response.json()["location_source"], "LAST_KNOWN")
        with patch.dict(os.environ, {"WALK_MARKER_MAX_ACCURACY_M": "not-a-number"}):
            response = self.behavior(walk_id, "POOP", seconds=30, latitude=37.6, longitude=127.0, accuracy_m=30)
        self.assertEqual(response.json()["location_source"], "CURRENT")

    def test_no_location_to_fall_back_to_is_conflict(self):
        walk_id = self.walking()
        self.assertEqual(self.behavior(walk_id, "POOP").status_code, 409)
        self.assertEqual(self.behavior(walk_id, "POOP", latitude=LAT, longitude=LON, accuracy_m=500).status_code, 409)
        self.assertEqual(self.rows(WalkMarker), [])
        self.assertEqual(self.behavior(walk_id, "POOP", latitude=LAT, longitude=LON, accuracy_m=5).status_code, 201)

    # 4. 상한, 권한, 상태, 시각
    def test_marker_limit_per_walk(self):
        walk_id = self.walking()
        with patch.dict(os.environ, {"WALK_MARKER_MAX_PER_WALK": "2"}):
            for _ in range(2):
                self.assertEqual(self.behavior(walk_id, "POOP", latitude=LAT, longitude=LON).status_code, 201)
            self.assertEqual(self.behavior(walk_id, "POOP", latitude=LAT, longitude=LON).status_code, 409)
        self.assertEqual(len(self.rows(WalkMarker)), 2)

    def test_owner_state_and_time_rules_are_kept(self):
        walk_id = self.walking(with_point=True)
        for user in (None, 2, 3, 4, 5, 7):
            expected = 401 if user is None else 403
            self.assertEqual(self.behavior(walk_id, "POOP", user=user, latitude=LAT, longitude=LON).status_code, expected)
        self.assertEqual(self.behavior(walk_id, "POOP", seconds=-1, latitude=LAT, longitude=LON).status_code, 409)
        self.assertEqual(self.behavior(walk_id, "POOP", seconds=121, latitude=LAT, longitude=LON).status_code, 409)
        self.assertEqual(self.behavior(999, "POOP", latitude=LAT, longitude=LON).status_code, 404)
        self.advance(60)
        self.assertEqual(self.end(walk_id).status_code, 200)
        self.assertEqual(self.behavior(walk_id, "POOP", seconds=30, latitude=LAT, longitude=LON).status_code, 409)
        self.assertEqual(self.rows(WalkMarker), [])

    # 5. 산책 중 지도와 완료 화면에 표시
    def test_saved_markers_appear_in_walk_detail_and_completion_summary(self):
        walk_id = self.walking(with_point=True)
        self.advance(20)
        self.assertEqual(self.behavior(walk_id, "POOP", seconds=10, latitude=37.5666, longitude=126.9781).status_code, 201)
        self.assertEqual(self.behavior(walk_id, "OTHER", seconds=15, memo="물웅덩이").status_code, 201)

        detail = self.detail(walk_id)
        self.assertEqual(detail.status_code, 200)
        markers = detail.json()["markers"]
        self.assertEqual([(m["marker_type"], m["location_source"]) for m in markers],
                         [("POOP", "CURRENT"), ("OTHER", "LAST_KNOWN")])
        self.assertEqual((markers[0]["latitude"], markers[0]["longitude"]), ("37.566600", "126.978100"))
        self.assertEqual(markers[1]["memo"], "물웅덩이")

        self.advance(60)
        self.assertEqual(self.end(walk_id).status_code, 200)
        summary = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertEqual(summary.status_code, 200, summary.text)
        data = summary.json()
        self.assertEqual(data["summary"]["marker_count"], 2)
        self.assertEqual([(m["marker_type"], m["latitude"], m["longitude"], m["location_source"]) for m in data["markers"]],
                         [("POOP", "37.566600", "126.978100", "CURRENT"), ("OTHER", "37.566500", "126.978000", "LAST_KNOWN")])

    # 6. 보호소 실시간 화면
    def test_shelter_live_walk_shows_behavior_markers_in_time_order(self):
        walk_id = self.walking(with_point=True)
        self.advance(30)
        self.assertEqual(self.incident(walk_id, seconds=10, incident_type="INJURY", description="상처",
                                       latitude=37.5667, longitude=126.9782).status_code, 201)
        self.assertEqual(self.behavior(walk_id, "OTHER", seconds=15, memo="낙엽 먹음", latitude=37.5668, longitude=126.9783).status_code, 201)
        self.assertEqual(self.behavior(walk_id, "CROSSWALK_WAIT", seconds=20, latitude=37.5669, longitude=126.9784).status_code, 201)
        self.assertEqual(self.behavior(walk_id, "POOP", seconds=5).status_code, 201)  # 점 업로드와 같은 시각, 마지막 위치 사용

        detail = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3))
        self.assertEqual(detail.status_code, 200, detail.text)
        markers = detail.json()["markers"]
        self.assertEqual([m["marker_type"] for m in markers], ["START", "BEHAVIOR", "INCIDENT", "BEHAVIOR", "BEHAVIOR"])
        self.assertEqual([(m["behavior_type"], m["description"], m["incident_type"]) for m in markers[1:]],
                         [("POOP", None, None), (None, "상처", "INJURY"), ("OTHER", "낙엽 먹음", None), ("CROSSWALK_WAIT", None, None)])
        self.assertEqual((markers[1]["latitude"], markers[1]["longitude"]), ("37.566500", "126.978000"))
        self.assertEqual(markers[3]["latitude"], "37.566800")

        updates = self.client.get(f"/shelters/me/live-walks/{walk_id}/updates?after_seq=0", headers=self.auth(3))
        self.assertEqual(updates.status_code, 200, updates.text)
        self.assertEqual([m["marker_type"] for m in updates.json()["markers"]],
                         ["START", "BEHAVIOR", "INCIDENT", "BEHAVIOR", "BEHAVIOR"])

    def test_live_walk_without_behavior_markers_is_unchanged_and_other_shelter_is_blocked(self):
        walk_id = self.walking(with_point=True)
        markers = self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(3)).json()["markers"]
        self.assertEqual([m["marker_type"] for m in markers], ["START"])
        self.assertIsNone(markers[0]["behavior_type"])
        self.assertEqual(self.behavior(walk_id, "POOP", latitude=LAT, longitude=LON).status_code, 201)
        for user in (1, 4):
            self.assertEqual(self.client.get(f"/shelters/me/live-walks/{walk_id}", headers=self.auth(user)).status_code, 403)
            self.assertEqual(self.client.get(f"/shelters/me/live-walks/{walk_id}/updates", headers=self.auth(user)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
