"""Authenticated GPS sessions; raw locations and safety reports are never public."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.account_status import require_active_writer
from app.auth import get_current_user
from app.database import get_db
from app.dependencies import require_role
from app.models import User, UserRole
from app.onboarding_common import require_onboarding_for_new_walk
from app.walk_request_service import owns_date_request, start_conflict
from app.walk_map_schemas import WalkHistoryResponse
from app.walk_map_service import user_walk_history
from app.walk_record_schemas import (
    ActiveWalkResponse, PointsReceipt, WalkDetailResponse, WalkEnd, WalkHandoverCreate, WalkHandoverReceipt,
    WalkIncidentCreate, WalkIncidentResponse, WalkMarkerCreate, WalkMarkerResponse, WalkPointsBatch, WalkResponse,
    WalkStart,
)
from app.walk_service import (add_marker, append_points, close_stale_walks, end_walk, get_active_walk, report_incident,
                              save_handover, start_walk, walk_detail)

router = APIRouter(prefix="/walks", tags=["walks"])
require_volunteer = require_role(UserRole.USER)


def persist(db, operation, response_model, actor, *, start_request_id=None, account_nowait=True):
    try:
        date_start = start_request_id is not None and owns_date_request(db, start_request_id, actor)
        # Match the business-row NOWAIT contract at the earlier account lock.
        # The active-account recheck and transaction-scoped lock remain mandatory.
        require_active_writer(db, actor, for_update=date_start, nowait=account_nowait)
        result = response_model.model_validate(operation())
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise_db_error(exc, start_request_id)


def settle_expired(db, actor, *, start_request_id=None):
    """Issue #89: commit the actor's no-signal auto-timeouts in their own transaction first, so a
    later 404 or 409 of the same request cannot roll the settlement back."""
    try:
        close_stale_walks(db, user_id=actor.id)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise_db_error(exc, start_request_id)


def raise_db_error(exc, start_request_id=None):
    original = getattr(exc, "orig", None)
    code = getattr(original, "pgcode", None)
    name = getattr(getattr(original, "diag", None), "constraint_name", None)
    if start_request_id is not None:  # Issue #87: the start screen branches on detail.code.
        if code in ("55P03", "40P01", "40001"):
            raise start_conflict("WALK_START_RETRYABLE", "다른 작업과 충돌했습니다. 진행 중 산책을 확인한 뒤 다시 시도하세요.") from None
        if code == "23505" and name == "uq_walks_request":
            raise start_conflict("WALK_START_ALREADY_STARTED", "이미 산책이 시작된 예약입니다.") from None
    if code in ("55P03", "40P01", "40001"):
        raise HTTPException(409, "다른 작업과 충돌했습니다. 다시 조회한 뒤 재시도하세요.") from None
    if code == "23505" and name in ("uq_walks_request", "uq_walk_points_sequence"):
        raise HTTPException(409, "산책 또는 좌표 순번이 중복됩니다. 저장 상태를 다시 확인하세요.") from None
    if code == "23505" and name == "uq_walk_markers_client_marker_id":
        raise HTTPException(409, "같은 마커 요청이 동시에 처리되었습니다. 다시 시도해 저장 결과를 확인하세요.") from None
    if code == "23505" and name == "volunteer_profiles_pkey":
        raise HTTPException(409, "봉사자 프로필 생성이 다른 요청과 충돌했습니다. 다시 시도하세요.") from None
    raise HTTPException(500, "산책 기록을 저장하지 못했습니다.") from None


@router.post("", response_model=WalkResponse, status_code=201)
def begin(payload: WalkStart, actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    def operation():
        require_onboarding_for_new_walk(db, actor)
        return start_walk(db, actor, payload)

    settle_expired(db, actor, start_request_id=payload.walk_request_id)
    return persist(db, operation, WalkResponse, actor, start_request_id=payload.walk_request_id)


@router.post("/{walk_id}/points", response_model=PointsReceipt)
def points(walk_id: int, payload: WalkPointsBatch, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    settle_expired(db, actor)  # an expired walk is settled and committed, then the batch gets 409
    return persist(db, lambda: append_points(db, walk_id, actor, payload), PointsReceipt, actor)


@router.post("/{walk_id}/markers", response_model=WalkMarkerResponse, status_code=201)
def marker(walk_id: int, payload: WalkMarkerCreate, response: Response, actor: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """client_marker_id 재요청: 같은 본문은 200과 기존 마커, 다른 본문은 409. 위치가 없으면
    409 detail.code=MARKER_LOCATION_UNAVAILABLE."""
    def operation():
        stored, created = add_marker(db, walk_id, actor, payload)
        if not created:
            response.status_code = 200
        return stored

    settle_expired(db, actor)
    return persist(db, operation, WalkMarkerResponse, actor)


@router.post("/{walk_id}/incidents", response_model=WalkIncidentResponse, status_code=201)
def incident(walk_id: int, payload: WalkIncidentCreate, response: Response, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    def operation():
        incident_obj, is_created = report_incident(db, walk_id, actor, payload)
        if not is_created:
            response.status_code = 200
        return incident_obj

    return persist(db, operation, WalkIncidentResponse, actor)


@router.patch("/{walk_id}/end", response_model=WalkResponse)
def end(walk_id: int, payload: WalkEnd, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """기존 종료 권한·잠금·상태 전이를 유지하며 GPS V1 이동·정지·UNKNOWN 시간을 저장합니다.
    average_pace는 실제 이동 초 / 유효 이동 거리 × 1000(초/km)로 계산합니다.
    total_distance_m은 기존 거리이며 moving_distance_m은 정지 흔들림·불명확 구간을 제외합니다.
    GPS 부족 시 UNKNOWN이며 이동 근거가 없으면 페이스는 NULL입니다. 기존 미분석 기록의
    새 필드는 NULL이며 기존 average_pace는 전체 시간 기준 값으로 보존됩니다.
    """
    return persist(db, lambda: end_walk(db, walk_id, actor, payload), WalkResponse, actor)


@router.post(
    "/{walk_id}/handover", response_model=WalkHandoverReceipt, status_code=201,
    responses={200: {"model": WalkHandoverReceipt, "description": "동일 본문으로 접수된 기존 인계 기록"}},
)
def handover(walk_id: int, payload: WalkHandoverCreate, response: Response,
             actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    """종료된 본인 산책의 인계 자가 확인 또는 문제를 접수합니다."""
    def operation():
        stored, created = save_handover(db, walk_id, actor, payload)
        if not created:
            response.status_code = 200
        return stored

    # Issue #93: wait for the end transaction before evaluating the handover.
    return persist(db, operation, WalkHandoverReceipt, actor, account_nowait=False)


@router.get("/active", response_model=ActiveWalkResponse)
def active(actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    """현재 로그인한 봉사자의 진행 중인 산책을 복원 조회합니다. 무수신 자동 종료 정산은 먼저 커밋되므로
    404여도 AUTO_TIMEOUT 결과가 남습니다(#89). auto_end_at이 지나면 자동 종료됩니다."""
    settle_expired(db, actor)
    # Preserve the existing account-lock wait behavior for this read endpoint.
    return persist(db, lambda: get_active_walk(db, actor), ActiveWalkResponse, actor, account_nowait=False)


@router.get("/history", response_model=WalkHistoryResponse)
def history(
    status: Literal["COMPLETED", "INTERRUPTED"] | None = Query(None),
    end_reason: Literal["USER", "STAFF", "AUTO_TIMEOUT"] | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(require_volunteer),
    db: Session = Depends(get_db),
    handover_status: Literal["NOT_CONFIRMED"] | None = Query(None),
):
    """F-12 SC-09: 로그인한 봉사자의 종료된 산책 이력 목록을 시작 시각 최신순으로 조회합니다."""
    return user_walk_history(db, actor.id, status, end_reason, limit, offset, handover_status=handover_status)


@router.get("/{walk_id}", response_model=WalkDetailResponse)
def detail(walk_id: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
           actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """기존 권한과 페이지 형식으로 조회합니다. Walk의 신규 시간·이동 거리 NULL은 레거시
    미분석 기록입니다. 새 분석의 average_pace는 전체 시간이 아닌 이동 시간 기준입니다.
    """
    return walk_detail(db, walk_id, actor, limit, offset)
