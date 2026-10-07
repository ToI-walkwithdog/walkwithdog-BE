import argparse
import contextlib
import io
import os
import unittest
from unittest.mock import patch, Mock

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.models import User, Shelter, Dog, Survey, MatchingResult
    from app.routers import surveys, matching
    from scripts.seed_demo import seed, EMAIL, SHELTER_KEY

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.engine import make_url
from scripts.prepare_demo_db import schema_state, prepare, backup_database, PreparationError
from scripts.demo_api import run_demo, request_json, DemoError, main, local_base_url, positive_timeout


class DemoToolTests(unittest.TestCase):
    def setUp(self):
        self.demo_config = patch.dict(os.environ, {"MATCHING_INCLUDE_DEMO": "true"})
        self.demo_config.start()
        self.addCleanup(self.demo_config.stop)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(surveys.router)
        app.include_router(matching.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        # The demo/scoring regressions have a test-only authenticated identity.
        from app.auth import get_current_user
        from types import SimpleNamespace
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def seed(self, apply=True):
        with self.Session.begin() as db:
            return seed(db, apply)

    def counts(self):
        with self.Session() as db:
            return tuple(db.query(model).count() for model in (User, Shelter, Dog, Survey, MatchingResult))

    def test_dry_run_does_not_insert(self):
        result = self.seed(False)
        self.assertEqual(result["planned_creates"], {"users": 1, "shelters": 1, "dogs": 6})
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
        self.assertIsNone(result["user_id"])

    def test_seed_twice_reuses_ids_without_overwriting(self):
        first = self.seed()
        with self.Session.begin() as db:
            db.get(Dog, first["dog_ids"][0]).energy_level = 4
        second = self.seed()
        self.assertEqual(first["dog_ids"], second["dog_ids"])
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertEqual(second["planned_creates"], {"users": 0, "shelters": 0, "dogs": 0})
        self.assertEqual(self.counts(), (1, 1, 6, 0, 0))
        with self.Session() as db:
            self.assertEqual(db.get(Dog, first["dog_ids"][0]).energy_level, 4)
            self.assertIsNone(db.get(User, first["user_id"]).password)

    def test_existing_records_are_untouched(self):
        with self.Session.begin() as db:
            db.add(User(email="existing@example.com", nickname="기존 사용자", password="unchanged"))
            db.add(Shelter(name="기존 보호소"))
            db.add(Dog(name="기존 강아지", energy_level=5))
        result = self.seed()
        self.assertEqual(self.counts(), (2, 2, 7, 0, 0))
        with self.Session() as db:
            user = db.query(User).filter(User.email == "existing@example.com").one()
            self.assertEqual(user.password, "unchanged")
            self.assertNotEqual(user.id, result["user_id"])
            self.assertEqual(db.query(Dog).filter(Dog.name == "기존 강아지").one().energy_level, 5)

    def test_user_key_collision_aborts(self):
        with self.Session.begin() as db:
            db.add(User(email=EMAIL, nickname="다른 사용자"))
        with self.assertRaises(ValueError):
            self.seed()
        self.assertEqual(self.counts(), (1, 0, 0, 0, 0))

    def test_shelter_key_collision_aborts(self):
        with self.Session.begin() as db:
            db.add(Shelter(desertion_no=SHELTER_KEY, name="기존 보호소"))
        with self.assertRaises(ValueError):
            self.seed()
        self.assertEqual(self.counts(), (0, 1, 0, 0, 0))

    def test_dog_key_collision_aborts_without_partial_inserts(self):
        with self.Session.begin() as db:
            db.add(Dog(desertion_no="DEMO-API-01", name="기존 강아지"))
        with self.assertRaises(ValueError):
            self.seed()
        self.assertEqual(self.counts(), (0, 0, 1, 0, 0))

    def test_transaction_rolls_back_mid_seed_failure(self):
        with self.assertRaises(RuntimeError):
            with self.Session.begin() as db:
                original_flush = db.flush
                calls = 0

                def fail_after_parent_creation(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    # Query autoflush is disabled to isolate the two explicit flushes.
                    if calls == 2:
                        raise RuntimeError("test failure")
                    return original_flush(*args, **kwargs)

                db.autoflush = False
                with patch.object(db, "flush", side_effect=fail_after_parent_creation):
                    seed(db, True)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_api_demo_and_repeated_runs_preserve_prior_history(self):
        user_id = self.seed()["user_id"]
        first = run_demo(self.client, user_id, report=lambda _: None)
        second = run_demo(self.client, user_id, report=lambda _: None)
        self.assertTrue(first["passed"] and second["passed"])
        self.assertEqual(first["result_counts"], [5, 5])
        self.assertTrue(set(first["survey_ids"]).isdisjoint(second["survey_ids"]))
        self.assertEqual(self.counts(), (1, 1, 6, 4, 20))

    def test_demo_fails_on_missing_user(self):
        with self.assertRaisesRegex(DemoError, "2. 첫 설문 저장: HTTP 403"):
            run_demo(self.client, 999, report=lambda _: None)

    def test_demo_fails_without_dogs(self):
        with self.Session.begin() as db:
            user = User(email="no-dogs@example.invalid", nickname="테스트")
            db.add(user)
            db.flush()
            user_id = user.id
        with self.assertRaisesRegex(DemoError, "3. 매칭 생성: HTTP 404"):
            run_demo(self.client, user_id, report=lambda _: None)

    def test_current_schema_is_ready(self):
        with self.engine.connect() as connection:
            self.assertEqual(schema_state(connection), {"ready": True, "missing": []})

    def test_wrong_column_type_is_rejected(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            columns = inspector.get_columns("matching_results")
            for column in columns:
                if column["name"] == "survey_id":
                    column["type"] = String()
            with patch("scripts.prepare_demo_db.inspect", return_value=inspector):
                with patch.object(inspector, "get_columns", return_value=columns):
                    with self.assertRaisesRegex(PreparationError, "타입"):
                        schema_state(connection)

    def test_missing_foreign_key_is_rejected(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            with patch("scripts.prepare_demo_db.inspect", return_value=inspector):
                with patch.object(inspector, "get_foreign_keys", return_value=[]):
                    with self.assertRaisesRegex(PreparationError, "외래키"):
                        schema_state(connection)

    def test_wrong_unique_index_shape_is_rejected(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            with patch("scripts.prepare_demo_db.inspect", return_value=inspector):
                with patch.object(inspector, "get_unique_constraints", return_value=[
                    {"name": "uq_matching_survey_dog", "column_names": ["dog_id"]}
                ]):
                    with self.assertRaisesRegex(PreparationError, "인덱스"):
                        schema_state(connection)

    def test_backup_failure_prevents_migration(self):
        with patch.object(self.engine.dialect, "name", "postgresql"), \
             patch.object(self.engine, "url", make_url("postgresql://localhost/test")), \
             patch("scripts.prepare_demo_db.schema_state", return_value={"ready": False, "missing": ["survey_id"]}), \
             patch("scripts.prepare_demo_db.backup_database", side_effect=PreparationError("백업 실패")), \
             patch("scripts.prepare_demo_db.apply_migration") as migration:
            with self.assertRaisesRegex(PreparationError, "백업 실패"):
                prepare(self.engine, True)
            migration.assert_not_called()

    def test_missing_backup_tools_fail_closed(self):
        with patch("scripts.prepare_demo_db.shutil.which", return_value=None):
            with self.assertRaises(PreparationError):
                backup_database(self.engine, "unused")

    def test_malformed_results_fail_validation(self):
        user_id = self.seed()["user_id"]
        original = request_json

        def missing_field(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[3] == "3. 매칭 생성":
                response[0]["dog"].pop("shelter")
            return response

        with patch("scripts.demo_api.request_json", side_effect=missing_field):
            with self.assertRaisesRegex(DemoError, "성향/보호소"):
                run_demo(self.client, user_id, report=lambda _: None)

    def test_changed_repeat_is_rejected(self):
        user_id = self.seed()["user_id"]
        original = request_json

        def changed_repeat(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[3] == "4. 매칭 재호출":
                response[0]["reason"] = "변경된 설명"
            return response

        with patch("scripts.demo_api.request_json", side_effect=changed_repeat):
            with self.assertRaisesRegex(DemoError, "결과/ID가 변경"):
                run_demo(self.client, user_id, report=lambda _: None)

    def test_changed_history_analysis_is_rejected(self):
        user_id = self.seed()["user_id"]
        original = request_json

        def changed_history(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[3] == "6. 이전 분석 보존 확인":
                response[-1]["reason"] = "변경된 설명"
            return response

        with patch("scripts.demo_api.request_json", side_effect=changed_history):
            with self.assertRaisesRegex(DemoError, "분석 내용이 변경"):
                run_demo(self.client, user_id, report=lambda _: None)


class DemoHTTPErrorTests(unittest.TestCase):
    def test_timeout_has_stage_and_is_forwarded(self):
        client = Mock()
        client.request.side_effect = httpx.ReadTimeout("test")
        with self.assertRaisesRegex(DemoError, "단계: 요청 시간 초과"):
            request_json(client, "GET", "/surveys/options", "단계", 3)
        self.assertEqual(client.request.call_args.kwargs["timeout"], 3)

    def test_network_error_is_sanitized(self):
        client = Mock()
        client.request.side_effect = httpx.ConnectError("SECRET")
        with self.assertRaisesRegex(DemoError, "서버 연결 실패") as error:
            request_json(client, "GET", "/", "단계", 2)
        self.assertNotIn("SECRET", str(error.exception))

    def test_non_200_has_status_and_no_sensitive_body(self):
        client = Mock()
        client.request.return_value = httpx.Response(500, text="SECRET")
        with self.assertRaisesRegex(DemoError, "단계: HTTP 500") as error:
            request_json(client, "GET", "/", "단계", 2)
        self.assertNotIn("SECRET", str(error.exception))

    def test_non_json_is_rejected(self):
        client = Mock()
        client.request.return_value = httpx.Response(200, text="not-json")
        with self.assertRaisesRegex(DemoError, "JSON"):
            request_json(client, "GET", "/", "단계", 2)

    def test_malformed_options_stop_before_survey_creation(self):
        client = Mock()
        client.request.return_value = httpx.Response(200, json={"housing_type": [{"code": ["invalid"], "label": "x"}]})
        with self.assertRaises(DemoError):
            run_demo(client, 1, report=lambda _: None)
        self.assertEqual(client.request.call_count, 1)

    def test_main_returns_nonzero_on_failure(self):
        client = Mock()
        client.request.side_effect = httpx.ConnectError("test")
        with patch("scripts.demo_api.httpx.Client") as client_type, contextlib.redirect_stdout(io.StringIO()):
            client_type.return_value.__enter__.return_value = client
            self.assertEqual(main(["--user-id", "1"]), 1)

    def test_local_only_url_and_timeout_validation(self):
        self.assertEqual(local_base_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        for value in ("https://example.com", "http://user:pass@localhost:8000", "http://localhost:8000?x=1"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                local_base_url(value)
        for value in ("0", "-1", "nan", "inf", "121"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                positive_timeout(value)
