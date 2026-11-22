"""Acceptance tests for F-09 Walk Completion Summary & Review (SC-08)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.models import Shelter, User, Walk, WalkPoint, WalkRequest, WalkReview
from app.routers import walk_maps, walk_reviews, walks
from walk_record_test_support import START, WalkFixture
from walk_request_test_support import END


class WalkReviewF09Tests(WalkFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_walk_api(engine)
        self.app.include_router(walk_reviews.router)
        self.app.include_router(walk_maps.router)

    # 1 & 2. summary에 경로, 행동마커, 시간, 거리, marker_count(행동마커만, 위험신고 미포함) 포함
    def test_summary_includes_metrics_and_marker_count_excludes_incidents(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 좌표 2개 업로드 (서버 현재시각 10초, 포인트 시각 5초, 10초)
        self.advance(10)
        p0 = self.point(sequence=0, seconds=5, latitude=37.5665, longitude=126.9780)
        p1 = self.point(sequence=1, seconds=10, latitude=37.5666, longitude=126.9781)
        up = self.upload(walk_id, [p0, p1], user=1)
        self.assertEqual(up.status_code, 200)

        # 행동마커 2건 등록
        self.advance(25)
        self.marker(walk_id, user=1, seconds=25, marker_type="SNIFF", memo="냄새 맡음")
        self.advance(30)
        self.marker(walk_id, user=1, seconds=30, marker_type="POOP", memo="배변")

        # 위험 신고 1건 등록
        self.advance(35)
        self.incident(walk_id, user=1, seconds=35, incident_type="INJURY", description="경미한 상처",
                      latitude=37.5666, longitude=126.9781)

        # 산책 종료
        self.advance(60)
        self.end(walk_id, user=1)

        # summary 조회
        r = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertEqual(r.status_code, 200)
        data = r.json()

        # 1) walk 정보
        self.assertEqual(data["walk"]["id"], walk_id)
        self.assertEqual(data["walk"]["status"], "COMPLETED")
        self.assertIsNotNone(data["walk"]["started_at"])
        self.assertIsNotNone(data["walk"]["ended_at"])

        # 2) summary 메트릭 및 marker_count
        self.assertEqual(data["summary"]["duration_seconds"], 60)
        self.assertIsNotNone(data["summary"]["total_distance_m"])
        # marker_count는 행동마커만 센다 (2건, 위험신고 1건 미포함)
        self.assertEqual(data["summary"]["marker_count"], 2)

        # 3) points 확인
        self.assertEqual(data["points_total"], 2)
        self.assertEqual(data["points_returned"], 2)
        self.assertFalse(data["is_downsampled"])
        self.assertEqual(len(data["points"]), 2)
        self.assertEqual(data["points"][0]["latitude"], "37.566500")

        # 4) markers 및 incidents
        self.assertEqual(len(data["markers"]), 2)
        self.assertEqual(len(data["incidents"]), 1)

    # 3. website_url이 NULL·빈 문자열·http(s) 아님이면 shelter_link가 null
    def test_shelter_link_formatting(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # 1) website_url이 None인 경우 -> null
        with self.Session.begin() as db:
            db.get(Shelter, 1).website_url = None
        r1 = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertIsNone(r1.json()["shelter_link"])

        # 2) website_url이 빈 문자열인 경우 -> null
        with self.Session.begin() as db:
            db.get(Shelter, 1).website_url = "   "
        r2 = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertIsNone(r2.json()["shelter_link"])

        # 3) website_url이 http(s)로 시작하지 않는 경우 -> null
        with self.Session.begin() as db:
            db.get(Shelter, 1).website_url = "ftp://shelter.org"
        r3 = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertIsNone(r3.json()["shelter_link"])

        # 4) 유효한 https URL인 경우 -> 링크 반환
        with self.Session.begin() as db:
            db.get(Shelter, 1).website_url = "https://shelter.example.com/dogs/1"
        r4 = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertEqual(r4.json()["shelter_link"], "https://shelter.example.com/dogs/1")

    # 4. 별점 없이 저장하면 422, 1~5 밖도 422
    def test_rating_validation_requires_1_to_5(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # 별점 누락 -> 422
        r_missing = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={
            "condition_tags": ["CALM"], "comment": "좋았어요"
        })
        self.assertEqual(r_missing.status_code, 422)

        # 별점 0 -> 422
        r_zero = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={"rating": 0})
        self.assertEqual(r_zero.status_code, 422)

        # 별점 6 -> 422
        r_six = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={"rating": 6})
        self.assertEqual(r_six.status_code, 422)

    # 5. 별점+태그+코멘트 저장 후 summary 및 이력에서 확인된다
    def test_save_review_reflected_in_summary_and_history(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # 평가 저장 (PUT -> 201)
        payload = {
            "rating": 5,
            "condition_tags": ["ENERGETIC", "EXCITED"],
            "comment": "오늘 활발하게 잘 뛰었어요!",
        }
        r_put = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json=payload)
        self.assertEqual(r_put.status_code, 201)
        rev = r_put.json()
        self.assertEqual(rev["rating"], 5)
        self.assertEqual(rev["condition_tags"], ["ENERGETIC", "EXCITED"])
        self.assertEqual(rev["comment"], "오늘 활발하게 잘 뛰었어요!")

        # summary 조회 시 review 포함 확인
        r_sum = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
        self.assertEqual(r_sum.status_code, 200)
        sum_rev = r_sum.json()["review"]
        self.assertIsNotNone(sum_rev)
        self.assertEqual(sum_rev["rating"], 5)
        self.assertEqual(sum_rev["condition_tags"], ["ENERGETIC", "EXCITED"])

        # GET /users/{user_id}/walks 이력 조회 시 rating과 has_review=True 확인
        r_hist = self.client.get("/users/1/walks", headers=self.auth(1))
        self.assertEqual(r_hist.status_code, 200)
        hist_walk = [w for w in r_hist.json()["walks"] if w["walk_id"] == walk_id][0]
        self.assertTrue(hist_walk["has_review"])
        self.assertEqual(hist_walk["rating"], 5)

    # 6. 코멘트 101자 422, 알 수 없는 태그 422, 태그 중복 422
    def test_review_payload_validation_errors(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # 1) 코멘트 101자 -> 422
        long_comment = "a" * 101
        r_long = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={
            "rating": 5, "comment": long_comment
        })
        self.assertEqual(r_long.status_code, 422)

        # 2) 알 수 없는 태그 -> 422
        r_unknown = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={
            "rating": 5, "condition_tags": ["UNKNOWN_TAG"]
        })
        self.assertEqual(r_unknown.status_code, 422)

        # 3) 태그 중복 -> 422
        r_dup = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={
            "rating": 5, "condition_tags": ["CALM", "CALM"]
        })
        self.assertEqual(r_dup.status_code, 422)

    # 7. 평가 없는 산책은 이력에 has_review=false, 나중에 작성 가능
    def test_unreviewed_walk_has_review_false_and_can_review_later(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # 평가 작성 전 이력 조회
        r_hist = self.client.get("/users/1/walks", headers=self.auth(1))
        hist_walk = [w for w in r_hist.json()["walks"] if w["walk_id"] == walk_id][0]
        self.assertFalse(hist_walk["has_review"])
        self.assertIsNone(hist_walk["rating"])

        # 나중에 작성
        self.advance(100)
        r_put = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={"rating": 4})
        self.assertEqual(r_put.status_code, 201)

        # 작성 후 이력 확인
        r_hist2 = self.client.get("/users/1/walks", headers=self.auth(1))
        hist_walk2 = [w for w in r_hist2.json()["walks"] if w["walk_id"] == walk_id][0]
        self.assertTrue(hist_walk2["has_review"])
        self.assertEqual(hist_walk2["rating"], 4)

    # 8. 저장한 평가 수정 가능 (태그 교체 포함, 200 반환)
    def test_update_review_and_replace_tags(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # 1) 최초 작성 (201)
        r1 = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={
            "rating": 3, "condition_tags": ["TENSE", "SCARED"], "comment": "처음엔 긴장했어요"
        })
        self.assertEqual(r1.status_code, 201)

        # 2) 수정 요청 (200, 태그 교체)
        r2 = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(1), json={
            "rating": 4, "condition_tags": ["CALM"], "comment": "나중엔 차분해졌어요"
        })
        self.assertEqual(r2.status_code, 200)
        rev = r2.json()
        self.assertEqual(rev["rating"], 4)
        self.assertEqual(rev["condition_tags"], ["CALM"])
        self.assertEqual(rev["comment"], "나중엔 차분해졌어요")

        # GET /walks/{id}/review 단건 조회로 확인
        r_get = self.client.get(f"/walks/{walk_id}/review", headers=self.auth(1))
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json()["condition_tags"], ["CALM"])

    # 9. COMPLETED·INTERRUPTED 작성 가능, IN_PROGRESS·CANCELLED 409
    def test_review_status_restrictions(self):
        # 1) IN_PROGRESS 상태에서 작성 시도 -> 409
        walk1 = self.begin(user=1, dog=1)
        r_prog = self.client.put(f"/walks/{walk1['id']}/review", headers=self.auth(1), json={"rating": 5})
        self.assertEqual(r_prog.status_code, 409)

        # 2) COMPLETED 상태 작성 -> 201 성공
        self.advance(10)
        self.end(walk1["id"], user=1)
        r_comp = self.client.put(f"/walks/{walk1['id']}/review", headers=self.auth(1), json={"rating": 5})
        self.assertEqual(r_comp.status_code, 201)

        # 3) INTERRUPTED 상태 작성 -> 201 성공
        walk2_req = self.seed_request(user=1, dog=2, status="APPROVED")
        walk2 = self.start(walk2_req, user=1).json()
        self.advance(20)
        self.end(walk2["id"], user=1, status="INTERRUPTED")
        r_intr = self.client.put(f"/walks/{walk2['id']}/review", headers=self.auth(1), json={"rating": 3})
        self.assertEqual(r_intr.status_code, 201)

    # 10. 타인 산책 작성·조회 403
    def test_other_user_forbidden(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # 사용자 2(타인)가 사용자 1의 산책에 대해 시도
        r_sum = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(2))
        self.assertEqual(r_sum.status_code, 403)

        r_put = self.client.put(f"/walks/{walk_id}/review", headers=self.auth(2), json={"rating": 5})
        self.assertEqual(r_put.status_code, 403)

        r_get = self.client.get(f"/walks/{walk_id}/review", headers=self.auth(2))
        self.assertEqual(r_get.status_code, 403)

    # 11. 마이그레이션 적용 후 기존 walk_reviews 행(별점 없음)이 제약에 걸리지 않는다
    def test_legacy_review_without_rating_satisfies_constraints(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        # DB에 별점 없이 기존 007 관찰 지표만 있는 레거시 행 삽입
        with self.Session.begin() as db:
            legacy = WalkReview(walk_id=walk_id, leash_pulling=4, rating=None, comment=None)
            db.add(legacy)

        # 제약 위반 없이 조회 가능
        r = self.client.get(f"/walks/{walk_id}/review", headers=self.auth(1))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["rating"])

    # 12. 점이 상한을 넘으면 첫·마지막 점 보존, points_returned <= 상한, is_downsampled=true
    def test_points_downsampled_when_exceeding_max(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        # 10개 포인트 업로드 (sequence 0~9, 정상 보행 속도 1.4 m/s)
        points = []
        for seq in range(10):
            points.append(self.point(sequence=seq, seconds=seq * 5,
                                     latitude=37.5000 + seq * 0.00005,
                                     longitude=127.0000 + seq * 0.00005))
        self.advance(50)
        up = self.upload(walk_id, points, user=1)
        self.assertEqual(up.status_code, 200)
        self.advance(60)
        self.end(walk_id, user=1)

        # 상한을 4개로 모킹하여 다운샘플링 검증
        with patch("app.walk_review_service.get_walk_summary_max_points", return_value=4):
            r = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
            self.assertEqual(r.status_code, 200)
            data = r.json()

            self.assertEqual(data["points_total"], 10)
            self.assertTrue(data["is_downsampled"])
            self.assertLessEqual(data["points_returned"], 5)
            # 첫 점과 마지막 점 보존 확인
            self.assertEqual(data["points"][0]["latitude"], "37.500000")
            self.assertEqual(data["points"][-1]["latitude"], "37.500450")

    # 13. 상한 이하이면 전체 점이 반환되고 is_downsampled=false
    def test_points_not_downsampled_when_within_max(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]

        points = []
        for seq in range(3):
            points.append(self.point(sequence=seq, seconds=seq * 5,
                                     latitude=37.5000 + seq * 0.00005,
                                     longitude=127.0000 + seq * 0.00005))
        self.advance(20)
        up = self.upload(walk_id, points, user=1)
        self.assertEqual(up.status_code, 200)
        self.advance(30)
        self.end(walk_id, user=1)

        with patch("app.walk_review_service.get_walk_summary_max_points", return_value=2000):
            r = self.client.get(f"/walks/{walk_id}/summary", headers=self.auth(1))
            self.assertEqual(r.status_code, 200)
            data = r.json()

            self.assertEqual(data["points_total"], 3)
            self.assertEqual(data["points_returned"], 3)
            self.assertFalse(data["is_downsampled"])
            self.assertEqual(len(data["points"]), 3)

    # 14. 평가가 없을 때 GET /walks/{id}/review는 200 + null
    def test_get_review_returns_null_when_not_reviewed(self):
        walk = self.begin(user=1, dog=1)
        walk_id = walk["id"]
        self.advance(10)
        self.end(walk_id, user=1)

        r = self.client.get(f"/walks/{walk_id}/review", headers=self.auth(1))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json())

    # 15. 없는 산책 404
    def test_nonexistent_walk_returns_404(self):
        r_sum = self.client.get("/walks/99999/summary", headers=self.auth(1))
        self.assertEqual(r_sum.status_code, 404)

        r_put = self.client.put("/walks/99999/review", headers=self.auth(1), json={"rating": 5})
        self.assertEqual(r_put.status_code, 404)

        r_get = self.client.get("/walks/99999/review", headers=self.auth(1))
        self.assertEqual(r_get.status_code, 404)
