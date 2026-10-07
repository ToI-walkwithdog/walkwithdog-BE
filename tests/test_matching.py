"""Run with python -m unittest discover -s tests -v; no external DB is used."""

import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

# Set before app.database imports so .env cannot select the user's real DB.
with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.models import User, Survey, Dog, Shelter, MatchingResult
    from app.routers import surveys, matching, simulation

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

from app.schemas import SurveyCreate
from app.survey_codes import SURVEY_FIELDS, parse_walk_hours
from app.matching_service import build_analysis, calculate_score, get_score_label
from app.auth import get_current_user


def survey_values(**changes):
    values = dict.fromkeys((*SURVEY_FIELDS, "daily_walk_time", "mbti"))
    values.update(changes)
    return SimpleNamespace(**values)


def dog_values(**changes):
    values = {"name": "코기", "energy_level": 3, "sociability_level": 3,
              "independence_level": 3, "separation_anxiety_level": 3,
              "training_level": 3, "barking_level": 3, "trait_source": "test"}
    values.update(changes)
    return SimpleNamespace(**values)


class SurveyCodeTests(unittest.TestCase):
    def test_all_codes_and_labels_are_accepted(self):
        for field, enum in SURVEY_FIELDS.items():
            for member in enum:
                for value in (member.value, member.label):
                    with self.subTest(field=field, value=value):
                        model = SurveyCreate(user_id=1, **{field: value})
                        self.assertEqual(model.model_dump(mode="json")[field], member.value)

    def test_figma_spacing_and_range_aliases(self):
        model = SurveyCreate(user_id=1, family_type="영유아 자녀 가구 (0-6세)",
                             life_pattern="재택 / 프리랜서", walk_duration="20 ~ 40분")
        self.assertEqual(model.family_type.value, "young_children")
        self.assertEqual(model.life_pattern.value, "remote_worker")
        self.assertEqual(model.walk_duration.value, "20_to_40_minutes")
        self.assertEqual(SurveyCreate(user_id=1, family_type="부부 / 커플 (자녀 없음)").family_type.value,
                         "couple_without_children")

    def test_invalid_options_are_rejected(self):
        for field in SURVEY_FIELDS:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                SurveyCreate(user_id=1, **{field: "unknown"})

    def test_mbti_normalization_and_validation(self):
        self.assertEqual(SurveyCreate(user_id=1, mbti=" infj ").mbti.value, "INFJ")
        with self.assertRaises(ValidationError):
            SurveyCreate(user_id=1, mbti="XXXX")

    def test_walk_hours_units_and_limits(self):
        for value, expected in ((0, 0), (1.5, 1.5), ("30분", .5), ("1.5시간", 1.5), ("24", 24)):
            with self.subTest(value=value):
                self.assertEqual(parse_walk_hours(value), expected)
        for value in (-1, 25, True, "abc", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SurveyCreate(user_id=1, daily_walk_time=value)


class ScoreTests(unittest.TestCase):
    def test_codes_and_legacy_labels_have_identical_analysis(self):
        coded = survey_values(housing_type="studio", family_type="couple_without_children",
                              life_pattern="remote_worker", walk_duration="20_to_40_minutes")
        legacy = survey_values(housing_type="원룸/오피스텔", family_type="부부 / 커플 (자녀 없음)",
                               life_pattern="재택 / 프리랜서", walk_duration="20 ~ 40분")
        self.assertEqual(build_analysis(coded, dog_values()), build_analysis(legacy, dog_values()))

    def test_breakdown_is_actual_score_contribution(self):
        result = build_analysis(survey_values(housing_type="studio", family_type="single"),
                                dog_values(energy_level=5))
        self.assertEqual(result["score_breakdown"]["housing_match"], -10)
        self.assertEqual(result["score_breakdown"]["family_match"], 8)
        self.assertEqual(result["score"], 48)
        self.assertEqual(sum(result["score_breakdown"].values()), 48)

    def test_daily_walk_time_is_numeric_not_substring_matching(self):
        self.assertEqual(calculate_score(survey_values(daily_walk_time="30분"), dog_values()), 56)
        self.assertEqual(calculate_score(survey_values(daily_walk_time="1.5"), dog_values()), 58)
        self.assertEqual(calculate_score(survey_values(daily_walk_time="0.25"), dog_values(energy_level=5)), 50)

    def test_missing_traits_and_unknown_legacy_values(self):
        dog = dog_values(**dict.fromkeys(("energy_level", "sociability_level", "independence_level",
                                         "training_level", "separation_anxiety_level", "barking_level")))
        result = build_analysis(survey_values(housing_type="unknown", daily_walk_time="unknown", mbti="InvalidJ"), dog)
        self.assertEqual(result["score"], 50)
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["care_points"])

    def test_high_score_is_clamped(self):
        survey = survey_values(housing_type="house_with_yard", family_type="multigenerational",
                               outing_frequency="mostly_home", daily_walk_time=2,
                               life_pattern="remote_worker", walk_frequency="daily",
                               walk_duration="60_minutes_or_more", dog_experience="current_owner", mbti="ENFJ")
        dog = dog_values(energy_level=5, sociability_level=5, independence_level=5,
                         separation_anxiety_level=5, training_level=5)
        result = build_analysis(survey, dog)
        self.assertGreater(sum(result["score_breakdown"].values()), 100)
        self.assertEqual(result["score"], 100)

    def test_warnings_use_codes(self):
        result = build_analysis(survey_values(housing_type="studio", outing_frequency="daily",
                                              walk_duration="under_20_minutes"),
                                dog_values(energy_level=5, separation_anxiety_level=5, barking_level=5))
        self.assertEqual(len(result["warnings"]), 3)

    def test_label_boundaries(self):
        for score, label in ((85, "매우 잘 맞아요"), (84, "잘 맞는 편이에요"), (70, "잘 맞는 편이에요"),
                             (69, "조금 주의가 필요해요"), (50, "조금 주의가 필요해요"), (49, "신중한 고민이 필요해요")):
            with self.subTest(score=score):
                self.assertEqual(get_score_label(score), label)


class MatchingAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(surveys.router)
        app.include_router(matching.router)
        app.include_router(simulation.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        # These regression tests exercise scoring, not authentication.
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        self.client = TestClient(app)
        with self.Session() as db:
            db.add(User(id=1, email="test@example.com", nickname="테스트"))
            db.add(Shelter(id=1, name="테스트 보호소", phone="02-123-4567", address="서울"))
            db.commit()
            for index in range(1, 8):
                db.add(Dog(id=index, shelter_id=1, name=f"강아지{index}", energy_level=index % 5 + 1,
                           independence_level=3, sociability_level=4, separation_anxiety_level=4,
                           training_level=3, barking_level=4))
            db.commit()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def create_survey(self, **changes):
        payload = {"user_id": 1, "housing_type": "studio", "family_type": "single",
                   "outing_frequency": "daily", "daily_walk_time": .5,
                   "life_pattern": "office_worker", "walk_frequency": "3_to_4_per_week",
                   "walk_duration": "20_to_40_minutes", "dog_experience": "first_time", "mbti": "INFJ"}
        payload.update(changes)
        response = self.client.post("/surveys/", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def generate(self, survey_id):
        response = self.client.post(f"/matching/{survey_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_survey_round_trip_stores_codes(self):
        survey = self.create_survey(family_type="부부 / 커플 (자녀 없음)", life_pattern="재택 / 프리랜서",
                                    walk_duration="20 ~ 40분", daily_walk_time="30분", mbti="infj")
        self.assertEqual(survey["family_type"], "couple_without_children")
        self.assertEqual(survey["daily_walk_time"], .5)
        with self.Session() as db:
            row = db.get(Survey, survey["id"])
            self.assertEqual(row.life_pattern, "remote_worker")
            self.assertEqual(row.daily_walk_time, "0.5")
        self.assertEqual(self.client.get("/surveys/1").json(), [survey])

    def test_options_route_and_openapi_enums(self):
        response = self.client.get("/surveys/options")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["housing_type"][0], {"code": "studio", "label": "원룸/오피스텔"})
        schema = self.client.get("/openapi.json").json()["components"]["schemas"]
        self.assertIn("studio", schema["HousingType"]["enum"])

    def test_invalid_survey_returns_422_and_does_not_save(self):
        for field, value in (("family_type", "unknown"), ("mbti", "XXXX"), ("daily_walk_time", -1)):
            with self.subTest(field=field):
                self.assertEqual(self.client.post("/surveys/", json={"user_id": 1, field: value}).status_code, 422)
        with self.Session() as db:
            self.assertEqual(db.query(Survey).count(), 0)

    def test_missing_user_and_survey_return_404(self):
        self.assertEqual(self.client.post("/surveys/", json={"user_id": 999}).status_code, 403)
        self.assertEqual(self.client.post("/matching/999").status_code, 404)
        self.assertEqual(self.client.post("/matching/score", json={"user_id": 999, "dog_id": 1}).status_code, 403)

    def test_missing_dog_returns_404(self):
        self.create_survey()
        self.assertEqual(self.client.post("/matching/score", json={"user_id": 1, "dog_id": 999}).status_code, 404)

    def test_no_dogs_returns_404_without_history(self):
        survey = self.create_survey()
        with self.Session() as db:
            db.query(Dog).delete()
            db.commit()
        self.assertEqual(self.client.post(f"/matching/{survey['id']}").status_code, 404)
        self.assertEqual(self.client.get("/matching/history/1").json(), [])

    def test_result_contains_full_analysis_dog_and_shelter(self):
        survey = self.create_survey()
        results = self.generate(survey["id"])
        self.assertEqual(len(results), 5)
        self.assertEqual([(-r["score"], r["dog_id"]) for r in results],
                         sorted((-r["score"], r["dog_id"]) for r in results))
        for result in results:
            self.assertEqual(result["survey_id"], survey["id"])
            self.assertEqual(result["dog"]["shelter"]["phone"], "02-123-4567")
            single = self.client.post("/matching/score", json={"user_id": 1, "dog_id": result["dog_id"]}).json()
            for key in single:
                self.assertEqual(result[key], single[key])
            self.assertTrue(result["care_points"])

    def test_repeated_request_is_idempotent(self):
        survey = self.create_survey()
        first = self.generate(survey["id"])
        self.assertEqual(self.generate(survey["id"]), first)
        with self.Session() as db:
            self.assertEqual(db.query(MatchingResult).count(), 5)

    def test_new_survey_preserves_historical_analysis(self):
        first = self.generate(self.create_survey()["id"])
        second = self.create_survey(housing_type="house_with_yard", outing_frequency="mostly_home")
        self.generate(second["id"])
        with self.Session() as db:
            db.get(Dog, first[0]["dog_id"]).energy_level = 5
            db.commit()
        history = self.client.get("/matching/history/1").json()
        self.assertEqual(len(history), 10)
        by_id = {r["id"]: r for r in history}
        for result in first:
            for key in ("score", "reason", "detail_analysis", "warnings", "care_points", "score_breakdown"):
                self.assertEqual(by_id[result["id"]][key], result[key])

    def test_legacy_history_does_not_use_latest_survey(self):
        with self.Session() as db:
            db.add(MatchingResult(user_id=1, dog_id=1, score=42, reason="이전 설명", detail_analysis="이전 분석"))
            db.commit()
        self.create_survey()
        row = self.client.get("/matching/history/1").json()[0]
        self.assertEqual(row["reason"], "이전 설명")
        self.assertIsNone(row["survey_id"])
        self.assertEqual(row["warnings"], [])
        self.assertEqual(row["score_breakdown"], {})

    def test_unique_constraint_blocks_duplicate_results(self):
        survey = self.create_survey()
        first = self.generate(survey["id"])[0]
        with self.Session() as db:
            db.add(MatchingResult(user_id=1, survey_id=survey["id"], dog_id=first["dog_id"], score=50))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_simulation_warnings_work_with_canonical_codes(self):
        self.create_survey(walk_duration="under_20_minutes")
        response = self.client.get("/simulation/timeline", params={"user_id": 1, "dog_id": 4})
        self.assertEqual(response.status_code, 200)
        timeline = response.json()["timeline"]
        self.assertIsNotNone(timeline[1]["warning"])
        self.assertIsNotNone(timeline[2]["warning"])

    def test_legacy_survey_is_read_as_codes_without_db_mutation(self):
        with self.Session() as db:
            db.add(Survey(user_id=1, family_type="부부/커플 (자녀없음)", life_pattern="재택/프리랜서",
                          walk_duration="20~40분", daily_walk_time="30분"))
            db.commit()
        row = self.client.get("/surveys/1").json()[0]
        self.assertEqual(row["family_type"], "couple_without_children")
        self.assertEqual(row["daily_walk_time"], .5)
        self.generate(row["id"])

    def test_unknown_legacy_answers_do_not_break_reads(self):
        with self.Session() as db:
            db.add(Survey(user_id=1, housing_type="옛날 선택값", daily_walk_time="미정", mbti="미정"))
            db.commit()
        response = self.client.get("/surveys/1")
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()[0]
        self.assertEqual(row["housing_type"], "옛날 선택값")
        self.generate(row["id"])

    def test_competing_request_unique_conflict_rolls_back_and_returns_existing(self):
        survey = self.create_survey()
        first = self.generate(survey["id"])
        original = matching.survey_results
        calls = 0

        def concurrent_lookup(db, survey_id):
            nonlocal calls
            calls += 1
            # Simulate another request committing after our initial lookup.
            return [] if calls == 1 else original(db, survey_id)

        with patch.object(matching, "survey_results", side_effect=concurrent_lookup):
            self.assertEqual(self.generate(survey["id"]), first)
        with self.Session() as db:
            self.assertEqual(db.query(MatchingResult).count(), 5)

    def test_fewer_dogs_null_traits_and_no_shelter(self):
        survey = self.create_survey()
        with self.Session() as db:
            db.query(Dog).delete()
            db.add(Dog(id=10, name="정보 미상"))
            db.commit()
        result = self.generate(survey["id"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 50)
        self.assertIsNone(result[0]["dog"]["shelter"])

    def test_history_is_filtered_by_user(self):
        self.generate(self.create_survey()["id"])
        self.assertEqual(self.client.get("/matching/history/999").status_code, 403)


if __name__ == "__main__":
    unittest.main()
