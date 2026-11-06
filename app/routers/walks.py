"""Authenticated GPS sessions; raw locations and safety reports are never public."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.dependencies import require_role
from app.models import User, UserRole
from app.walk_record_schemas import (
    PointsReceipt, WalkDetailResponse, WalkEnd, WalkIncidentCreate, WalkIncidentResponse,
    WalkMarkerCreate, WalkMarkerResponse, WalkPointsBatch, WalkResponse, WalkStart,
)
from app.walk_service import add_marker, append_points, end_walk, report_incident, start_walk, walk_detail

router = APIRouter(prefix="/walks", tags=["walks"])
require_volunteer = require_role(UserRole.USER)


def persist(db, operation, response_model):
    try:
        result = response_model.model_validate(operation())
        db.commit()
        return result
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
        if code == "23505" and name in ("uq_walks_request", "uq_walk_points_sequence"):
            raise HTTPException(409, "산책 또는 좌표 순번이 중복됩니다. 저장 상태를 다시 확인하세요.") from None
        raise HTTPException(500, "산책 기록을 저장하지 못했습니다.") from None


@router.post("", response_model=WalkResponse, status_code=201)
def begin(payload: WalkStart, actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    return persist(db, lambda: start_walk(db, actor, payload), WalkResponse)


@router.post("/{walk_id}/points", response_model=PointsReceipt)
def points(walk_id: int, payload: WalkPointsBatch, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return persist(db, lambda: append_points(db, walk_id, actor, payload), PointsReceipt)


@router.post("/{walk_id}/markers", response_model=WalkMarkerResponse, status_code=201)
def marker(walk_id: int, payload: WalkMarkerCreate, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return persist(db, lambda: add_marker(db, walk_id, actor, payload), WalkMarkerResponse)


@router.post("/{walk_id}/incidents", response_model=WalkIncidentResponse, status_code=201)
def incident(walk_id: int, payload: WalkIncidentCreate, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return persist(db, lambda: report_incident(db, walk_id, actor, payload), WalkIncidentResponse)


@router.patch("/{walk_id}/end", response_model=WalkResponse)
def end(walk_id: int, payload: WalkEnd, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return persist(db, lambda: end_walk(db, walk_id, actor, payload), WalkResponse)


@router.get("/{walk_id}", response_model=WalkDetailResponse)
def detail(walk_id: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
           actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return walk_detail(db, walk_id, actor, limit, offset)
