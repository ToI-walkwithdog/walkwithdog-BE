from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Survey, Dog
from app.survey_codes import normalize_survey_value, OutingFrequency, WalkDuration
from app.auth import get_current_user, require_owner
from app.personality import personality_data
from app.survey_codes import parse_walk_hours

router = APIRouter(
    prefix="/simulation",
    tags=["simulation"]
)


@router.get("/timeline")
def get_simulation_timeline(
    user_id: int,
    dog_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    survey_id: int | None = Query(default=None, gt=0)
):
    require_owner(user_id, current_user)
    query = (
        db.query(Survey)
        .filter(Survey.user_id == user_id)
    )
    if survey_id is not None:
        query = query.filter(Survey.id == survey_id)
    survey = query.order_by(Survey.created_at.desc(), Survey.id.desc()).first()

    if survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")

    dog = db.query(Dog).filter(Dog.id == dog_id).first()

    if dog is None:
        raise HTTPException(status_code=404, detail="Dog not found")

    daytime_warning = None
    evening_warning = None

    if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
        if normalize_survey_value("outing_frequency", survey.outing_frequency) in [OutingFrequency.THREE_TO_FOUR_DAYS, OutingFrequency.DAILY]:
            daytime_warning = "혼자 있는 시간이 길어 분리불안이 나타날 수 있어요."

    if dog.energy_level is not None and dog.energy_level >= 4:
        if normalize_survey_value("walk_duration", survey.walk_duration) in [WalkDuration.UNDER_TWENTY, WalkDuration.TWENTY_TO_FORTY]:
            evening_warning = "활동량이 높은 강아지라 추가 산책이나 놀이가 필요할 수 있어요."

    try:
        walk_hours = parse_walk_hours(survey.daily_walk_time)
    except ValueError:
        walk_hours = None
    chart = personality_data(dog)
    result = {
        "user_id": user_id,
        "survey_id": survey.id,
        "dog_id": dog_id,
        "dog_name": dog.name,
        "is_simulation": True,
        "trait_source": chart["source"],
        "estimated": chart["estimated"],
        "missing_traits": chart["missing_fields"],
        "daily_walk_minutes": None if walk_hours is None else round(walk_hours * 60),
        "notice": "입양 전 생활을 생각해 보는 가상 시나리오이며 실제 행동·훈련 결과를 예측하거나 보장하지 않습니다.",
        "timeline": [
            {
                "time": "morning",
                "dog_state": "기대함",
                "event": "아침 인사와 짧은 산책",
                "message": f"{dog.name}가 하루를 시작하며 보호자와의 시간을 기다리고 있어요.",
                "warning": None
            },
            {
                "time": "daytime",
                "dog_state": "혼자 있음",
                "event": "사용자 외출 시간",
                "message": f"{dog.name}가 집에서 쉬거나 장난감을 가지고 시간을 보내요.",
                "warning": daytime_warning
            },
            {
                "time": "evening",
                "dog_state": "활발함",
                "event": "저녁 산책과 놀이",
                "message": f"{dog.name}가 보호자와 산책하고 교감하는 시간이 필요해요.",
                "warning": evening_warning
            },
            {
                "time": "night",
                "dog_state": "안정됨",
                "event": "휴식 시간",
                "message": f"{dog.name}가 하루 활동을 마치고 편안하게 쉬어요.",
                "warning": None
            }
        ]
    }
    actions = {
        "morning": [("short_walk", "짧게 산책하기"), ("rest", "쉬어가기")],
        "daytime": [("enrichment", "장난감과 환경 준비"), ("rest", "휴식 공간 준비")],
        "evening": [("play", "함께 놀기"), ("short_walk", "산책하기")],
        "night": [("rest", "편안하게 쉬기")],
    }
    for event in result["timeline"]:
        event["event_id"] = f"{survey.id}:{dog.id}:{event['time']}"
        event["animation"] = {"morning": "greet", "daytime": "idle", "evening": "walk", "night": "sleep"}[event["time"]]
        event["interaction_options"] = [{"code": code, "label": label} for code, label in actions[event["time"]]]
    return result


class InteractionRequest(BaseModel):
    survey_id: int = Field(gt=0)
    dog_id: int = Field(gt=0)
    time: Literal["morning", "daytime", "evening", "night"]
    action: Literal["short_walk", "enrichment", "play", "rest"]


@router.post("/interaction")
def preview_interaction(request: InteractionRequest, user=Depends(get_current_user), db: Session = Depends(get_db)):
    timeline = get_simulation_timeline(user.id, request.dog_id, db, user, request.survey_id)
    event = next(e for e in timeline["timeline"] if e["time"] == request.time)
    if request.action not in {a["code"] for a in event["interaction_options"]}:
        raise HTTPException(422, "해당 시간대에 제공하지 않는 행동입니다.")
    messages = {"short_walk": "안전한 장소에서 짧게 걷고 반응을 살펴보는 장면입니다. 시간과 강도는 보호소에 확인하세요.",
                "enrichment": "혼자 있는 시간을 준비하며 안전한 장난감과 환경을 살펴보는 장면입니다. 분리불안의 치료 효과를 보장하지 않습니다.",
                "play": "짧게 함께 놀며 반응을 살펴보는 장면입니다. 강아지가 쉬고 싶어 하면 멈춰주세요.",
                "rest": "조용하고 안전한 휴식 공간을 준비하는 장면입니다."}
    return {"survey_id": request.survey_id, "dog_id": request.dog_id, "event_id": event["event_id"],
            "action": request.action, "animation": {"short_walk": "walk", "enrichment": "sniff", "play": "play", "rest": "idle"}[request.action],
            "message": messages[request.action], "warning": event["warning"], "is_simulation": True, "notice": timeline["notice"]}
