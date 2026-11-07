"""Review raw data only; no GPS, volunteer-statistic or trait-summary writes."""
from fastapi import HTTPException
from sqlalchemy.orm import joinedload

from app.dependencies import require_shelter_access
from app.models import UserRole, Walk, WalkRequest, WalkReview
from app.walk_request_service import lock_row, locked_request


def locked_review_walk(db, walk_id, actor):
    """Dog -> User -> WalkRequest -> Walk, without the recording IN_PROGRESS gate."""
    if actor.role != UserRole.USER:
        raise HTTPException(403, "봉사자 본인만 산책 평가를 등록할 수 있습니다.")
    hint = (db.query(Walk.walk_request_id, WalkRequest.user_id).join(WalkRequest)
            .filter(Walk.id == walk_id).first())
    if hint is None:
        raise HTTPException(404, "Walk not found")
    if actor.id != hint.user_id:
        raise HTTPException(403, "본인 산책만 평가할 수 있습니다.")
    # START authorizes the request owner and locks resources; it does not check
    # request/walk status. Do not use validate_walk_start() or locked_walk().
    request, _, volunteer = locked_request(db, hint.walk_request_id, actor, "START")
    if volunteer.role != UserRole.USER:
        raise HTTPException(403, "현재 봉사자 계정만 평가를 등록할 수 있습니다.")
    walk = lock_row(db, Walk, walk_id)
    if walk is None:
        raise HTTPException(404, "Walk not found")
    if walk.walk_request_id != request.id:
        raise HTTPException(409, "산책 신청 연결이 변경되었습니다. 다시 조회하세요.")
    if walk.status != "COMPLETED":
        raise HTTPException(409, "완료한 산책만 평가할 수 있습니다.")
    return walk


def create_review(db, walk_id, actor, payload):
    walk = locked_review_walk(db, walk_id, actor)
    if db.query(WalkReview.id).filter_by(walk_id=walk.id).first() is not None:
        raise HTTPException(409, "이미 평가한 산책입니다.")
    review = WalkReview(walk_id=walk.id, **payload.model_dump())
    db.add(review)
    db.flush()  # UNIQUE(walk_id) remains the final concurrency guard.
    return review


def get_review(db, walk_id, actor):
    walk = (db.query(Walk).options(joinedload(Walk.walk_request))
            .filter_by(id=walk_id).first())
    if walk is None:
        raise HTTPException(404, "Walk not found")
    request = walk.walk_request
    if actor.id != request.user_id:
        require_shelter_access(actor, request.shelter_id)
    review = db.query(WalkReview).filter_by(walk_id=walk.id).first()
    if review is None:
        raise HTTPException(404, "Walk review not found")
    return review
