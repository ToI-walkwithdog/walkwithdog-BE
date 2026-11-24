"""Walk application, review, historical shelter inbox, and read-only volunteer profile."""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import walk_request_service
from app.account_status import age_review_required, ensure_active, require_active_writer
from app.api_contract import ContractError, INTERNAL_SERVER_ERROR_MESSAGE, contract_response
from app.auth import (bearer, contract_unauthorized, get_contract_session, get_contract_user,
                      get_current_session, get_current_user)
from app.database import get_db
from app.dependencies import require_role, require_shelter, require_shelter_access
from app.models import Shelter, User, UserRole, VolunteerProfile, WalkRequest
from app.onboarding_common import require_onboarding_for_new_walk
from app.walk_request_schemas import (
    DateWalkRequestResponse, ManagedWalkRequestResponse, RequestStatus, ReservationStatusGroup,
    ReservationTab, VolunteerProfileResponse,
    WalkRequestAction, WalkRequestCreate, WalkRequestDetailResponse, WalkRequestResponse,
)
from app.shelter_volunteer_review_service import get_volunteer_review_summary
from app.walk_date_slots import slot_unavailable
from app.walk_request_service import (can_cancel_request, create_date_request, create_request, date_request_fingerprint,
                                     date_request_replay, get_request_detail, owns_date_request, request_list_query,
                                     transition_request)

router = APIRouter(tags=["walk-requests"])
require_volunteer = require_role(UserRole.USER)
logger = logging.getLogger(__name__)


def database_error(exc, *, cancelling=False):
    original = getattr(exc, "orig", None)
    code = getattr(original, "pgcode", None)
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    if code in ("55P03", "40P01", "40001"):
        message = "다른 작업과 충돌했습니다. 다시 조회한 뒤 재시도하세요."
        return HTTPException(409, {"code": "REQUEST_LOCK_CONFLICT", "message": message} if cancelling else message)
    if code == "23505" and constraint == "uq_walk_requests_active":
        return HTTPException(409, "활성 신청이 중복됩니다.")
    if code == "23505" and constraint == "volunteer_profiles_pkey":
        return HTTPException(409, "봉사자 프로필 생성이 다른 요청과 충돌했습니다. 다시 시도하세요.")
    if code == "23P01" and constraint in ("ex_walk_requests_dog_booking", "ex_walk_requests_user_booking"):
        subject = "강아지" if constraint == "ex_walk_requests_dog_booking" else "봉사자"
        return HTTPException(409, f"{subject}의 승인 예약이 DB 제약과 충돌합니다.")
    return HTTPException(500, "산책 신청을 저장하지 못했습니다.")


def persist_request(db, operation, actor, *, cancel_request_id=None, response_model=WalkRequestResponse):
    try:
        date_cancel = cancel_request_id is not None and owns_date_request(db, cancel_request_id, actor)
        require_active_writer(db, actor, for_update=date_cancel, nowait=date_cancel)
        request = operation()
        can_cancel = can_cancel_request(request, actor, walk_request_service.utc_now())
        response = response_model.model_validate(request, context={"can_cancel": can_cancel})
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise database_error(exc, cancelling=cancel_request_id is not None) from None


def invalid_date_request():
    return ContractError(422, "INVALID_WALK_REQUEST", "산책 신청 내용을 확인해 주세요.")


