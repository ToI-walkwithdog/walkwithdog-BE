from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Survey, User
from app.schemas import SurveyCreate, SurveyResponse
from app.survey_codes import SURVEY_FIELDS, MBTI
from app.auth import get_current_user, require_owner

router = APIRouter(
    prefix="/surveys",
    tags=["surveys"]
)

@router.get("/options")
def get_survey_options():
    return {
        **{field: [{"code": member.value, "label": member.label} for member in enum]
           for field, enum in SURVEY_FIELDS.items()},
        "mbti": [{"code": member.value, "label": member.value} for member in MBTI],
    }

@router.post("/", response_model=SurveyResponse)
def create_survey(survey: SurveyCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_owner(survey.user_id, current_user)
    user = db.query(User).filter(User.id == survey.user_id).first()

    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_survey = Survey(
        user_id=survey.user_id,
        **{
            key: (None if value is None else str(value))
            for key, value in survey.model_dump(mode="json", exclude={"user_id"}).items()
        },
    )

    db.add(new_survey)
    db.commit()
    db.refresh(new_survey)

    return new_survey


@router.get("/{user_id}", response_model=list[SurveyResponse])
def get_surveys_by_user(user_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_owner(user_id, current_user)
    return db.query(Survey).filter(Survey.user_id == user_id).all()
