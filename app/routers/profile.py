"""Owner profile on /10-2 (Issue #103). Responses use {code, data, message} and are never cached.

Registered before app.routers.walk_maps so /users/me/profile is not taken by
GET /users/{user_id}/profile. Every route: Bearer session (401), then role=user and
ACTIVE (403); no pledge or full onboarding is required.
"""
from fastapi import APIRouter, Body, Depends
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, ok
from app.auth import get_contract_user
from app.database import get_db
from app.models import User
from app.onboarding_common import get_clock
from app.phone_verification_service import (PURPOSE_PROFILE, confirm_code, parse_verification_id,
                                            send_verification)
from app.profile_service import invalid_payload, read_profile, require_profile_user, update_profile
from app.routers.onboarding_basic import PhoneVerificationConfirmRequest, PhoneVerificationRequest
from app.sms_provider import get_sms_provider


class ProfileRoute(ContractRoute):
    def validation_error(self, exc: RequestValidationError) -> ContractError:
        if "PATCH" in self.methods:  # PATCH reads a plain object; any unreadable body is one code
            return invalid_payload()
        return super().validation_error(exc)


router = APIRouter(prefix="/users/me/profile", tags=["profile"], route_class=ProfileRoute)

NO_STORE = "private, no-store"
COMMON_ERRORS = {
    401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED"},
    403: {"model": ContractErrorResponse, "description": "PROFILE_UPDATE_NOT_ALLOWED(일반 사용자 외), AGE_REVIEW_REQUIRED"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
}
PATCH_BODY = Body(None, examples=[{"expected_profile_version": 3, "nickname": "산책왕", "full_name": "김민지",
                                   "phone_number": "010-1234-5678",
                                   "phone_verification_id": "8f2a0c4e-1b5d-4e7a-9c3f-2d6b8a1e0f47"}],
                  description="expected_profile_version 필수. 보내지 않은 필드는 변경 없음, 빈값과 null은 422. "
                              "번호가 바뀌면 프로필 목적으로 인증한 phone_verification_id가 필요")


def get_profile_user(user: User = Depends(get_contract_user)) -> User:
    """Role and status (403) as a dependency, so they are answered before body validation (422)."""
    require_profile_user(user)
    return user


def no_store(response):
    response.headers["Cache-Control"] = NO_STORE
    return response


@router.get("", responses=COMMON_ERRORS)
def get_profile(user: User = Depends(get_profile_user), db: Session = Depends(get_db)):
    """닉네임, 이름, 인증된 휴대폰 번호(국내 표기 원문), 인증 시각, profile_version. Step 1 전이면 이름과 번호는 null."""
    return no_store(ok(read_profile(db, user), "내 정보를 조회했습니다."))


@router.patch("", responses={**COMMON_ERRORS,
                             404: {"model": ContractErrorResponse, "description": "VERIFICATION_NOT_FOUND"},
                             409: {"model": ContractErrorResponse,
                                   "description": "PROFILE_VERSION_MISMATCH, NICKNAME_ALREADY_EXISTS, "
                                                  "PHONE_VERIFICATION_REQUIRED, PHONE_ALREADY_REGISTERED, "
                                                  "PROFILE_BASIC_INFO_REQUIRED"},
                             422: {"model": ContractErrorResponse,
                                   "description": "INVALID_PROFILE_PAYLOAD, NICKNAME_REQUIRED, INVALID_NICKNAME_LENGTH, "
                                                  "INVALID_NICKNAME_FORMAT, REQUIRED_FIELD, INVALID_FULL_NAME, "
                                                  "PHONE_NUMBER_REQUIRED, INVALID_PHONE_NUMBER"}})
def patch_profile(payload: dict | None = PATCH_BODY, user: User = Depends(get_profile_user),
                  db: Session = Depends(get_db), clock=Depends(get_clock)):
    return no_store(ok(update_profile(db, user.id, payload, clock()), "내 정보가 저장되었습니다."))


@router.post("/phone-verifications",
             responses={**COMMON_ERRORS,
                        409: {"model": ContractErrorResponse,
                              "description": "PROFILE_BASIC_INFO_REQUIRED, PHONE_ALREADY_REGISTERED"},
                        422: {"model": ContractErrorResponse, "description": "PHONE_NUMBER_REQUIRED, INVALID_PHONE_NUMBER"},
                        429: {"model": ContractErrorResponse,
                              "description": "PHONE_VERIFICATION_RATE_LIMITED(온보딩 인증과 합산)"},
                        503: {"model": ContractErrorResponse, "description": "SMS_DELIVERY_UNAVAILABLE"}})
def post_profile_phone_verification(payload: PhoneVerificationRequest, user: User = Depends(get_profile_user),
                                    db: Session = Depends(get_db), provider=Depends(get_sms_provider),
                                    clock=Depends(get_clock)):
    """새 번호 확인용 인증번호 발송. 확인만으로 번호는 바뀌지 않으며, PATCH 저장에서만 바뀐다."""
    data = send_verification(db, provider, user.id, payload.phone_number, clock(), purpose=PURPOSE_PROFILE)
    return no_store(ok(data, "인증번호를 발송했습니다."))


@router.post("/phone-verifications/confirm",
             responses={**COMMON_ERRORS,
                        404: {"model": ContractErrorResponse, "description": "VERIFICATION_NOT_FOUND(없음, 타인, 다른 목적)"},
                        409: {"model": ContractErrorResponse,
                              "description": "VERIFICATION_NOT_SENT, VERIFICATION_ALREADY_CONSUMED"},
                        410: {"model": ContractErrorResponse, "description": "VERIFICATION_EXPIRED"},
                        422: {"model": ContractErrorResponse,
                              "description": "INVALID_VERIFICATION_PAYLOAD, VERIFICATION_CODE_INVALID"},
                        429: {"model": ContractErrorResponse, "description": "VERIFICATION_ATTEMPTS_EXCEEDED"}})
def post_profile_phone_verification_confirm(payload: PhoneVerificationConfirmRequest,
                                            user: User = Depends(get_profile_user), db: Session = Depends(get_db),
                                            provider=Depends(get_sms_provider), clock=Depends(get_clock)):
    data = confirm_code(db, provider, user.id, parse_verification_id(payload.verification_id), payload.code, clock(),
                        purpose=PURPOSE_PROFILE)
    return no_store(ok(data, "전화번호 인증이 완료되었습니다."))