class BookingRoute(APIRoute):
    """Only the date-slot version of the existing URL uses the booking envelope."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def dispatch(request):
            is_date_booking = "idempotency-key" in request.headers
            media_type = request.headers.get("content-type", "application/json").split(";", 1)[0].strip().lower()
            if media_type == "application/json" or media_type.endswith("+json"):
                try:
                    body = await request.json()
                except (ValueError, RecursionError):
                    pass
                else:
                    if isinstance(body, dict):
                        is_date_booking = "date_slot_id" in body
            request.state.date_booking = is_date_booking
            if not is_date_booking:
                return await handler(request)
            try:
                return await handler(request)
            except ContractError as exc:
                return exc.response()
            except RequestValidationError:
                return invalid_date_request().response()
            except StarletteHTTPException as exc:
                if exc.status_code == 401:
                    return contract_unauthorized("AUTHENTICATION_REQUIRED").response()
                if exc.status_code == 403:
                    return age_review_required().response()
                if exc.status_code == 400:
                    return invalid_date_request().response()
                raise
            except Exception as exc:
                logger.error("Date-slot booking failed: %s", type(exc).__name__)
                return contract_response(500, "INTERNAL_SERVER_ERROR", INTERNAL_SERVER_ERROR_MESSAGE)

        return dispatch


def booking_actor(request: Request, credentials=Depends(bearer), db: Session = Depends(get_db)):
    if not request.state.date_booking:
        return require_volunteer(get_current_user(get_current_session(credentials, db), db))
    actor = get_contract_user(get_contract_session(credentials, db), db)
    if actor.role != UserRole.USER:
        raise ContractError(403, "WALK_REQUEST_NOT_ALLOWED", "산책을 신청할 권한이 없습니다.")
    ensure_active(actor)
    return actor


def date_receipt(data):
    return contract_response(201, "WALK_REQUEST_CREATED", "산책 신청이 접수됐어요.", data)


def persist_date_request(db, actor, payload, header):
    if payload.date_slot_id is None or not header:
        raise invalid_date_request()
    try:
        key = UUID(header)
    except ValueError:
        raise invalid_date_request() from None
    if str(key) != header.lower():
        raise invalid_date_request()
    fingerprint = date_request_fingerprint(payload)
    actor_id = actor.id
    try:
        require_active_writer(db, actor, for_update=True, nowait=True)
        if actor.role != UserRole.USER:
            raise ContractError(403, "WALK_REQUEST_NOT_ALLOWED", "산책을 신청할 권한이 없습니다.")
        data = create_date_request(db, actor, payload, key, fingerprint)
        db.commit()
        return date_receipt(data)
    except (HTTPException, ContractError):
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        original = getattr(exc, "orig", None)
        code = getattr(original, "pgcode", None)
        constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
        if code in ("55P03", "40P01", "40001") or constraint == "uq_walk_requests_user_idempotency":
            replay = date_request_replay(db, actor_id, key, fingerprint)
            if replay is not None:
                return date_receipt(replay)
            raise ContractError(409, "WALK_REQUEST_RETRYABLE", "같은 내용으로 다시 시도해 주세요.") from None
        if constraint == "uq_walk_requests_occupied_date_slot":
            raise slot_unavailable() from None
        if constraint == "uq_walk_requests_active" or code == "23P01":
            raise ContractError(409, "WALK_REQUEST_CONFLICT", "기존 산책 신청 또는 예약과 충돌합니다.") from None
        raise


booking_router = APIRouter(route_class=BookingRoute)


@booking_router.post("/walk-requests", response_model=WalkRequestResponse | DateWalkRequestResponse, status_code=201,
    responses={
        401: {"description": "인증 실패. 날짜 신청은 AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN 또는 SESSION_EXPIRED."},
        403: {"description": "역할, 계정 상태 또는 온보딩 제한."},
        404: {"description": "강아지 없음 또는 WALK_DOG_NOT_AVAILABLE."},
        409: {"description": "날짜 신청은 WALK_SLOT_CHANGED, WALK_SLOT_UNAVAILABLE, WALK_REQUEST_RETRYABLE, "
                             "WALK_REQUEST_CONFLICT 또는 IDEMPOTENCY_KEY_REUSED."},
        422: {"description": "기존 요청의 FastAPI 입력 오류 또는 날짜 신청의 INVALID_WALK_REQUEST."},
        500: {"description": "서버 오류 또는 INTERNAL_SERVER_ERROR."},
    })
def apply_for_walk(payload: WalkRequestCreate, request: Request, actor: User = Depends(booking_actor),
                   db: Session = Depends(get_db), request_key: str | None = Header(None, alias="Idempotency-Key")):
    if request.state.date_booking:
        return persist_date_request(db, actor, payload, request_key)

    def operation():
        require_onboarding_for_new_walk(db, actor)
        return create_request(db, actor, payload)

    return persist_request(db, operation, actor)


router.include_router(booking_router)


@router.patch("/walk-requests/{request_id}", response_model=ManagedWalkRequestResponse)
def review_or_cancel(request_id: int, payload: WalkRequestAction,
                     actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return persist_request(db, lambda: transition_request(db, request_id, actor, payload), actor,
                           cancel_request_id=request_id if payload.action == "CANCEL" else None,
                           response_model=ManagedWalkRequestResponse)


@router.get("/walk-requests/my", response_model=list[ManagedWalkRequestResponse])
def my_requests(
    status: RequestStatus | None = None,
    tab: ReservationTab | None = Query(None, description="마이페이지 탭 (ALL/PENDING/APPROVED/REJECTED/COMPLETED)"),
    status_group: ReservationStatusGroup | None = None,
    startable_only: bool = False,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """F-13 SC-10: 마이페이지 내 예약 내역 및 보호소 승인 상태 조회."""
    if sum(value is not None for value in (status, tab, status_group)) > 1:
        raise HTTPException(422, {"code": "RESERVATION_FILTER_CONFLICT",
                                  "message": "status, tab, status_group 중 하나만 지정할 수 있습니다."})
    now = walk_request_service.utc_now()
    requests = (
        request_list_query(db, status=status, tab=tab, status_group=status_group,
                           startable_at=now if startable_only else None)
        .filter(WalkRequest.user_id == actor.id)
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [ManagedWalkRequestResponse.model_validate(row, context={"can_cancel": can_cancel_request(row, actor, now)})
            for row in requests]


@router.get("/walk-requests/{request_id}", response_model=WalkRequestDetailResponse)
def get_walk_request(request_id: int, actor: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    return get_request_detail(db, request_id, actor)


@router.get("/shelters/{shelter_id}/walk-requests", response_model=list[WalkRequestResponse])
def shelter_requests(shelter_id: int, status: RequestStatus | None = None,
                     dog_id: int | None = Query(None, gt=0), limit: int = Query(20, ge=1, le=100),
                     offset: int = Query(0, ge=0), actor: User = Depends(require_shelter),
                     db: Session = Depends(get_db)):
    require_shelter_access(actor, shelter_id)
    if db.get(Shelter, shelter_id) is None:
        raise HTTPException(404, "Shelter not found")
    query = request_list_query(db, tab=status).filter(WalkRequest.shelter_id == shelter_id)
    if dog_id is not None:
        query = query.filter(WalkRequest.dog_id == dog_id)
    now = walk_request_service.utc_now()
    return [WalkRequestResponse.model_validate(row, context={"can_cancel": can_cancel_request(row, actor, now)})
            for row in query.limit(limit).offset(offset).all()]


@router.get("/volunteers/me", response_model=VolunteerProfileResponse, tags=["volunteers"])
def my_volunteer_profile(actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    profile = db.get(VolunteerProfile, actor.id)
    summary = get_volunteer_review_summary(db, actor.id)
    if profile is None:
        return VolunteerProfileResponse(user_id=actor.id, review_summary=summary)
    return VolunteerProfileResponse(
        user_id=profile.user_id,
        level=profile.level,
        total_walk_count=profile.total_walk_count,
        total_distance_m=profile.total_distance_m,
        total_duration_seconds=profile.total_duration_seconds,
        unique_dog_count=profile.unique_dog_count,
        review_summary=summary,
    )
