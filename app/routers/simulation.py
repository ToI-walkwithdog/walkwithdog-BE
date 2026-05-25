from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Survey, Dog

router = APIRouter(
    prefix="/simulation",
    tags=["simulation"]
)


@router.get("/timeline")
def get_simulation_timeline(
    user_id: int,
    dog_id: int,
    db: Session = Depends(get_db)
):
    survey = (
        db.query(Survey)
        .filter(Survey.user_id == user_id)
        .order_by(Survey.created_at.desc())
        .first()
    )

    if survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")

    dog = db.query(Dog).filter(Dog.id == dog_id).first()

    if dog is None:
        raise HTTPException(status_code=404, detail="Dog not found")

    daytime_warning = None
    evening_warning = None

    if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
        if survey.outing_frequency in ["주 3~4일 외출해요", "거의 매일 집을 비워요"]:
            daytime_warning = "혼자 있는 시간이 길어 분리불안이 나타날 수 있어요."

    if dog.energy_level is not None and dog.energy_level >= 4:
        if survey.walk_duration in ["20분 미만", "20~40분"]:
            evening_warning = "활동량이 높은 강아지라 추가 산책이나 놀이가 필요할 수 있어요."

    return {
        "user_id": user_id,
        "dog_id": dog_id,
        "dog_name": dog.name,
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