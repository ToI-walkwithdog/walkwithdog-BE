from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.routers.dogs import dogs
from app.routers.surveys import surveys

router = APIRouter(prefix="/matching", tags=["matching"])


class MatchingRequest(BaseModel):
    user_id: int


def calculate_score(dog, survey):
    score = 100

    # 사용자가 산책을 자주 못 하는데 강아지 활동성이 높으면 감점
    score -= abs(dog["activity_level"] - survey["activity_preference"]) * 10

    # 혼자 있는 시간이 긴데 강아지 불안도가 높으면 감점
    if survey["alone_time"] >= 6:
        score -= dog["anxiety_level"] * 5

    # 초보자인데 불안도가 높으면 감점
    if survey["experience_level"] <= 1:
        score -= dog["anxiety_level"] * 5

    return max(score, 0)


@router.post("/")
def match_dogs(request: MatchingRequest):
    user_survey = None

    for survey in surveys:
        if survey["user_id"] == request.user_id:
            user_survey = survey
            break

    if user_survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")

    results = []

    for dog in dogs:
        score = calculate_score(dog, user_survey)

        results.append({
            "dog_id": dog["id"],
            "dog_name": dog["name"],
            "breed": dog["breed"],
            "score": score,
            "reason": "사용자 라이프스타일과 강아지 성향을 비교해 계산한 점수입니다."
        })

    results.sort(key=lambda x: x["score"], reverse=True)

    return results