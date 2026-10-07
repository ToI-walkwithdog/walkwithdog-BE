"""Exercise real auth/API transactions on PostgreSQL, rollback all verification data."""
import secrets
import os
import argparse
from contextlib import nullcontext
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import engine, get_db
from app.routers import auth, users, surveys, matching, simulation, personality, chat
from scripts.prepare_demo_db import table_counts, validate_local_engine
from app.public_data import synchronize
from app.models import Dog


def verify(live_ai=False):
    validate_local_engine(engine)
    with engine.connect() as connection:
        before = table_counts(connection)
        connection.rollback()
        transaction = connection.begin()
        Session = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
        app = FastAPI()
        for router in (auth.router, users.router, surveys.router, matching.router, simulation.router, personality.router, chat.router):
            app.include_router(router)

        def db_override():
            with Session() as db:
                yield db

        app.dependency_overrides[get_db] = db_override
        try:
            with Session.begin() as db:
                identifier = "99" + str(secrets.randbelow(10**16)).zfill(16)
                source = {"desertionNo": identifier, "kindNm": "검증용", "processState": "보호중"}
                original_dogs = db.query(Dog).count()
                synchronize(db, [source], False)
                original_id = db.query(Dog).filter(Dog.desertion_no == identifier).one().id
                synchronize(db, [{**source, "processState": "종료(입양)"}], False)
                assert db.query(Dog).count() == original_dogs + 1
                assert db.query(Dog).filter(Dog.desertion_no == identifier).one().id == original_id
            with TestClient(app) as client:
                password = secrets.token_urlsafe(24)
                accounts = []
                for _ in range(2):
                    email = f"auth-verify-{secrets.token_hex(8)}@example.com"
                    response = client.post("/users/", json={"email": email, "nickname": "[VERIFY]", "password": password})
                    assert response.status_code == 200, "registration failed"
                    accounts.append((email, response.json()["id"]))
                response = client.post("/auth/login", json={"email": accounts[0][0], "password": password})
                assert response.status_code == 200, "login failed"
                client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
                assert client.get("/users/me").json()["id"] == accounts[0][1]
                assert client.get(f"/users/{accounts[1][1]}").status_code == 403
                assert client.post("/surveys/", json={"user_id": accounts[1][1]}).status_code == 403
                survey = client.post("/surveys/", json={"user_id": accounts[0][1], "housing_type": "studio"})
                assert survey.status_code == 200, "survey failed"
                path = f"/matching/{survey.json()['id']}"
                result = client.post(path)
                assert result.status_code == 200 and 1 <= len(result.json()) <= 5, "matching failed"
                assert result.json() == client.post(path).json(), "idempotency failed"
                history = client.get(f"/matching/history/{accounts[0][1]}").json()
                assert {r["id"]: r for r in result.json()} == {r["id"]: r for r in history}
                dog_id = result.json()[0]["dog_id"]
                chart = client.get(f"/dogs/{dog_id}/personality")
                assert chart.status_code == 200 and len(chart.json()["axes"]) == 5
                timeline = client.get("/simulation/timeline", params={"user_id": accounts[0][1], "dog_id": dog_id, "survey_id": survey.json()["id"]})
                assert timeline.status_code == 200 and len(timeline.json()["timeline"]) == 4
                interaction = client.post("/simulation/interaction", json={"dog_id": dog_id, "survey_id": survey.json()["id"], "time": "night", "action": "rest"})
                assert interaction.status_code == 200 and interaction.json()["is_simulation"]
                ai_context = (nullcontext() if live_ai else
                              patch("app.routers.chat.generate_reply", return_value=("격리된 검증용 응답", "verification-only")))
                with ai_context:
                    session = client.post("/chat/sessions", json={"dog_id": dog_id, "survey_id": survey.json()["id"], "consent_to_external_ai": True})
                    assert session.status_code == 200, "chat session persistence failed"
                    chat_path = f"/chat/sessions/{session.json()['id']}/messages"
                    message = client.post(chat_path, json={"message": "이 대화가 실제 행동이나 건강을 보장하는지 한 문장으로 알려줘."})
                    assert message.status_code == 200 and message.json().get("content"), (
                        f"chat generation failed: HTTP {message.status_code} "
                        f"{message.json().get('detail', 'missing content')}"
                    )
                    assert message.json()["pii_redacted"] is False
                    assert len(client.get(chat_path).json()["messages"]) == 2
                assert client.post("/auth/logout").status_code == 204
                assert client.get("/users/me").status_code == 401
        finally:
            transaction.rollback()
        assert table_counts(connection) == before, "verification row counts changed"
    print("PostgreSQL registration/login/owner access/matching/idempotency/logout: PASS; verification rows rolled back")
    print("PostgreSQL personality/simulation/chat persistence: PASS (" + ("LIVE Gemini" if live_ai else "AI provider MOCKED") + ")")
    print("PostgreSQL public-data repeated synchronization: PASS (isolated verification record rolled back)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-ai", action="store_true")
    verify(parser.parse_args().live_ai)
