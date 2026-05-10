from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/surveys", tags=["surveys"])


class SurveyCreate(BaseModel):
    user_id: int
    housing_type: str
    walk_frequency: int
    alone_time: int
    experience_level: int
    activity_preference: int


surveys = []


@router.post("/")
def create_survey(survey: SurveyCreate):
    new_survey = survey.model_dump()
    new_survey["id"] = len(surveys) + 1
    surveys.append(new_survey)
    return new_survey


@router.get("/{user_id}")
def get_survey(user_id: int):
    for survey in surveys:
        if survey["user_id"] == user_id:
            return survey

    raise HTTPException(status_code=404, detail="Survey not found")