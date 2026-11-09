"""Real bearer authorization and API transactions against isolated SQLite fixtures."""
from datetime import datetime, time, timedelta, timezone
import os
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.auth import token_digest
    from app.database import Base, get_db
    from app.models import (
        AuthSession, Dog, DogWalkSetting, DogWalkSlot, MatchingResult, Shelter,
        Survey, User, UserRole, Walk, WalkRequest,
    )
    from app.routers import dogs, matching, public_api, shelters
    from app.schemas import DogCardResponse, DogDetailResponse
    from app.walk_schemas import WalkSettingsResponse

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def slot(day=0, start="09:00", end="11:00"):
    return {"day_of_week": day, "start_time": start, "end_time": end}


def public_item(identifier="448567202600718", **changes):
    return {"desertionNo": identifier, "kindNm": "말티즈", "careNm": "첫 보호소",
            "careRegNo": "123", "careAddr": "가상 주소", "sexCd": "M",
            "processState": "보호중", "specialMark": "관찰", **changes}


class WalkSettingsAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.app = FastAPI()
        for router in (dogs.router, shelters.router, public_api.router, matching.router):
            self.app.include_router(router)

        def test_db():
            with self.Session() as db:
                yield db

        for dependency in (get_db, dogs.get_db, shelters.get_db):
            self.app.dependency_overrides[dependency] = test_db
        self.client = TestClient(self.app)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.client.close)
        with self.Session.begin() as db:
            db.add_all([Shelter(id=1, name="첫 보호소", external_code="123", address="가상 주소"),
                        Shelter(id=2, name="다른 보호소"), Shelter(id=3, name="빈 보호소")])
            db.flush()
            db.add_all([
                User(id=1, email="user@example.com", nickname="일반"),
                User(id=2, email="first@example.com", nickname="담당1", role=UserRole.SHELTER, shelter_id=1),
                User(id=3, email="second@example.com", nickname="담당2", role=UserRole.SHELTER, shelter_id=2),
                User(id=4, email="unset@example.com", nickname="소속 없음", role=UserRole.SHELTER),
                User(id=5, email="admin@example.com", nickname="관리자", role=UserRole.ADMIN),
                Dog(id=1, name="미설정", shelter_id=1, desertion_no="448567202600718", energy_level=2, description="기존 설명"),
                Dog(id=2, name="타 보호소", shelter_id=2, energy_level=4),
                Dog(id=3, name="미배정"), Dog(id=4, name="산책 가능", shelter_id=1),
                Dog(id=5, name="산책 불가", shelter_id=1),
            ])
            db.flush()
            for user_id in range(1, 6):
                db.add(AuthSession(token_hash=token_digest(f"walk-session-{user_id}"), user_id=user_id,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            enabled = DogWalkSetting(dog_id=4, walkable=True, precautions="기존 주의사항", recommended_duration_minutes=30)
            db.add_all([enabled, DogWalkSetting(dog_id=5, walkable=False)])
            db.flush()
            db.add(DogWalkSlot(walk_setting_id=enabled.id, day_of_week=0, start_time=time(9), end_time=time(11)))

    def authenticate(self, user_id=2):
        self.client.headers["Authorization"] = f"Bearer walk-session-{user_id}"

    def update(self, dog_id=1, **payload):
        response = self.client.patch(f"/dogs/{dog_id}/walk-settings", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def rows(self, model):
        with self.Session() as db:
            return [tuple(getattr(row, c.name) for c in model.__table__.columns)
                    for row in db.query(model).order_by(*model.__table__.primary_key.columns)]

    def settings_snapshot(self):
        return self.rows(DogWalkSetting), self.rows(DogWalkSlot)

    def ids(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return [dog["id"] for dog in response.json()]

    def test_first_upsert_full_payload_preserves_dog_columns(self):
        self.authenticate()
        before = self.rows(Dog)
        result = self.update(walkable=True, difficulty="EXPERIENCED", precautions="거리 유지",
                             recommended_duration_minutes=45, slots=[slot(2, "14:00", "17:00"), slot()])
        self.assertEqual(result["dog_id"], 1)
        self.assertTrue(result["walkable"])
        self.assertEqual(result["difficulty"], "EXPERIENCED")
        self.assertEqual(result["precautions"], "거리 유지")
        self.assertEqual(result["recommended_duration_minutes"], 45)
        self.assertEqual([s["day_of_week"] for s in result["slots"]], [0, 2])
        self.assertEqual(result["slots"][0]["start_time"], "09:00:00")
        self.assertEqual(WalkSettingsResponse.model_validate(result).model_dump(mode="json"), result)
        self.assertEqual(self.rows(Dog), before)

    def test_minimal_first_upsert_uses_existing_orm_defaults(self):
        self.authenticate()
        result = self.update(precautions="주의")
        self.assertEqual((result["walkable"], result["difficulty"], result["recommended_duration_minutes"], result["slots"]),
                         (False, "BEGINNER", None, []))
        with self.Session() as db:
            self.assertEqual(db.query(DogWalkSetting).filter_by(dog_id=1).count(), 1)

    def test_partial_patch_omission_keeps_values_and_slot_ids(self):
        self.authenticate()
        before = self.client.get("/dogs/4").json()["walk_settings"]
        result = self.update(4, difficulty="EXPERIENCED")
        self.assertEqual(result, {**before, "difficulty": "EXPERIENCED"})
        with self.Session() as db:
            created = db.get(DogWalkSetting, result["id"]).created_at
        result = self.update(4, walkable=False)
        self.assertFalse(result["walkable"])
        self.assertEqual(result["slots"], before["slots"])
        self.assertTrue(self.update(4, walkable=True)["walkable"])
        with self.Session() as db:
            self.assertEqual(db.get(DogWalkSetting, result["id"]).created_at, created)

    def test_nullable_fields_explicitly_clear(self):
        self.authenticate()
        result = self.update(4, precautions=None, recommended_duration_minutes=None)
        self.assertIsNone(result["precautions"])
        self.assertIsNone(result["recommended_duration_minutes"])
        self.assertTrue(result["walkable"])
        self.assertEqual(len(result["slots"]), 1)

    def test_empty_patch_and_null_nonnullable_fields_are_422(self):
        self.authenticate()
        before = self.settings_snapshot()
        for body in ({}, {"walkable": None}, {"difficulty": None}, {"slots": None}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch("/dogs/4/walk-settings", json=body).status_code, 422)
        self.assertEqual(self.settings_snapshot(), before)

    def test_invalid_scalar_types_and_unknown_fields_are_422(self):
        self.authenticate()
        before = self.settings_snapshot()
        for body in (
            {"walkable": "true"}, {"walkable": 1}, {"difficulty": "INTERMEDIATE"},
            {"recommended_duration_minutes": 0}, {"recommended_duration_minutes": -1},
            {"recommended_duration_minutes": 1.5}, {"recommended_duration_minutes": True},
            {"recommended_duration_minutes": "30"}, {"recommended_duration_minutes": 2147483648},
            {"precautions": 123}, {"dog_id": 2}, {"walkable": True, "shelter_id": 2},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch("/dogs/4/walk-settings", json=body).status_code, 422)
        self.assertEqual(self.settings_snapshot(), before)

    def test_invalid_slot_shapes_times_and_weekdays_are_422_atomically(self):
        self.authenticate()
        before = self.settings_snapshot()
        for invalid in (
            {**slot(), "day_of_week": -1}, {**slot(), "day_of_week": 7},
            {**slot(), "day_of_week": "0"}, {**slot(), "day_of_week": True},
            slot(start="bad"), slot(start="24:00"), slot(start="11:00"), slot(start="12:00"),
            slot(start="23:00", end="01:00"), slot(start=3600),
            slot(start="09:00Z"), slot(start="09:00+09:00"), {**slot(), "id": 1},
            {"day_of_week": 0, "start_time": "09:00"},
        ):
            with self.subTest(invalid=invalid):
                response = self.client.patch("/dogs/4/walk-settings", json={"walkable": False, "slots": [slot(6), invalid]})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.settings_snapshot(), before)

    def test_duplicate_and_overlapping_slots_are_409_atomically(self):
        self.authenticate()
        before = self.settings_snapshot()
        for slots in ([slot(), slot()], [slot(), slot(start="10:00", end="12:00")],
                      [slot(start="10:00", end="10:30"), slot()],
                      [slot(2), slot(), slot(start="10:59", end="12:00")]):
            with self.subTest(slots=slots):
                response = self.client.patch("/dogs/4/walk-settings", json={"walkable": False, "slots": slots})
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.settings_snapshot(), before)
        self.assertEqual(self.client.patch("/dogs/1/walk-settings", json={"slots": [slot(), slot()]}).status_code, 409)
        self.assertEqual(self.settings_snapshot(), before)  # no partial first setting

    def test_adjacent_slots_different_days_and_seconds_are_allowed(self):
        self.authenticate()
        result = self.update(slots=[slot(0, "11:00", "12:00"), slot(0), slot(1),
                                    slot(6, "09:00:01.123456", "09:00:02.123456")])
        self.assertEqual([s["day_of_week"] for s in result["slots"]], [0, 0, 1, 6])
        self.assertEqual(result["slots"][-1]["start_time"], "09:00:01.123456")

    def test_slot_replacement_identical_replacement_and_clear(self):
        self.authenticate()
        other = self.client.get("/dogs/5").json()["walk_settings"]
        result = self.update(4, slots=[slot(3, "14:00", "15:00"), slot(6)])
        self.assertEqual([s["day_of_week"] for s in result["slots"]], [3, 6])
        result = self.update(4, slots=[slot(3, "14:00", "15:00"), slot(6)])
        self.assertEqual(len(result["slots"]), 2)
        self.assertEqual(self.update(4, slots=[])["slots"], [])
        with self.Session() as db:
            self.assertEqual(db.query(DogWalkSlot).filter_by(walk_setting_id=result["id"]).count(), 0)
        self.assertEqual(self.client.get("/dogs/5").json()["walk_settings"], other)

    def test_slots_only_patch_updates_parent_timestamp(self):
        self.authenticate()
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.Session.begin() as db:
            setting = db.query(DogWalkSetting).filter_by(dog_id=4).one()
            setting.updated_at = old
        self.update(4, slots=[])
        with self.Session() as db:
            self.assertGreater(db.query(DogWalkSetting).filter_by(dog_id=4).one().updated_at.year, 2020)

    def test_permissions_cover_roles_shelter_assignment_and_unassigned_dogs(self):
        before = self.settings_snapshot()
        for user_id, targets, status in (
            (None, (1,), 401), (1, (1, 2, 3), 403), (2, (2, 3), 403),
            (3, (1, 3), 403), (4, (1, 2, 3), 403),
        ):
            self.client.headers.pop("Authorization", None)
            if user_id:
                self.authenticate(user_id)
            for dog_id in targets:
                with self.subTest(user_id=user_id, dog_id=dog_id):
                    self.assertEqual(self.client.patch(f"/dogs/{dog_id}/walk-settings", json={"walkable": True}).status_code, status)
        self.assertEqual(self.settings_snapshot(), before)

    def test_invalid_and_expired_bearer_tokens_are_401(self):
        self.client.headers["Authorization"] = "Bearer invalid"
        self.assertEqual(self.client.patch("/dogs/1/walk-settings", json={"walkable": True}).status_code, 401)
        with self.Session.begin() as db:
            db.get(AuthSession, token_digest("walk-session-2")).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.authenticate()
        self.assertEqual(self.client.patch("/dogs/1/walk-settings", json={"walkable": True}).status_code, 401)

    def test_second_shelter_staff_and_admin_can_update_authorized_dogs(self):
        self.authenticate(3)
        self.assertTrue(self.update(2, walkable=True)["walkable"])
        self.authenticate(5)
        for dog_id in (1, 2, 3):
            self.assertTrue(self.update(dog_id, walkable=True)["walkable"])

    def test_missing_dog_update_and_detail_are_404(self):
        self.authenticate()
        before = self.settings_snapshot()
        self.assertEqual(self.client.patch("/dogs/999/walk-settings", json={"walkable": True}).status_code, 404)
        self.assertEqual(self.client.get("/dogs/999").status_code, 404)
        self.assertEqual(self.settings_snapshot(), before)

    def test_commit_failure_rolls_back_creation_or_replacement_without_details(self):
        class LockFailure(Exception):
            pgcode = "55P03"

        self.authenticate()
        before = self.settings_snapshot()
        for error, status in (
            (SQLAlchemyError("sensitive-internal"), 500),
            (IntegrityError("sensitive-internal", {}, Exception("private")), 409),
            (DBAPIError("sensitive-internal", {}, LockFailure("private")), 409),
            (DBAPIError("sensitive-internal", {}, Exception("private")), 500),
        ):
            for dog_id in (1, 4):
                rollbacks = []

                def failing_db():
                    with self.Session() as db:
                        with patch.object(db, "commit", side_effect=error), patch.object(db, "rollback", wraps=db.rollback) as rollback:
                            try:
                                yield db
                            finally:
                                rollbacks.append(rollback.called)

                self.app.dependency_overrides[dogs.get_db] = failing_db
                response = self.client.patch(f"/dogs/{dog_id}/walk-settings", json={"walkable": True, "slots": [slot(2)]})
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn("sensitive-internal", response.text)
                self.assertNotIn("private", response.text)
                self.assertEqual(rollbacks, [True])
                self.assertEqual(self.settings_snapshot(), before)

    def test_changing_settings_preserves_requests_walks_and_all_dog_columns(self):
        now = datetime.now(timezone.utc)
        with self.Session.begin() as db:
            request = WalkRequest(user_id=1, dog_id=4, shelter_id=1, status="APPROVED", reviewed_by=2,
                                  reviewed_at=now, requested_start_at=now, requested_end_at=now + timedelta(hours=1))
            db.add(request)
            db.flush()
            db.add(Walk(walk_request_id=request.id, started_at=now))
        before = self.rows(Dog), self.rows(WalkRequest), self.rows(Walk)
        self.authenticate()
        self.update(4, walkable=False, slots=[])
        self.assertEqual((self.rows(Dog), self.rows(WalkRequest), self.rows(Walk)), before)

    def test_public_reads_never_create_settings_and_missing_detail_is_null(self):
        before = self.settings_snapshot()
        for path in ("/dogs/", "/dogs/1", "/shelters/1/dogs", "/dogs/?walkable=false"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIsNone(self.client.get("/dogs/1").json()["walk_settings"])
        self.assertEqual(self.settings_snapshot(), before)

    def test_list_filters_include_unconfigured_as_false_and_combine_with_and(self):
        for path, expected in (
            ("/dogs/", [1, 2, 3, 4, 5]), ("/dogs/?walkable=true", [4]),
            ("/dogs/?walkable=false", [1, 2, 3, 5]), ("/dogs/?shelter_id=1", [1, 4, 5]),
            ("/dogs/?walkable=true&shelter_id=1", [4]),
            ("/dogs/?walkable=false&shelter_id=1", [1, 5]),
            ("/dogs/?walkable=true&shelter_id=2", []), ("/dogs/?shelter_id=999", []),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.ids(path), expected)

    def test_shelter_dogs_public_filter_empty_and_nonexistent(self):
        for path, expected in (("/shelters/1/dogs", [1, 4, 5]), ("/shelters/1/dogs?walkable=true", [4]),
                               ("/shelters/1/dogs?walkable=false", [1, 5]), ("/shelters/2/dogs", [2]),
                               ("/shelters/3/dogs", [])):
            self.assertEqual(self.ids(path), expected)
        self.assertEqual(self.client.get("/shelters/999/dogs").status_code, 404)

    def test_invalid_query_parameters_are_422(self):
        for path in ("/dogs/?walkable=unknown", "/dogs/?shelter_id=abc", "/shelters/1/dogs?walkable=unknown"):
            self.assertEqual(self.client.get(path).status_code, 422)

    def test_existing_response_fields_and_nested_orm_serialization(self):
        card = self.client.get("/dogs/").json()[0]
        self.assertEqual(set(card), set(DogCardResponse.model_fields))
        detail = self.client.get("/dogs/4").json()
        self.assertEqual(set(detail), set(DogDetailResponse.model_fields))
        self.assertEqual(detail["name"], "산책 가능")
        self.assertEqual(detail["shelter"]["name"], "첫 보호소")
        self.assertTrue(detail["walk_settings"]["walkable"])
        self.assertEqual(DogDetailResponse.model_validate(detail).model_dump(mode="json"), detail)

    def test_list_and_detail_queries_do_not_lazy_load_per_dog(self):
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            for path, expected in (("/dogs/", 1), ("/dogs/?walkable=false", 1),
                                   ("/shelters/1/dogs?walkable=true", 2), ("/dogs/4", 2)):
                statements.clear()
                self.assertEqual(self.client.get(path).status_code, 200)
                self.assertEqual(len(statements), expected, statements)
                self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements))
        finally:
            event.remove(self.engine, "before_cursor_execute", record)

    def test_shared_matching_detail_eager_loads_walk_settings(self):
        with self.Session.begin() as db:
            survey = Survey(user_id=1)
            db.add(survey)
            db.flush()
            db.add_all([MatchingResult(user_id=1, dog_id=i, survey_id=survey.id, score=80) for i in (1, 4, 5)])
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        self.authenticate(1)
        event.listen(self.engine, "before_cursor_execute", record)
        try:
            response = self.client.get("/matching/history/1")
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 3)
        self.assertEqual(len(statements), 4, statements)  # bearer + user + results + all slots
        self.assertEqual({r["dog_id"]: r["dog"]["walk_settings"] is not None for r in response.json()}, {1: False, 4: True, 5: True})

    def import_items(self, items, dry_run=False):
        self.authenticate(5)
        with patch("app.routers.public_api.fetch_pages", return_value=(items, {"pages": 1})):
            response = self.client.post("/public-api/fetch-dogs", json={"dry_run": dry_run})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_new_public_dog_stays_unconfigured_and_not_walkable(self):
        self.import_items([public_item("448567202600719")])
        with self.Session() as db:
            new_dog = db.query(Dog).filter_by(desertion_no="448567202600719").one()
            dog_id = new_dog.id
            self.assertIsNone(new_dog.walk_setting)
        self.assertNotIn(dog_id, self.ids("/dogs/?walkable=true"))
        self.assertIn(dog_id, self.ids("/dogs/?walkable=false"))

    def test_public_resync_and_payload_duplicates_preserve_settings_and_slots(self):
        self.authenticate()
        self.update(walkable=True, slots=[slot(), slot(2)])
        before = self.settings_snapshot()
        for _ in range(2):
            result = self.import_items([public_item(), public_item(processState="종료(입양)")])
            self.assertEqual(result["updated_count"], 1)
            self.assertEqual(result["duplicate_payload_count"], 1)
            self.assertEqual(self.settings_snapshot(), before)
        with self.Session() as db:
            self.assertEqual(db.query(Dog).filter_by(desertion_no="448567202600718").count(), 1)
            self.assertEqual(db.get(Dog, 1).energy_level, 2)

    def test_public_dry_run_preserves_all_dogs_settings_slots_and_shelters(self):
        self.authenticate()
        self.update(walkable=True, slots=[slot()])
        before = self.rows(Dog), self.settings_snapshot(), self.rows(Shelter)
        self.import_items([public_item(), public_item("448567202600719", careRegNo="999", careNm="새 보호소")], dry_run=True)
        self.assertEqual((self.rows(Dog), self.settings_snapshot(), self.rows(Shelter)), before)

    def test_import_role_policy_is_preserved(self):
        for user_id in (1, 2, 3, 4):
            self.authenticate(user_id)
            with patch("app.routers.public_api.fetch_pages") as fetch:
                self.assertEqual(self.client.post("/public-api/fetch-dogs", json={}).status_code, 403)
                fetch.assert_not_called()

    def test_manual_registration_preserves_identifier_reimport_reuses_row_and_duplicate_is_409(self):
        self.authenticate()
        body = {"name": "수동 등록", "shelter_id": 1, "desertion_no": "448567202600719"}
        self.assertEqual(self.client.post("/dogs/", json={**body, "desertion_no": "x" * 21}).status_code, 422)
        response = self.client.post("/dogs/", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        dog_id = response.json()["id"]
        self.assertEqual(response.json()["desertion_no"], body["desertion_no"])
        self.assertEqual(self.client.post("/dogs/", json=body).status_code, 409)
        self.import_items([public_item(body["desertion_no"])])
        with self.Session() as db:
            dog = db.query(Dog).filter_by(desertion_no=body["desertion_no"]).one()
            self.assertEqual(dog.id, dog_id)
            self.assertIsNone(dog.walk_setting)


if __name__ == "__main__":
    unittest.main()
