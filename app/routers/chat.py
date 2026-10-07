from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import ChatSession, ChatMessage, Survey, Dog
from app.personality import personality_data
from app.survey_codes import SURVEY_FIELDS, normalize_survey_value, parse_walk_hours
from app.ai_chat import generate_reply, configuration, NOTICE, redact_personal_data

router = APIRouter(prefix="/chat", tags=["chat"])


class SessionRequest(BaseModel):
    dog_id: int = Field(gt=0)
    survey_id: int = Field(gt=0)
    consent_to_external_ai: Literal[True]


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("메시지가 필요합니다.")
        return value.strip()


def context_for(survey, dog):
    chart = personality_data(dog)
    lifestyle = {}
    for key in SURVEY_FIELDS:
        value = normalize_survey_value(key, getattr(survey, key))
        # Unknown legacy free text is not sent to the external provider.
        lifestyle[key] = value.value if hasattr(value, "value") else None
    try:
        lifestyle["daily_walk_time"] = parse_walk_hours(survey.daily_walk_time)
    except ValueError:
        lifestyle["daily_walk_time"] = None
    return {"dog": {"name": dog.name, "breed": dog.breed, "age": dog.age,
                    "axes": chart["axes"], "source": chart["source"], "estimated": chart["estimated"]},
            "lifestyle": lifestyle}


def owned_session(db, session_id, user_id, lock=False):
    query = db.query(ChatSession).filter(ChatSession.id == session_id, ChatSession.user_id == user_id)
    if lock:
        query = query.with_for_update()
    session = query.first()
    if session is None:
        raise HTTPException(404, "Chat session not found")
    return session


@router.post("/sessions")
def create_session(request: SessionRequest, user=Depends(get_current_user), db: Session = Depends(get_db)):
    survey = db.query(Survey).filter(Survey.id == request.survey_id, Survey.user_id == user.id).first()
    if survey is None:
        raise HTTPException(404, "Survey not found")
    dog = db.get(Dog, request.dog_id)
    if dog is None:
        raise HTTPException(404, "Dog not found")
    configuration()  # Don't create unusable sessions when provider configuration is absent.
    session = ChatSession(user_id=user.id, dog_id=dog.id, survey_id=survey.id,
                          context_snapshot=context_for(survey, dog), consent_to_external_ai=True)
    db.add(session)
    db.commit()
    db.refresh(session)
    return {"id": session.id, "dog_id": dog.id, "survey_id": survey.id, "notice": NOTICE}


@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    owned_session(db, session_id, user.id)
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.id).all()
    return {"session_id": session_id, "messages": [{"id": m.id, "role": m.role, "content": m.content} for m in messages], "notice": NOTICE}


@router.post("/sessions/{session_id}/messages")
def send_message(session_id: int, request: MessageRequest, user=Depends(get_current_user), db: Session = Depends(get_db)):
    session = owned_session(db, session_id, user.id, lock=True)
    if not session.consent_to_external_ai:
        raise HTTPException(403, "외부 AI 전송 동의가 필요합니다.")
    history = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.id).all()
    if len(history) >= 40:
        raise HTTPException(409, "대화는 세션당 20회까지 가능합니다. 새 세션을 시작하세요.")
    safe_message, pii_redacted = redact_personal_data(request.message)
    try:
        reply, model = generate_reply(session.context_snapshot, history, safe_message)
        db.add(ChatMessage(session_id=session_id, role="user", content=safe_message))
        response = ChatMessage(session_id=session_id, role="assistant", content=reply)
        db.add(response)
        db.commit()
        db.refresh(response)
    except Exception:
        db.rollback()
        raise
    return {"id": response.id, "session_id": session_id, "role": "assistant", "content": reply,
            "model": model, "pii_redacted": pii_redacted, "notice": NOTICE}
