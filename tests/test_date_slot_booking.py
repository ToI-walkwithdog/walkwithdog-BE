"""HTTP contract and compatibility tests for Issue #29."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from walk_record_test_support import WalkFixture
from walk_request_test_support import END, NOW, START
from app.models import Dog, DogWalkDateSlot, Shelter, WalkRequest
from app.region_models import LEVEL_SIDO, LEVEL_SIGUNGU, Region

LIST_URL = "/dogs/walk-availability"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-image"


def slot(start="09:00", end="10:00", day="2030-01-07", duration=30):
    return {"slot_date": day, "start_time": start, "end_time": end, "duration_minutes": duration}


class DateBookingFixture(WalkFixture):
    def setup_date_api(self, engine):
        self.setup_walk_api(engine)
        for target in ("app.routers.dogs.datetime", "app.services.dog_service.datetime"):
            clock = self.enterContext(patch(target, wraps=datetime))
            clock.now.return_value = NOW
        self.request_clock.return_value = NOW
        self.upload_dir = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="toi-date-image-")))
        self.enterContext(patch.dict(os.environ, {"UPLOAD_DIR": str(self.upload_dir)}))
        with self.Session.begin() as db:
            db.add_all([Region(region_code="11", region_name="테스트 서울", region_level=LEVEL_SIDO, is_active=True),
                        Region(region_code="26", region_name="테스트 부산", region_level=LEVEL_SIDO, is_active=True)])
            db.flush()
            db.add_all([Region(region_code="11680", region_name="테스트 구역 1", region_level=LEVEL_SIGUNGU,
                               parent_region_code="11", is_active=True),
                        Region(region_code="26350", region_name="테스트 구역 2", region_level=LEVEL_SIGUNGU,
                               parent_region_code="26", is_active=True)])
            db.flush()
            db.get(Shelter, 1).region_code = "11680"
            db.get(Shelter, 2).region_code = "26350"

    def register_slots(self, *slots, dog=1, actor=3):
        return self.client.post(f"/dogs/{dog}/date-slots", json=list(slots), headers=self.auth(actor))

    def create_slot(self, payload=None, *, dog=1, actor=3):
        response = self.register_slots(payload or slot(), dog=dog, actor=actor)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()[0]["id"]

    def submit(self, slot_id, key, *, actor=1, dog=1, start=START, end=END):
        return self.client.post("/walk-requests", json={"dog_id": dog, "date_slot_id": slot_id,
                                "requested_start_at": start.isoformat(), "requested_end_at": end.isoformat()},
                                headers={**self.auth(actor), **({"Idempotency-Key": str(key)} if key else {})})

    def codes(self, response, code, status=409):
        self.assertEqual((response.status_code, response.json().get("code")), (status, code), response.text)


class DateSlotBookingTests(DateBookingFixture, unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self.setup_date_api(engine)

    def test_new_registration_saves_image_dog_settings_and_slots_then_serves_the_image(self):
        response = self.client.post("/dogs/walk-registration", headers=self.auth(3),
            data={"name": "합성 반려견", "age_months": 4, "size_code": "SMALL", "difficulty_score": "3.5",
                  "max_walk_minutes": 30, "slots": json.dumps([slot()])}, files={"image": ("fixture.png", PNG)})
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual((data["dog"]["shelter_id"], data["dog"]["age_months"]), (1, 4))
        self.assertEqual(data["walk_settings"]["difficulty_score"], 3.5)
        image = self.client.get(data["dog"]["image_url"])
        self.assertEqual((image.status_code, image.content), (200, PNG))
        found = self.client.get(LIST_URL, params={"region_codes": "11680", "size_code": "SMALL"}, headers=self.auth())
        self.assertEqual(found.json()["data"]["items"][0]["date_slots"][0]["date_slot_id"], data["date_slots"][0]["id"])
        previous_json = self.client.post("/dogs/", headers=self.auth(3), json={"name": "기존 등록", "shelter_id": 1})
        self.assertEqual(previous_json.status_code, 200, previous_json.text)

    def test_file_and_database_are_rolled_back_when_a_slot_is_invalid(self):
        before = self.rows(Dog), self.rows(DogWalkDateSlot)
        def register(image):
            return self.client.post("/dogs/walk-registration", headers=self.auth(3),
                data={"name": "등록 실패", "slots": json.dumps([slot(), slot("09:30", "10:30")])},
                files={"image": ("fixture.png", image)})
        self.assertEqual(register(b"invalid photo").status_code, 400)
        self.assertEqual(register(PNG).status_code, 409)
        self.assertEqual((self.rows(Dog), self.rows(DogWalkDateSlot)), before)
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_management_enforces_owner_future_bounds_duration_and_overlap(self):
        self.assertEqual(self.register_slots(slot(), actor=4).status_code, 403)
        self.assertEqual(self.register_slots(slot(), actor=7).status_code, 403)
        self.assertEqual(self.register_slots(slot(), actor=1).status_code, 403)
        self.assertEqual(self.register_slots(slot(day="2030-01-05")).status_code, 422)
        self.assertEqual(self.register_slots(slot(day="2031-01-07")).status_code, 422)
        self.assertEqual(self.register_slots(slot(duration=90)).status_code, 422)
        first = self.create_slot()
        self.create_slot(slot("10:00", "11:00"))
        self.assertEqual(self.register_slots(slot("09:30", "10:30")).status_code, 409)
        patch = self.client.patch(f"/dogs/1/date-slots/{first}", headers=self.auth(3), json={"duration_minutes": 60})
        self.assertEqual((patch.status_code, patch.json()["duration_minutes"]), (200, 60))
        rows = self.client.get("/dogs/1/date-slots", headers=self.auth(5)).json()
        self.assertEqual([entry["start_time"] for entry in rows], ["09:00:00", "10:00:00"])

    def test_availability_filters_before_paging_and_keeps_visit_information_private(self):
        for start, end in (("09:00", "10:00"), ("10:00", "11:00"), ("11:00", "12:00")):
            self.create_slot(slot(start, end))
        self.create_slot(dog=3, actor=4)
        self.create_slot(dog=8)
        update = self.client.patch("/dogs/3/walk-settings", headers=self.auth(4),
                                   json={"size_code": "MEDIUM", "difficulty_score": 4.5, "max_walk_minutes": 20})
        self.assertEqual(update.status_code, 200, update.text)
        def search(**args):
            return self.client.get(LIST_URL, params={"region_codes": ["11680", "26350"], **args}, headers=self.auth()).json()["data"]
        result = search(limit=1)
        self.assertEqual((result["items"][0]["dog_id"], result["next_offset"]), (1, 1))
        card = result["items"][0]
        self.assertTrue(card["has_more_date_slots"])
        self.assertEqual(len(card["date_slots"]), 2)
        self.assertIsNone(card["max_walk_minutes"])
        self.assertEqual([item["dog_id"] for item in search(size_code="MEDIUM", limit=1)["items"]], [3])
        detail = self.client.get("/dogs/1/walk-availability", headers=self.auth()).json()["data"]
        self.assertEqual(len(detail["date_slots"]), 3)
        self.assertEqual(set(detail["shelter"]), {"id", "name", "region_code", "region_name"})

    def test_availability_reuses_onboarding_and_validates_parameters(self):
        self.codes(self.client.get(LIST_URL), "AUTHENTICATION_REQUIRED", 401)
        self.codes(self.client.get(LIST_URL, headers=self.auth(3)), "WALK_LIST_NOT_ALLOWED", 403)
        self.codes(self.client.get(LIST_URL, headers=self.auth(1)), "REGIONS_REQUIRED", 422)
        for search, code in (({"region_codes": ["11680"] * 4}, "REGION_LIMIT_EXCEEDED"),
                             ({"region_codes": ["11680", "11680"]}, "INVALID_REGION_CODE"),
                             ({"region_codes": "11680", "size_code": "ALL"}, "INVALID_DOG_SIZE"),
                             ({"region_codes": "11680", "limit": 0}, "INVALID_PAGINATION")):
            self.codes(self.client.get(LIST_URL, params=search, headers=self.auth()), code, 422)
        self.codes(self.client.get("/dogs/invalid/walk-availability", headers=self.auth()), "INVALID_DOG_ID", 422)
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "new-pledge"}):
            self.codes(self.client.get(LIST_URL, headers=self.auth()), "ONBOARDING_NOT_COMPLETED", 403)

    def test_region_management_reuses_selectable_region_and_shelter_permissions(self):
        path = "/shelters/1/region"
        self.assertEqual(self.client.patch(path, json={"region_code": "26350"}, headers=self.auth(4)).status_code, 403)
        self.assertEqual(self.client.patch(path, json={"region_code": "26"}, headers=self.auth(3)).status_code, 422)
        response = self.client.patch(path, json={"region_code": "26350"}, headers=self.auth(3))
        self.assertEqual(response.json(), {"shelter_id": 1, "region_code": "26350", "region_name": "테스트 구역 2"})

    def test_date_slot_booking_approves_starts_and_ends_without_a_weekly_slot(self):
        path = "/dogs/1/walk-settings"
        self.assertEqual(self.client.patch(path, json={"slots": []}, headers=self.auth(3)).status_code, 200)
        date_id = self.create_slot()
        reply = self.submit(date_id, uuid4())
        self.codes(reply, "WALK_REQUEST_CREATED", 201)
        result = reply.json()["data"]
        self.assertEqual((result["date_slot_id"], result["status"], result["requested_start_at"]),
                         (date_id, "PENDING", "2030-01-07T00:00:00Z"))
        self.assertEqual(self.start(result["id"]).status_code, 409)
        self.assertEqual(self.act(result["id"], "APPROVE").json()["date_slot_id"], date_id)
        self.advance(-1)
        self.assertEqual(self.start(result["id"]).status_code, 409)
        self.advance(0)
        self.assertEqual(self.start(result["id"], user=2).status_code, 403)
        started = self.start(result["id"])
        self.assertEqual(started.status_code, 201, started.text)
        self.advance(60)
        self.assertEqual(self.end(started.json()["id"]).status_code, 200)

    def test_slot_occupancy_blocks_both_paths_and_releases_after_rejection_or_cancellation(self):
        date_id = self.create_slot()
        first = self.submit(date_id, uuid4()).json()["data"]["id"]
        self.codes(self.submit(date_id, uuid4(), actor=2), "WALK_SLOT_UNAVAILABLE")
        self.assertEqual(self.post(user=2).status_code, 409)
        self.assertEqual(self.client.get("/dogs/1/walk-availability", headers=self.auth()).json()["data"]["date_slots"], [])
        self.assertEqual(self.act(first, "REJECT", rejection_reason_code="SCHEDULE_CONFLICT").status_code, 200)
        second = self.submit(date_id, uuid4(), actor=2).json()["data"]["id"]
        self.assertEqual(self.act(second, "APPROVE").status_code, 200)
        self.codes(self.submit(date_id, uuid4()), "WALK_SLOT_UNAVAILABLE")
        self.assertEqual(self.act(second, "CANCEL", user=2).status_code, 200)
        self.codes(self.submit(date_id, uuid4()), "WALK_REQUEST_CREATED", 201)

    def test_existing_weekly_booking_blocks_date_overlap_but_allows_touching_boundaries(self):
        old = self.post()
        self.assertEqual(old.status_code, 201, old.text)
        self.assertNotIn("date_slot_id", old.json())
        self.assertEqual(self.post(user=2).status_code, 201)
        earlier = self.create_slot()
        later = self.create_slot(slot("10:00", "11:00"))
        detail = self.client.get("/dogs/1/walk-availability", headers=self.auth()).json()["data"]
        self.assertEqual([row["date_slot_id"] for row in detail["date_slots"]], [later])
        self.codes(self.submit(earlier, uuid4(), actor=2), "WALK_SLOT_UNAVAILABLE")
        self.codes(self.submit(later, uuid4(), actor=2, start=END, end=END + timedelta(hours=1)), "WALK_REQUEST_CREATED", 201)

    def test_retry_preserves_first_response_after_status_time_and_pledge_change(self):
        date_id = self.create_slot()
        key = uuid4()
        first = self.submit(date_id, key)
        self.codes(first, "WALK_REQUEST_CREATED", 201)
        request_id = first.json()["data"]["id"]
        self.assertEqual(self.act(request_id, "APPROVE").status_code, 200)
        self.advance(60)
        with patch.dict(os.environ, {"PLEDGE_CURRENT_VERSION": "new-pledge"}):
            equivalent = START.astimezone(datetime.fromisoformat("2030-01-01T00:00:00+09:00").tzinfo)
            replay = self.submit(date_id, key, start=equivalent)
            self.assertEqual((replay.status_code, replay.json()), (201, first.json()))
            self.codes(self.submit(date_id, key, end=END + timedelta(minutes=1)), "IDEMPOTENCY_KEY_REUSED")
            self.codes(self.submit(date_id, uuid4()), "ONBOARDING_NOT_COMPLETED", 403)
        self.assertEqual(len(self.rows(WalkRequest)), 1)

    def test_changed_wrong_and_expired_slots_are_rejected_without_creating_requests(self):
        date_id = self.create_slot()
        self.codes(self.submit(date_id, None), "INVALID_WALK_REQUEST", 422)
        self.codes(self.submit(date_id, "invalid"), "INVALID_WALK_REQUEST", 422)
        self.codes(self.submit(date_id, uuid4(), dog=8), "INVALID_WALK_REQUEST", 422)
        self.codes(self.submit(date_id, uuid4(), end=END + timedelta(minutes=1)), "WALK_SLOT_CHANGED")
        self.advance(0)
        self.codes(self.submit(date_id, uuid4()), "WALK_SLOT_UNAVAILABLE")
        self.assertEqual(self.rows(WalkRequest), [])

    def test_retiring_and_replacing_slots_preserves_original_reservation_and_history(self):
        date_id = self.create_slot()
        request_id = self.submit(date_id, uuid4()).json()["data"]["id"]
        url = f"/dogs/1/date-slots/{date_id}"
        self.assertEqual(self.client.patch(url, headers=self.auth(3), json={"end_time": "10:30"}).status_code, 409)
        self.assertEqual(self.client.delete(url, headers=self.auth(3)).status_code, 409)
        self.assertEqual(self.act(request_id, "REJECT", rejection_reason_code="INFO_INSUFFICIENT").status_code, 200)
        new_slot = self.client.patch(url, headers=self.auth(3), json={"end_time": "10:30"}).json()
        self.assertNotEqual(new_slot["id"], date_id)
        detail = self.client.get(f"/walk-requests/{request_id}", headers=self.auth()).json()
        self.assertEqual((detail["date_slot_id"], detail["status"], detail["requested_end_at"], detail["reviewed_by"]),
                         (date_id, "REJECTED", "2030-01-07T01:00:00Z", 3))
        self.assertEqual(self.client.delete(url, headers=self.auth(3)).status_code, 204)
        with self.Session() as db:
            self.assertFalse(db.get(DogWalkDateSlot, date_id).is_active)
