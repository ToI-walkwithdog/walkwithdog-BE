from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.account_rules import AVAILABILITY_LOGIN_ID_FORMAT_MESSAGE, normalize_login_id, normalize_nickname
from app.api_contract import ContractErrorResponse, ContractRoute, ok
from app.database import get_db
from app.dependencies import require_admin
from app.auth import get_current_user, require_owner
from app.models import Shelter, User, UserRole
from app.rate_limit import RateLimiter
from app.schemas import (AvailabilityResponse, SignupRequest, SignupResponse, SignupUserData,
                         UserResponse, UserShelterUpdate)
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
                 responses={409: {"model": ContractErrorResponse, "description": "아이디, 닉네임 중복"},
                            **ERROR_RESPONSES})
def create_user(payload: SignupRequest, db: Session = Depends(get_db)):
    user = register_user(db, payload.login_id, payload.nickname, payload.password)
    data = SignupUserData.model_validate(user, from_attributes=True).model_dump(mode="json")
    return ok(data, "회원가입이 완료되었습니다.")


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


router.include_router(onboarding)


@router.get("/me", response_model=UserResponse)
def get_me(user=Depends(get_current_user)):
    return user


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
