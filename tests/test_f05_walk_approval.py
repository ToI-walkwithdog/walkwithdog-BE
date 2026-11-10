"""Acceptance and regression tests for F-05 walk approval (shelter SC-14)."""
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, time, timedelta, timezone
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from walk_request_test_support import END, NOW, RequestFixture, START
from app.models import DogWalkSetting, Survey, User, UserRole


class WalkApprovalF05Tests(RequestFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_api(engine)

        # Seed survey data for user 1 and user 2
        with self.Session.begin() as db:
            db.add(
                Survey(
                    user_id=1,
                    dog_experience="previous_owner",
                    walk_frequency="3_to_4_per_week",
                    housing_type="apartment",
                    family_type="single",
                )
            )
            # Intermediate dog setting for dog 3
            setting3 = db.query(DogWalkSetting).filter_by(dog_id=3).first()
            if setting3:
                setting3.difficulty = "INTERMEDIATE"

    # 1. 승인 대기 목록 (SC-14): PENDING 상태 신청만 확인
    def test_shelter_pending_requests_list(self):
        first = self.create(user=1, dog=1)
        second = self.create(user=2, dog=2)

        # Approve the first request
        self.assertEqual(self.act(first["id"], "APPROVE").status_code, 200)

        # Query pending requests for shelter 1
        resp = self.client.get("/shelters/1/walk-requests?status=PENDING", headers=self.auth(3))
        self.assertEqual(resp.status_code, 200)
        items = resp.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], second["id"])
        self.assertEqual(items[0]["status"], "PENDING")

    # 2. 신청 상세 조회: 신청자 정보(닉네임, 반려견 경험, 산책 빈도)와 강아지 난이도 확인
    def test_walk_request_detail_with_applicant_and_dog_difficulty(self):
        created = self.create(user=1, dog=1)

        # Shelter staff viewing request detail
        resp = self.client.get(f"/walk-requests/{created['id']}", headers=self.auth(3))
        self.assertEqual(resp.status_code, 200)
        detail = resp.json()

        self.assertEqual(detail["id"], created["id"])
        self.assertEqual(detail["status"], "PENDING")
        self.assertEqual(detail["dog_difficulty"], "BEGINNER")
        self.assertFalse(detail["cancelled_after_approval"])

        # Applicant details check
        applicant = detail["applicant"]
        self.assertEqual(applicant["id"], 1)
        self.assertEqual(applicant["nickname"], "사용자1")
        self.assertEqual(applicant["dog_experience"], "previous_owner")
        self.assertEqual(applicant["walk_frequency"], "3_to_4_per_week")
        self.assertIsNone(applicant["name"])
        self.assertIsNone(applicant["age"])
        self.assertIsNone(applicant["experienced_breed"])

        # Applicant本人 조회 가능
        resp_self = self.client.get(f"/walk-requests/{created['id']}", headers=self.auth(1))
        self.assertEqual(resp_self.status_code, 200)

        # Other volunteer cannot view (403)
        resp_other = self.client.get(f"/walk-requests/{created['id']}", headers=self.auth(2))
        self.assertEqual(resp_other.status_code, 403)

        # Other shelter cannot view (403)
        resp_other_shelter = self.client.get(f"/walk-requests/{created['id']}", headers=self.auth(4))
        self.assertEqual(resp_other_shelter.status_code, 403)

    # 3. 승인 결과가 사용자 마이페이지 예약 내역에 반영
    def test_approve_request_and_user_mypage_reflection(self):
        created = self.create(user=1, dog=1)

        # Shelter staff approves
        approve_resp = self.act(created["id"], "APPROVE", user=3)
        self.assertEqual(approve_resp.status_code, 200)
        approved_data = approve_resp.json()
        self.assertEqual(approved_data["status"], "APPROVED")
        self.assertEqual(approved_data["reviewed_by"], 3)
        self.assertIsNone(approved_data["rejection_reason"])
        self.assertIsNone(approved_data["rejection_reason_code"])

        # User checks mypage walk requests (GET /walk-requests/my)
        my_resp = self.client.get("/walk-requests/my", headers=self.auth(1))
        self.assertEqual(my_resp.status_code, 200)
        my_list = my_resp.json()
        matching = [item for item in my_list if item["id"] == created["id"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["status"], "APPROVED")
        self.assertIsNone(matching[0]["rejection_reason"])

    # 4. 거절 사유 3종 코드 + 기본 텍스트 자동 채움 및 사용자 마이페이지 반영
    def test_reject_request_with_predefined_codes_and_mypage(self):
        codes = [
            ("SCHEDULE_CONFLICT", "보호소 일정상 어려움"),
            ("EXPERIENCE_MISMATCH", "경험 부족"),
            ("INFO_INSUFFICIENT", "정보 확인 어려움"),
        ]
        for idx, (code, default_text) in enumerate(codes, start=1):
            created = self.seed_request(user=1, dog=1, start=START + timedelta(days=idx * 7),
                                        end=END + timedelta(days=idx * 7))
            reject_resp = self.act(created, "REJECT", user=3, rejection_reason_code=code)
            self.assertEqual(reject_resp.status_code, 200)
            data = reject_resp.json()
            self.assertEqual(data["status"], "REJECTED")
            self.assertEqual(data["rejection_reason_code"], code)
            self.assertEqual(data["rejection_reason"], default_text)

        # User checks my requests: rejection reasons and codes are properly displayed
        my_resp = self.client.get("/walk-requests/my?status=REJECTED", headers=self.auth(1))
        self.assertEqual(my_resp.status_code, 200)
        rejected_items = my_resp.json()
        self.assertEqual(len(rejected_items), 3)

    # 4-2. 거절 사유 기타(OTHER) 선택 시 직접 입력 필수 검증
    def test_reject_with_other_requires_detail(self):
        created = self.create(user=1, dog=1)

        # OTHER without rejection_reason should fail validation (422)
        resp_missing = self.act(created["id"], "REJECT", user=3, rejection_reason_code="OTHER")
        self.assertEqual(resp_missing.status_code, 422)

        # OTHER with empty whitespace should fail (422)
        resp_blank = self.act(created["id"], "REJECT", user=3, rejection_reason_code="OTHER", rejection_reason="   ")
        self.assertEqual(resp_blank.status_code, 422)

        # OTHER with valid detail succeeds
        resp_ok = self.act(created["id"], "REJECT", user=3, rejection_reason_code="OTHER", rejection_reason="당일 비 예보로 취소")
        self.assertEqual(resp_ok.status_code, 200)
        self.assertEqual(resp_ok.json()["rejection_reason_code"], "OTHER")
        self.assertEqual(resp_ok.json()["rejection_reason"], "당일 비 예보로 취소")

    # 4-3. 하위 호환: rejection_reason_code 없이 사유 텍스트만 보낼 경우 OTHER로 처리
    def test_reject_legacy_reason_only_defaults_to_other(self):
        created = self.create(user=1, dog=1)
        resp = self.act(created["id"], "REJECT", user=3, rejection_reason="기존 레거시 거절 문구")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["rejection_reason_code"], "OTHER")
        self.assertEqual(resp.json()["rejection_reason"], "기존 레거시 거절 문구")

    # 5. 이미 처리된 신청은 다시 처리할 수 없음 (409)
    def test_already_processed_request_cannot_be_reprocessed(self):
        req1 = self.create(user=1, dog=1)
        self.assertEqual(self.act(req1["id"], "APPROVE", user=3).status_code, 200)
        # Attempt to approve or reject again
        self.assertEqual(self.act(req1["id"], "APPROVE", user=3).status_code, 409)
        self.assertEqual(self.act(req1["id"], "REJECT", user=3, rejection_reason_code="SCHEDULE_CONFLICT").status_code, 409)

        req2 = self.create(user=2, dog=2)
        self.assertEqual(self.act(req2["id"], "REJECT", user=3, rejection_reason_code="SCHEDULE_CONFLICT").status_code, 200)
        self.assertEqual(self.act(req2["id"], "APPROVE", user=3).status_code, 409)
        self.assertEqual(self.act(req2["id"], "REJECT", user=3, rejection_reason_code="EXPERIENCE_MISMATCH").status_code, 409)

    # 6. 거절된 슬롯은 다시 신청 가능 상태로 복귀
    def test_rejected_slot_can_be_booked_again(self):
        req = self.create(user=1, dog=1)
        self.assertEqual(self.act(req["id"], "REJECT", user=3, rejection_reason_code="SCHEDULE_CONFLICT").status_code, 200)

        # Same slot, different user (or same user) can now apply
        resubmitted = self.create(user=2, dog=1)
        self.assertEqual(resubmitted["status"], "PENDING")

    # 7. 승인 후 사용자가 취소하면 슬롯 복귀 및 보호소 목록에 "승인 후 취소" 표시
    def test_cancel_after_approval_reopens_slot_and_shows_cancelled_after_approval(self):
        created = self.create(user=1, dog=1)
        # 1. 보호소 승인
        self.assertEqual(self.act(created["id"], "APPROVE", user=3).status_code, 200)

        # 2. 사용자 본인이 취소
        cancel_resp = self.act(created["id"], "CANCEL", user=1)
        self.assertEqual(cancel_resp.status_code, 200)
        cancelled_data = cancel_resp.json()
        self.assertEqual(cancelled_data["status"], "CANCELLED")
        self.assertTrue(cancelled_data["cancelled_after_approval"])

        # 3. 보호소 신청 내역 목록에서 확인
        shelter_list_resp = self.client.get("/shelters/1/walk-requests?status=CANCELLED", headers=self.auth(3))
        self.assertEqual(shelter_list_resp.status_code, 200)
        items = shelter_list_resp.json()
        matching = [item for item in items if item["id"] == created["id"]]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0]["cancelled_after_approval"])

        # 4. 신청 상세에서도 cancelled_after_approval == True
        detail_resp = self.client.get(f"/walk-requests/{created['id']}", headers=self.auth(3))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertTrue(detail_resp.json()["cancelled_after_approval"])

        # 5. 슬롯이 복귀되어 다른 사용자가 동일 슬롯에 신청 가능
        new_app = self.create(user=2, dog=1)
        self.assertEqual(new_app["status"], "PENDING")

    # 7-2. 승인 전 대기 상태(PENDING)에서 취소한 경우 cancelled_after_approval == False
    def test_cancel_before_approval_shows_false(self):
        created = self.create(user=1, dog=1)
        cancel_resp = self.act(created["id"], "CANCEL", user=1)
        self.assertEqual(cancel_resp.status_code, 200)
        self.assertFalse(cancel_resp.json()["cancelled_after_approval"])

    # 8. 강아지 난이도 3단계 (INTERMEDIATE) 지원 확인
    def test_dog_walk_settings_intermediate_difficulty(self):
        # Dog 3 was seeded with INTERMEDIATE in setUp
        created = self.create(user=1, dog=3)
        detail_resp = self.client.get(f"/walk-requests/{created['id']}", headers=self.auth(4))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.json()["dog_difficulty"], "INTERMEDIATE")

        # Update difficulty to INTERMEDIATE via dog walk setting PATCH
        patch_resp = self.client.patch("/dogs/1/walk-settings", headers=self.auth(3), json={"difficulty": "INTERMEDIATE"})
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual(patch_resp.json()["difficulty"], "INTERMEDIATE")
