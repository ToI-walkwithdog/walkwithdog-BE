"""Onboarding Step 1 routes (Issue #40). Every route: Bearer session (401), role=user
(403 ONBOARDING_NOT_ALLOWED), ACTIVE account (403 AGE_REVIEW_REQUIRED), current pledge
(409 PREVIOUS_STEP_NOT_COMPLETED with required_step=PLEDGE), then its own checks.
Writes re-check the account status and pledge on the locked users row."""
from typing import ClassVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, as_field_error, ok
from app.basic_info_service import (normalize_full_name, parse_birth_date, read_basic_info,
                                    require_verification_id, save_basic_info)
from app.database import get_db
from app.models import User
from app.onboarding_common import get_clock, get_onboarding_user
from app.phone_verification_service import confirm_code, normalize_phone, parse_verification_id, send_verification
from app.sms_provider import get_sms_provider

router = APIRouter(prefix="/users/me", tags=["onboarding"], route_class=ContractRoute)

COMMON_ERRORS = {
    401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED"},
    403: {"model": ContractErrorResponse, "description": "ONBOARDING_NOT_ALLOWED(일반 사용자 외), AGE_REVIEW_REQUIRED"},
    409: {"model": ContractErrorResponse, "description": "PREVIOUS_STEP_NOT_COMPLETED(required_step=PLEDGE) 외 API별 코드"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
}
INVALID_VERIFICATION_PAYLOAD_MESSAGE = "인증번호를 확인해 주세요."


def _invalid_verification_payload(field):
    return ContractError(422, "INVALID_VERIFICATION_PAYLOAD", INVALID_VERIFICATION_PAYLOAD_MESSAGE, {"field": field})


class PhoneVerificationRequest(BaseModel):
    phone_number: str = Field(description="국내 010 휴대전화 번호. 숫자와 하이픈, 서버에서 +8210으로 정규화",
                              examples=["010-1234-5678"])
    contract_required_errors: ClassVar[dict] = {"phone_number": ("PHONE_NUMBER_REQUIRED", "전화번호를 입력해 주세요.")}

    @field_validator("phone_number", mode="before")
    @classmethod
    def check_phone(cls, value):
        return as_field_error(normalize_phone, value)


def _check_verification_id(value):
    if parse_verification_id(value) is None:
        raise _invalid_verification_payload("verification_id")
    return value


def _check_code(value):
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise _invalid_verification_payload("code")
    return value


class PhoneVerificationConfirmRequest(BaseModel):
    verification_id: str = Field(description="발송 응답의 verification_id(UUID)")
    code: str = Field(description="숫자 6자리 문자열, 앞자리 0 허용", examples=["023841"])
    contract_required_errors: ClassVar[dict] = {
        "verification_id": ("INVALID_VERIFICATION_PAYLOAD", INVALID_VERIFICATION_PAYLOAD_MESSAGE),
        "code": ("INVALID_VERIFICATION_PAYLOAD", INVALID_VERIFICATION_PAYLOAD_MESSAGE)}

    @field_validator("verification_id", mode="before")
    @classmethod
    def check_verification_id(cls, value):
        return as_field_error(_check_verification_id, value)

    @field_validator("code", mode="before")
    @classmethod
    def check_code(cls, value):
        return as_field_error(_check_code, value)


class BasicInfoRequest(BaseModel):
    full_name: str = Field(description="실명. NFC 정규화, 앞뒤 공백 제거, 2~10자, 내부 공백 유지", examples=["김민지"])
    birth_date: str = Field(description="YYYY-MM-DD. Asia/Seoul 기준 만 14~120세", examples=["2002-12-10"])
    phone_verification_id: str = Field(description="인증에 성공한 최신 미사용 요청의 verification_id")

    @field_validator("full_name", mode="before")
    @classmethod
    def check_full_name(cls, value):
        return as_field_error(normalize_full_name, value)

    @field_validator("birth_date", mode="before")
    @classmethod
    def check_birth_date(cls, value):
        return as_field_error(lambda v: parse_birth_date(v).isoformat(), value)

    @field_validator("phone_verification_id", mode="before")
    @classmethod
    def check_verification_id(cls, value):
        return as_field_error(require_verification_id, value)


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/onboarding/basic-info", responses=COMMON_ERRORS)
def get_basic_info(user: User = Depends(get_onboarding_user), db: Session = Depends(get_db),
                   clock=Depends(get_clock)):
    return _no_store(ok(read_basic_info(db, user.id, clock()), "기본 정보 조회가 완료되었습니다."))


@router.post("/phone-verifications",
             responses={**COMMON_ERRORS,
                        422: {"model": ContractErrorResponse, "description": "PHONE_NUMBER_REQUIRED, INVALID_PHONE_NUMBER"},
                        409: {"model": ContractErrorResponse,
                              "description": "PREVIOUS_STEP_NOT_COMPLETED, PHONE_ALREADY_REGISTERED"},
                        429: {"model": ContractErrorResponse,
                              "description": "PHONE_VERIFICATION_RATE_LIMITED(retry_after_seconds, Retry-After)"},
                        503: {"model": ContractErrorResponse,
                              "description": "SMS_DELIVERY_UNAVAILABLE. 공급자 미설정, 발송 실패, 결과 불명(UNKNOWN)"}})
def post_phone_verification(payload: PhoneVerificationRequest, user: User = Depends(get_onboarding_user),
                            db: Session = Depends(get_db), provider=Depends(get_sms_provider),
                            clock=Depends(get_clock)):
    data = send_verification(db, provider, user.id, payload.phone_number, clock())
    return ok(data, "인증번호를 발송했습니다.")


@router.post("/phone-verifications/confirm",
             responses={**COMMON_ERRORS,
                        404: {"model": ContractErrorResponse, "description": "VERIFICATION_NOT_FOUND(없음, 타인 소유)"},
                        409: {"model": ContractErrorResponse,
                              "description": "PREVIOUS_STEP_NOT_COMPLETED, VERIFICATION_NOT_SENT, VERIFICATION_ALREADY_CONSUMED"},
                        410: {"model": ContractErrorResponse, "description": "VERIFICATION_EXPIRED(만료, 재발송으로 대체)"},
                        422: {"model": ContractErrorResponse,
                              "description": "INVALID_VERIFICATION_PAYLOAD, VERIFICATION_CODE_INVALID"},
                        429: {"model": ContractErrorResponse, "description": "VERIFICATION_ATTEMPTS_EXCEEDED"}})
def post_phone_verification_confirm(payload: PhoneVerificationConfirmRequest,
                                    user: User = Depends(get_onboarding_user), db: Session = Depends(get_db),
                                    provider=Depends(get_sms_provider), clock=Depends(get_clock)):
    data = confirm_code(db, provider, user.id, parse_verification_id(payload.verification_id), payload.code, clock())
    return ok(data, "전화번호 인증이 완료되었습니다.")


@router.put("/onboarding/basic-info",
            responses={**COMMON_ERRORS,
                       404: {"model": ContractErrorResponse, "description": "VERIFICATION_NOT_FOUND"},
                       409: {"model": ContractErrorResponse,
                             "description": "PREVIOUS_STEP_NOT_COMPLETED, PHONE_VERIFICATION_REQUIRED, "
                                            "BASIC_INFO_ALREADY_COMPLETED, PHONE_ALREADY_REGISTERED"},
                       422: {"model": ContractErrorResponse,
                             "description": "REQUIRED_FIELD, INVALID_FULL_NAME, INVALID_BIRTH_DATE, AGE_BELOW_MINIMUM "
                                            "(계정을 AGE_REVIEW_REQUIRED로 전환 후 응답), AGE_ABOVE_MAXIMUM"}})
def put_basic_info(payload: BasicInfoRequest, user: User = Depends(get_onboarding_user),
                   db: Session = Depends(get_db), clock=Depends(get_clock)):
    data = save_basic_info(db, user.id, payload.full_name, parse_birth_date(payload.birth_date),
                           payload.phone_verification_id, clock())
    return _no_store(ok(data, "기본 정보 입력이 완료되었습니다."))
