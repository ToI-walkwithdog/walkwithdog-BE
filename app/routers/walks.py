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
from app.walk_review_schemas import WalkReviewCreate, WalkReviewResponse
from app.walk_review_service import create_review, get_review

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
        if code == "23505" and name == "volunteer_profiles_pkey":
            raise HTTPException(409, "봉사자 프로필 생성이 다른 요청과 충돌했습니다. 다시 시도하세요.") from None
        if code == "23505" and name == "uq_walk_reviews_walk":
            raise HTTPException(409, "이미 평가한 산책입니다.") from None
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
    """기존 종료 권한·잠금·상태 전이를 유지하며 GPS V1 이동·정지·UNKNOWN 시간을 저장합니다.
    average_pace는 실제 이동 초 / 유효 이동 거리 × 1000(초/km)로 계산합니다.
    total_distance_m은 기존 거리이며 moving_distance_m은 정지 흔들림·불명확 구간을 제외합니다.
    GPS 부족 시 UNKNOWN이며 이동 근거가 없으면 페이스는 NULL입니다. 기존 미분석 기록의
    새 필드는 NULL이며 기존 average_pace는 전체 시간 기준 값으로 보존됩니다.
    """
    return persist(db, lambda: end_walk(db, walk_id, actor, payload), WalkResponse)


@router.get("/{walk_id}", response_model=WalkDetailResponse)
def detail(walk_id: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
           actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """기존 권한과 페이지 형식으로 조회합니다. Walk의 신규 시간·이동 거리 NULL은 레거시
    미분석 기록입니다. 새 분석의 average_pace는 전체 시간이 아닌 이동 시간 기준입니다.
    """
    return walk_detail(db, walk_id, actor, limit, offset)


@router.post("/{walk_id}/review", response_model=WalkReviewResponse, status_code=201)
def review_create(walk_id: int, payload: WalkReviewCreate,
                  actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    """COMPLETED 산책의 봉사자 본인만 평가를 한 번 등록합니다. 재전송도 409입니다.
    점수는 정수 1~5이며 최소 하나가 필요합니다. 미관찰 항목은 null로 보존합니다.
    1점/5점의 행동 의미는 미정이며 점수 보정·성향 계산은 수행하지 않습니다.
    """
    return persist(db, lambda: create_review(db, walk_id, actor, payload), WalkReviewResponse)


@router.get("/{walk_id}/review", response_model=WalkReviewResponse)
def review_get(walk_id: int, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """본인·신청 당시 담당 보호소·관리자만 저장된 평가 원본을 조회합니다.
    평가가 없으면 404이며 자동 생성하지 않습니다. GPS·신고 정보는 포함하지 않습니다.
    """
    return get_review(db, walk_id, actor)
