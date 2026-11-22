from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.walk_review_schemas import (
    WalkReviewCreate,
    WalkReviewResponse,
    WalkSummaryResponse,
)
from app.walk_review_service import (
    get_walk_review,
    get_walk_summary,
    upsert_walk_review,
)

router = APIRouter(prefix="/walks", tags=["walk-reviews"])


@router.get("/{walk_id}/summary", response_model=WalkSummaryResponse)
def get_summary(
    walk_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """산책 완료 요약 화면용 데이터 (경로, 마커, 시간, 거리, 보호소 링크, 평가) 조회."""
    return get_walk_summary(db=db, walk_id=walk_id, actor=actor)


@router.put("/{walk_id}/review", response_model=WalkReviewResponse)
def put_review(
    walk_id: int,
    payload: WalkReviewCreate,
    response: Response,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """산책 평가 작성 및 수정 (없으면 201 생성, 있으면 200 수정)."""
    try:
        review_data, is_created = upsert_walk_review(db=db, walk_id=walk_id, actor=actor, payload=payload)
        response.status_code = 201 if is_created else 200
        db.commit()
        return review_data
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        original = getattr(exc, "orig", None)
        code = getattr(original, "pgcode", None)
        name = getattr(getattr(original, "diag", None), "constraint_name", None)
        if code in ("55P03", "40P01", "40001"):
            raise HTTPException(409, "다른 작업과 충돌했습니다. 다시 조회한 뒤 재시도하세요.") from None
        if code == "23505" and name == "uq_walk_reviews_walk":
            raise HTTPException(409, "해당 산책에 대한 평가가 이미 등록되었습니다.") from None
        raise HTTPException(500, "산책 평가를 저장하지 못했습니다.") from None


@router.get("/{walk_id}/review", response_model=WalkReviewResponse | None)
def get_review(
    walk_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """산책 평가 단건 조회 (미작성 시 200 + null 반환)."""
    return get_walk_review(db=db, walk_id=walk_id, actor=actor)
