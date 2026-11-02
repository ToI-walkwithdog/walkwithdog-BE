from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models import Survey, Dog, DogWalkSetting, MatchingResult
from app.schemas import MatchingDetailResponse, MatchingScoreRequest, MatchingScoreResponse
from app.matching_service import build_analysis, get_score_label
from app.auth import get_current_user, require_owner
from app.eligibility import recommendation_candidate

router = APIRouter(prefix="/matching", tags=["matching"])


def result_query(db):
    return db.query(MatchingResult).options(
        joinedload(MatchingResult.dog).joinedload(Dog.shelter),
        joinedload(MatchingResult.dog).joinedload(Dog.walk_setting).selectinload(DogWalkSetting.slots),
    )


def serialize_result(result):
    # Legacy rows have no recoverable survey identity. Never reinterpret them
    # using the latest survey or invent historical warnings/breakdowns.
    analysis = result.analysis_snapshot or {
        "label": get_score_label(result.score), "warnings": [],
        "care_points": [], "score_breakdown": {},
    }
    return {
        **analysis, "id": result.id, "user_id": result.user_id,
        "dog_id": result.dog_id, "survey_id": result.survey_id,
        "score": result.score, "reason": result.reason,
        "detail_analysis": result.detail_analysis, "dog": result.dog,
    }


def survey_results(db, survey_id):
    return result_query(db).filter(MatchingResult.survey_id == survey_id).order_by(
        MatchingResult.score.desc(), MatchingResult.dog_id.asc()
    ).all()


@router.post("/score", response_model=MatchingScoreResponse)
def get_matching_score(request: MatchingScoreRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_owner(request.user_id, current_user)
    survey = db.query(Survey).filter(Survey.user_id == request.user_id).order_by(
        Survey.created_at.desc(), Survey.id.desc()
    ).first()
    if survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")
    dog = db.query(Dog).filter(Dog.id == request.dog_id).first()
    if dog is None:
        raise HTTPException(status_code=404, detail="Dog not found")
    return build_analysis(survey, dog)


@router.post("/{survey_id}", response_model=list[MatchingDetailResponse])
def create_matching_results(survey_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    survey = db.query(Survey).filter(Survey.id == survey_id, Survey.user_id == current_user.id).first()
    if survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")
    existing = survey_results(db, survey_id)
    if existing:
        return [serialize_result(result) for result in existing]
    dogs = [dog for dog in db.query(Dog).options(joinedload(Dog.shelter)).all()
            if recommendation_candidate(dog)]
    if not dogs:
        raise HTTPException(status_code=404, detail="Dog data not found")
    candidates = [(dog, build_analysis(survey, dog)) for dog in dogs]
    candidates.sort(key=lambda item: (-item[1]["score"], item[0].id))
    for dog, analysis in candidates[:5]:
        db.add(MatchingResult(
            user_id=survey.user_id, survey_id=survey.id, dog_id=dog.id,
            score=analysis["score"], reason=analysis["reason"],
            detail_analysis=analysis["detail_analysis"], analysis_snapshot=analysis,
        ))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent identical request may have committed first.
        db.rollback()
        existing = survey_results(db, survey_id)
        if not existing:
            raise
        return [serialize_result(result) for result in existing]
    return [serialize_result(result) for result in survey_results(db, survey_id)]


@router.get("/history/{user_id}", response_model=list[MatchingDetailResponse])
def get_matching_history(user_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_owner(user_id, current_user)
    results = result_query(db).filter(MatchingResult.user_id == user_id).order_by(
        MatchingResult.created_at.desc(), MatchingResult.id.desc()
    ).all()
    return [serialize_result(result) for result in results]
