import os
import unittest
from unittest.mock import patch, Mock

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.models import Dog, Shelter
    from app.public_data import synchronize, fetch_pages, fetch_page, age_number
    from app.personality import personality_data
    from app.routers import public_api, personality

import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def item(**changes):
    return {"desertionNo": "448567202600718", "kindNm": "말티즈", "age": "2024(년생)",
            "sexCd": "M", "careNm": "보호소", "careAddr": "가상 주소", "careRegNo": "123",
            "processState": "보호중", "neuterYn": "Y", "specialMark": "친화적", **changes}


class PublicDataTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(public_api.router)
        app.include_router(personality.router)
        def db_override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = db_override
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_dry_run_has_no_changes(self):
        with self.Session.begin() as db:
            result = synchronize(db, [item()])
            self.assertEqual(result["saved_count"], 1)
            self.assertEqual(db.query(Dog).count(), 0)
            self.assertEqual(db.query(Shelter).count(), 0)

    def test_reimport_updates_without_duplicates_or_invented_traits(self):
        with self.Session.begin() as db:
            synchronize(db, [item()], False)
        with self.Session.begin() as db:
            dog = db.query(Dog).one()
            dog.training_level = 5
            dog.description = "보호소 확인 기록"
            identifier = dog.id
        with self.Session.begin() as db:
            result = synchronize(db, [item(processState="종료(입양)")], False)
            self.assertEqual(result["updated_count"], 1)
            dog = db.query(Dog).one()
            self.assertEqual(dog.id, identifier)
            self.assertEqual(dog.training_level, 5)
            self.assertIsNone(dog.energy_level)
            self.assertEqual(dog.description, "보호소 확인 기록")
            self.assertEqual(dog.process_state, "종료(입양)")
            self.assertIsNotNone(dog.synced_at)
            self.assertEqual(db.query(Shelter).count(), 1)

    def test_verified_legacy_id_reuses_row(self):
        with self.Session.begin() as db:
            db.add(Dog(name="기존", description="유기번호: 448567202600718", energy_level=4))
        with self.Session.begin() as db:
            result = synchronize(db, [item()], False)
            self.assertEqual(result["reused_legacy_count"], 1)
            self.assertEqual(db.query(Dog).count(), 1)
            self.assertEqual(db.query(Dog).one().energy_level, 4)

    def test_legacy_collision_stops_before_writing(self):
        with self.Session.begin() as db:
            db.add_all([Dog(name=str(i), description="유기번호: 448567202600718") for i in range(2)])
        with self.Session() as db:
            with self.assertRaises(HTTPException) as error:
                synchronize(db, [item()], False)
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(db.query(Shelter).count(), 0)

    def test_payload_duplicates_and_invalid_id(self):
        with self.Session.begin() as db:
            result = synchronize(db, [item(), item(), item(desertionNo="invalid")], False)
            self.assertEqual(result["duplicate_payload_count"], 1)
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(db.query(Dog).count(), 1)

    def test_import_requires_management_key_and_defaults_dry_run(self):
        self.assertIn(self.client.post("/public-api/fetch-dogs").status_code, (403, 503))
        with patch.dict(os.environ, {"PUBLIC_IMPORT_KEY": "management-secret"}):
            self.assertEqual(self.client.post("/public-api/fetch-dogs", headers={"X-Import-Key": "wrong"}).status_code, 403)
            with patch("app.routers.public_api.fetch_pages", return_value=([item()], {"pages": 1})):
                response = self.client.post("/public-api/fetch-dogs", headers={"X-Import-Key": "management-secret"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["dry_run"])
        with self.Session() as db:
            self.assertEqual(db.query(Dog).count(), 0)

    def test_mid_import_failure_rolls_back(self):
        with patch.dict(os.environ, {"PUBLIC_IMPORT_KEY": "management-secret"}), patch("app.routers.public_api.fetch_pages", return_value=([item(), item(desertionNo="448567202600719")], {})):
            from app.public_data import resolve_shelter
            with patch("app.public_data.resolve_shelter", side_effect=[None, HTTPException(409, "collision")]):
                response = self.client.post("/public-api/fetch-dogs", json={"dry_run": False}, headers={"X-Import-Key": "management-secret"})
                self.assertEqual(response.status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(Dog).count(), 0)

    def test_pagination_and_truncation(self):
        with patch("app.public_data.fetch_page", side_effect=[([item()], 3), ([item()], 3)]) as fetch:
            records, metadata = fetch_pages(2, 1)
            self.assertEqual(fetch.call_count, 2)
            self.assertTrue(metadata["truncated"])
        with patch("app.public_data.fetch_page", return_value=([item()], 1)) as fetch:
            self.assertFalse(fetch_pages(3, 1)[1]["truncated"])
            self.assertEqual(fetch.call_count, 1)

    def test_timeouts_and_invalid_remote_responses_are_sanitized(self):
        for response in (Mock(status_code=500), Mock(status_code=200, json=Mock(return_value={})),
                         Mock(status_code=200, json=Mock(return_value={"response": {"header": {"resultCode": "30"}}}))):
            with patch.dict(os.environ, {"PUBLIC_API_KEY": "never-echo"}), patch("app.public_data.requests.get", return_value=response):
                with self.assertRaises(HTTPException) as error:
                    fetch_page()
                self.assertEqual(error.exception.status_code, 502)
                self.assertNotIn("never-echo", error.exception.detail)
        with patch.dict(os.environ, {"PUBLIC_API_KEY": "never-echo"}), patch("app.public_data.requests.get", side_effect=requests.Timeout):
            with self.assertRaises(HTTPException) as error:
                fetch_page()
            self.assertEqual(error.exception.status_code, 504)

    def test_age_not_hardcoded(self):
        from datetime import datetime, timezone
        self.assertEqual(age_number(f"{datetime.now(timezone.utc).year - 2}(년생)"), 2)
        self.assertIsNone(age_number("2099(년생)"))

    def test_chart_nulls_and_source_labels(self):
        with self.Session.begin() as db:
            db.add(Dog(name="테스트", energy_level=4, training_level=9, trait_source="shelter_text_parsed"))
        response = self.client.get("/dogs/1/personality")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["axes"]), 5)
        self.assertTrue(data["estimated"])
        self.assertIn("training_level", data["missing_fields"])
        self.assertIsNone(data["axes"][3]["value"])
        self.assertEqual(self.client.get("/dogs/999/personality").status_code, 404)


if __name__ == "__main__":
    unittest.main()
