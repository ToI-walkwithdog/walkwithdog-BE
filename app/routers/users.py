from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.account_rules import AVAILABILITY_LOGIN_ID_FORMAT_MESSAGE, normalize_login_id, normalize_nickname
from app.api_contract import ContractErrorResponse, ContractRoute, ok
from app.database import get_db
from app.dependencies import require_admin
from app.auth import get_contract_user, get_current_user, require_owner
from app.models import Shelter, User, UserRole
from app.rate_limit import RateLimiter
from app.schemas import (AvailabilityResponse, MeResponse, PledgeRequest, PledgeResponse, SignupRequest,
                         SignupResponse, UserResponse, UserShelterUpdate)
from app.account_status import ensure_active
from app.api_contract import ContractError
from app.onboarding_common import as_utc, onboarding_status
from app.pledge_service import PLEDGE_COMPLETED_MESSAGE, PLEDGE_NOT_ALLOWED_MESSAGE, acknowledge_pledge
from app.signup_service import is_taken, register_user

router = APIRouter(
    prefix="/users",
    tags=["users"]
)

# Onboarding routes answer with {code, data, message}; the rest keep FastAPI errors.
onboarding = APIRouter(route_class=ContractRoute)

# Per client IP. Defaults are an interim proposal, not a final operations policy;
# override with *_RATE_LIMIT_REQUESTS / *_RATE_LIMIT_WINDOW_SECONDS. Both availability
# checks share one budget, so alternating between them does not double it.
SIGNUP_RATE_LIMITER = RateLimiter.from_env("signup", "SIGNUP", 5, 600)
AVAILABILITY_RATE_LIMITER = RateLimiter.from_env("availability", "AVAILABILITY", 30, 60)

ERROR_RESPONSES = {
    422: {"model": ContractErrorResponse, "description": "입력 검증 실패"},
    429: {"model": ContractErrorResponse,
          "description": "TOO_MANY_REQUESTS. 클라이언트 IP별 요청 제한 초과. data.retry_after_seconds와 "
                         "Retry-After 헤더는 다음 요청이 허용될 때까지의 초"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
}


@onboarding.post("/", response_model=SignupResponse, dependencies=[Depends(SIGNUP_RATE_LIMITER)],
                 responses={409: {"model": ContractErrorResponse,
                                  "description": "LOGIN_ID_ALREADY_EXISTS, NICKNAME_ALREADY_EXISTS, POLICY_VERSION_MISMATCH"},
                            **ERROR_RESPONSES,
                            422: {"model": ContractErrorResponse,
                                  "description": "계정 입력 오류, INVALID_PRIVACY_PAYLOAD, "
                                                 "PRIVACY_CONFIRMATION_REQUIRED, AGE_CONFIRMATION_REQUIRED"}})
def create_user(payload: SignupRequest, db: Session = Depends(get_db)):
    data = register_user(db, payload.login_id, payload.nickname, payload.password, payload.privacy)
    return ok(data.model_dump(mode="json"), "회원가입이 완료되었습니다.")


# Fixed paths stay ahead of /{user_id}: onboarding is included before those routes.
@onboarding.get("/nickname-availability", response_model=AvailabilityResponse, responses=ERROR_RESPONSES,
                dependencies=[Depends(AVAILABILITY_RATE_LIMITER)])
def nickname_availability(nickname: str | None = Query(default=None, description="확인할 닉네임"),
                          db: Session = Depends(get_db)):
    value = normalize_nickname(nickname, required_code="NICKNAME_REQUIRED")
    available = not is_taken(db, "nickname", value)
    return ok({"available": available}, "사용 가능한 닉네임입니다." if available else "이미 사용 중인 닉네임입니다.")


@onboarding.get("/id-availability", response_model=AvailabilityResponse, responses=ERROR_RESPONSES,
                dependencies=[Depends(AVAILABILITY_RATE_LIMITER)])
def login_id_availability(login_id: str | None = Query(default=None, description="확인할 로그인 아이디"),
                          db: Session = Depends(get_db)):
    value = normalize_login_id(login_id, required_code="LOGIN_ID_REQUIRED",
                               format_message=AVAILABILITY_LOGIN_ID_FORMAT_MESSAGE)
    available = not is_taken(db, "login_id", value)
    return ok({"available": available}, "사용 가능한 아이디입니다." if available else "이미 사용 중인 아이디입니다.")


@onboarding.get("/me", response_model=MeResponse,
                responses={401: {"model": ContractErrorResponse,
                                 "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED, "
                                                "USER_NOT_FOUND. Cache-Control: no-store"},
                           500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"}})
def get_me(user: User = Depends(get_contract_user), db: Session = Depends(get_db)):
    # Read only: no DB write. The progress fields come from one snapshot (Issue #45). Without a
    # configured current pledge version this fails closed with 500, like the pledge API (#33).
    data = UserResponse.model_validate(user, from_attributes=True).model_dump(mode="json")
    data.update(onboarding_status(db, user))
    response = ok(data, "사용자 정보를 조회했습니다.")
    response.headers["Cache-Control"] = "no-store"
    return response


def get_pledge_user(user: User = Depends(get_contract_user)) -> User:
    """Bearer session (401), role (403 PLEDGE_NOT_ALLOWED), account status (403 AGE_REVIEW_REQUIRED).

    The status is checked again on the locked row inside acknowledge_pledge().
    """
    if user.role != UserRole.USER:
        raise ContractError(403, "PLEDGE_NOT_ALLOWED", PLEDGE_NOT_ALLOWED_MESSAGE)
    ensure_active(user)
    return user


@onboarding.put("/me/pledge", response_model=PledgeResponse,
                responses={401: {"model": ContractErrorResponse,
                                 "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED, "
                                                "USER_NOT_FOUND"},
                           403: {"model": ContractErrorResponse,
                                 "description": "PLEDGE_NOT_ALLOWED(보호소, 관리자), AGE_REVIEW_REQUIRED(연령 확인 필요 계정)"},
                           409: {"model": ContractErrorResponse, "description": "PLEDGE_VERSION_MISMATCH"},
                           422: {"model": ContractErrorResponse,
                                 "description": "PLEDGE_VERSION_REQUIRED, INVALID_PLEDGE_VERSION"},
                           500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"}})
def put_my_pledge(payload: PledgeRequest, user: User = Depends(get_pledge_user), db: Session = Depends(get_db)):
    # Same version again returns the first confirmation time with 200 (idempotent).
    record = acknowledge_pledge(db, user.id, payload.pledge_version)
    # Always UTC ISO 8601 (#45), like GET /users/me: PostgreSQL returns TIMESTAMPTZ in the session
    # time zone, so an aware value is converted, not passed through (Issue #34).
    data = {"pledge_completed": True, "pledge_version": record.pledge_version,
            "acknowledged_at": as_utc(record.acknowledged_at).isoformat()}
    return ok(data, PLEDGE_COMPLETED_MESSAGE)


router.include_router(onboarding)


@router.patch("/{user_id}/shelter", response_model=UserResponse)
def update_user_shelter(
    user_id: int,
    assignment: UserShelterUpdate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.role != UserRole.SHELTER:
        raise HTTPException(409, "보호소 계정에만 담당 보호소를 지정할 수 있습니다.")
    if assignment.shelter_id is not None and db.get(Shelter, assignment.shelter_id) is None:
        raise HTTPException(404, "Shelter not found")

    user.shelter_id = assignment.shelter_id
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int, user=Depends(get_current_user)):
    require_owner(user_id, user)
    return user
