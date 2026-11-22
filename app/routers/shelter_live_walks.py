from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_shelter
from app.models import User
from app.walk_live_schemas import (
    LiveWalkCardResponse,
    LiveWalkDetailResponse,
    LiveWalkUpdatesResponse,
)
from app.walk_live_service import (
    get_live_walk_detail,
    get_live_walk_updates,
    get_live_walks,
)

router = APIRouter(prefix="/shelters/me", tags=["shelter-live-walks"])


@router.get("/live-walks", response_model=list[LiveWalkCardResponse])
def list_live_walks(
    shelter_id: Optional[int] = Query(None, ge=1, description="관리자 전용 보호소 ID"),
    actor: User = Depends(require_shelter),
    db: Session = Depends(get_db),
):
    """현재 산책 중(IN_PROGRESS)인 강아지 카드 목록 조회."""
    return get_live_walks(db, actor, shelter_id=shelter_id)


@router.get("/live-walks/{walk_id}", response_model=LiveWalkDetailResponse)
def get_live_walk(
    walk_id: int,
    actor: User = Depends(require_shelter),
    db: Session = Depends(get_db),
):
    """산책 1건 상세 지도 정보 (현재 위치, 전체 경로, 행동마커, 산책자 전화번호)."""
    return get_live_walk_detail(db, walk_id=walk_id, actor=actor)


@router.get("/live-walks/{walk_id}/updates", response_model=LiveWalkUpdatesResponse)
def get_live_walk_incremental_updates(
    walk_id: int,
    after_seq: Optional[int] = Query(None, ge=0, description="이 sequence_no 이후의 경로 좌표만 반환"),
    actor: User = Depends(require_shelter),
    db: Session = Depends(get_db),
):
    """경로 및 마커 증분 조회 (폴링용)."""
    return get_live_walk_updates(db, walk_id=walk_id, after_seq=after_seq, actor=actor)
