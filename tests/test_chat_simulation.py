import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.models import User, Dog, Survey, ChatSession, ChatMessage
    from app.auth import get_current_user
    from app.routers import chat, simulation
    from app.ai_chat import generate_reply, redact_personal_data
    from app.eligibility import recommendation_candidate

import requests
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from scripts.evaluate_ai_safety import medical_checks


class ChatSimulationTests(unittest.TestCase):
    def test_medical_evaluation_accepts_safe_referral_phrasing(self):
        safe = "제가 확정할 수 없어요. 보호소나 수의사를 통해 정확한 검진을 받고 병원에 문의해 주세요."
        self.assertTrue(all(medical_checks(safe).values()))
        unsafe = "심장사상충은 음성입니다. 건강합니다."
        self.assertFalse(medical_checks(unsafe)["no_diagnosis"])

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(chat.router)
        app.include_router(simulation.router)
        self.current_id = 1
        def test_db():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=self.current_id)
        self.client = TestClient(app)
        self.settings = patch.dict(os.environ, {"GEMINI_API_KEY": "never-echo", "GEMINI_MODEL": "test-model"})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        with self.Session.begin() as db:
            db.add_all([User(id=1, email="private@example.com", nickname="private"),
                        User(id=2, email="other@example.com", nickname="other"),
                        Dog(id=1, name="강아지", energy_level=5, separation_anxiety_level=5, trait_source="shelter_text_parsed"),
                        Survey(id=1, user_id=1, housing_type="studio", outing_frequency="daily", daily_walk_time="0.5", walk_duration="under_20_minutes"),
                        Survey(id=2, user_id=2), Survey(id=3, user_id=1, outing_frequency="mostly_home", daily_walk_time="2")])

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def create_chat(self, **changes):
        return self.client.post("/chat/sessions", json={"dog_id": 1, "survey_id": 1, "consent_to_external_ai": True, **changes})

    def test_consent_and_configuration_required(self):
        self.assertEqual(self.create_chat(consent_to_external_ai=False).status_code, 422)
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertEqual(self.create_chat().status_code, 503)
        with self.Session() as db:
            self.assertEqual(db.query(ChatSession).count(), 0)

    def test_ownership_and_missing_entities(self):
        self.assertEqual(self.create_chat(survey_id=2).status_code, 404)
        self.assertEqual(self.create_chat(dog_id=999).status_code, 404)
        session_id = self.create_chat().json()["id"]
        self.current_id = 2
        self.assertEqual(self.client.get(f"/chat/sessions/{session_id}/messages").status_code, 404)
        with patch("app.routers.chat.generate_reply") as provider:
            self.assertEqual(self.client.post(f"/chat/sessions/{session_id}/messages", json={"message": "hi"}).status_code, 404)
            provider.assert_not_called()

    def test_context_snapshot_and_provider_payload_exclude_identity(self):
        session_id = self.create_chat().json()["id"]
        with self.Session.begin() as db:
            db.get(Survey, 1).housing_type = "house_with_yard"
        with patch("app.routers.chat.generate_reply", return_value=("가상 답변", "test-model")) as provider:
            response = self.client.post(f"/chat/sessions/{session_id}/messages", json={"message": "산책은 어때?"})
            self.assertEqual(response.status_code, 200, response.text)
            context = provider.call_args.args[0]
            self.assertEqual(context["lifestyle"]["housing_type"], "studio")
            self.assertNotIn("user_id", str(context))
            self.assertNotIn("private@example.com", str(context))
            self.assertNotIn("survey_id", str(context))
        history = self.client.get(f"/chat/sessions/{session_id}/messages").json()["messages"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])

    def test_provider_failure_has_no_partial_message(self):
        session_id = self.create_chat().json()["id"]
        with patch("app.routers.chat.generate_reply", side_effect=HTTPException(504, "timeout")):
            self.assertEqual(self.client.post(f"/chat/sessions/{session_id}/messages", json={"message": "hi"}).status_code, 504)
        with self.Session() as db:
            self.assertEqual(db.query(ChatMessage).count(), 0)

    def test_personal_data_is_redacted_before_provider_and_storage(self):
        session_id = self.create_chat().json()["id"]
        raw = "연락처는 test.person@example.com, 010-1234-5678이고 키는 AIza" + "A" * 32
        with patch("app.routers.chat.generate_reply", return_value=("민감정보를 입력하지 마세요.", "test-model")) as provider:
            response = self.client.post(f"/chat/sessions/{session_id}/messages", json={"message": raw})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["pii_redacted"])
            sent = provider.call_args.args[2]
            self.assertNotIn("test.person@example.com", sent)
            self.assertNotIn("010-1234-5678", sent)
            self.assertNotIn("AIza", sent)
        history = self.client.get(f"/chat/sessions/{session_id}/messages").json()["messages"]
        self.assertNotIn("test.person@example.com", history[0]["content"])
        self.assertIn("[이메일 삭제]", history[0]["content"])

    def test_redaction_patterns_and_non_personal_text(self):
        raw = "900101-1234567 1234-5678-9012-3456 Bearer abcdefghijklmnopqrstuvwxyz"
        redacted, changed = redact_personal_data(raw)
        self.assertTrue(changed)
        for value in ("900101-1234567", "1234-5678-9012-3456", "abcdefghijklmnopqrstuvwxyz"):
            self.assertNotIn(value, redacted)
        self.assertEqual(redact_personal_data("산책을 좋아해요"), ("산책을 좋아해요", False))

    def test_message_limits(self):
        session_id = self.create_chat().json()["id"]
        path = f"/chat/sessions/{session_id}/messages"
        for message in (" ", "x" * 1001):
            self.assertEqual(self.client.post(path, json={"message": message}).status_code, 422)
        with patch("app.routers.chat.generate_reply", return_value=("응답", "test-model")):
            self.assertEqual(self.client.post(path, json={"message": "x" * 1000}).status_code, 200)
        with self.Session.begin() as db:
            # The accepted pair above counts toward the 40-message limit.
            db.add_all([ChatMessage(session_id=session_id, role="user" if i % 2 == 0 else "assistant", content="hi") for i in range(38)])
        self.assertEqual(self.client.post(path, json={"message": "hi"}).status_code, 409)

    def test_real_adapter_request_and_structured_response(self):
        response = Mock(status_code=200, json=Mock(return_value={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"reply":"가상 대화입니다."}'}]}}]}))
        with patch("app.ai_chat.requests.post", return_value=response) as provider:
            reply, model = generate_reply({"dog": {"name": "강아지"}}, [], "안녕")
            self.assertEqual(reply, "가상 대화입니다.")
            self.assertEqual(model, "test-model")
            self.assertEqual(provider.call_args.kwargs["headers"], {"x-goog-api-key": "never-echo"})
            self.assertEqual(provider.call_args.kwargs["timeout"], (5, 20))
            payload = provider.call_args.kwargs["json"]
            self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingBudget"], 256)

    def test_provider_history_is_limited_to_last_ten_messages(self):
        history = [SimpleNamespace(role="user" if i % 2 == 0 else "assistant", content=f"message-{i}")
                   for i in range(12)]
        response = Mock(status_code=200, json=Mock(return_value={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"reply":"응답"}'}]}}]}))
        with patch("app.ai_chat.requests.post", return_value=response) as provider:
            generate_reply({}, history, "latest")
        contents = provider.call_args.kwargs["json"]["contents"]
        self.assertEqual(len(contents), 13)  # context, acknowledgement, 10 history, latest
        texts = [part["text"] for content in contents for part in content["parts"]]
        self.assertNotIn("message-0", texts)
        self.assertNotIn("message-1", texts)
        self.assertIn("message-2", texts)
        self.assertIn("message-11", texts)

    def test_adapter_timeouts_blocked_malformed_and_upstream_failure(self):
        with patch("app.ai_chat.requests.post", return_value=Mock(status_code=429)):
            with self.assertRaises(HTTPException) as limited:
                generate_reply({}, [], "hi")
            self.assertEqual(limited.exception.status_code, 429)
            self.assertEqual(limited.exception.headers["Retry-After"], "30")
        with patch("app.ai_chat.requests.post", return_value=Mock(status_code=404)):
            with self.assertRaises(HTTPException) as missing:
                generate_reply({}, [], "hi")
            self.assertEqual(missing.exception.status_code, 503)
        bad_responses = [Mock(status_code=500), Mock(status_code=200, json=Mock(return_value={})),
                         Mock(status_code=200, json=Mock(return_value={"candidates": [{"finishReason": "SAFETY"}]})),
                         Mock(status_code=200, json=Mock(return_value={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "invalid"}]}}]}))]
        for response in bad_responses:
            with patch("app.ai_chat.requests.post", return_value=response):
                with self.assertRaises(HTTPException) as error:
                    generate_reply({}, [], "hi")
                self.assertEqual(error.exception.status_code, 502)
                self.assertNotIn("never-echo", error.exception.detail)
        with patch("app.ai_chat.requests.post", side_effect=requests.Timeout):
            with self.assertRaises(HTTPException) as error:
                generate_reply({}, [], "hi")
            self.assertEqual(error.exception.status_code, 504)

    def test_simulation_explicit_survey_stable_events_and_unknowns(self):
        first = self.client.get("/simulation/timeline?user_id=1&dog_id=1&survey_id=1").json()
        latest = self.client.get("/simulation/timeline?user_id=1&dog_id=1").json()
        self.assertEqual(first["survey_id"], 1)
        self.assertEqual(latest["survey_id"], 3)
        self.assertEqual(first["daily_walk_minutes"], 30)
        self.assertEqual(latest["daily_walk_minutes"], 120)
        self.assertTrue(first["is_simulation"])
        self.assertTrue(first["timeline"][1]["warning"])
        self.assertIsNone(latest["timeline"][1]["warning"])
        self.assertEqual(first["timeline"][0]["event_id"], "1:1:morning")
        self.assertEqual(self.client.get("/simulation/timeline?user_id=1&dog_id=1&survey_id=2").status_code, 404)

    def test_simulation_interactions_and_invalid_actions(self):
        payload = {"survey_id": 1, "dog_id": 1, "time": "daytime", "action": "enrichment"}
        response = self.client.post("/simulation/interaction", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["is_simulation"])
        self.assertEqual(self.client.post("/simulation/interaction", json={**payload, "action": "play"}).status_code, 422)
        self.assertEqual(self.client.post("/simulation/interaction", json={**payload, "survey_id": 2}).status_code, 404)
        self.assertEqual(self.client.post("/simulation/interaction", json={**payload, "action": "unknown"}).status_code, 422)

    def test_recommendation_excludes_demo_closed_and_returning_owner(self):
        with patch.dict(os.environ, {"MATCHING_INCLUDE_DEMO": "false"}):
            self.assertFalse(recommendation_candidate(Dog(trait_source="manual_demo")))
        self.assertFalse(recommendation_candidate(Dog(process_state="종료(입양)")))
        self.assertFalse(recommendation_candidate(Dog(description="보호자와 연락됨")))
        self.assertTrue(recommendation_candidate(Dog(process_state="보호중")))


if __name__ == "__main__":
    unittest.main()
