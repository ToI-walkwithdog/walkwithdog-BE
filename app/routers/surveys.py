from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Survey, User
from app.schemas import SurveyCreate, SurveyResponse

from app.dependencies import require_login
from app.models import User

router = APIRouter(
    prefix="/surveys",
    tags=["surveys"]
)

@router.post("/", response_model=SurveyResponse)
def create_survey(
    survey: SurveyCreate, 
    current_user: User = Depends(require_login), 
    db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == survey.user_id).first()

    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_survey = Survey(
        user_id=survey.user_id,
        housing_type=survey.housing_type,
        family_type=survey.family_type,
        outing_frequency=survey.outing_frequency,
        daily_walk_time=survey.daily_walk_time,
        life_pattern=survey.life_pattern,
        walk_frequency=survey.walk_frequency,
        walk_duration=survey.walk_duration,
        dog_experience=survey.dog_experience,
        mbti=survey.mbti
    )

    db.add(new_survey)
    db.commit()
    db.refresh(new_survey)

    return new_survey


@router.get("/{user_id}", response_model=list[SurveyResponse])
def get_surveys_by_user(user_id: int, db: Session = Depends(get_db)):
    return db.query(Survey).filter(Survey.user_id == user_id).all()