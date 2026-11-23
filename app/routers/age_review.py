"""Accounts in AGE_REVIEW_REQUIRED (Issue #41).

Both routes check: Bearer session (401), role=user (403), account status (409 when not
AGE_REVIEW_REQUIRED), then the body. The services lock the users row and re-check.
"""
from typing import ClassVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.account_status import ACCOUNT_AGE_REVIEW_REQUIRED
from app.age_review_service import (correct_birth_date, correction_not_required, delete_account, exit_not_required,
                                    password_confirmed)
from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, as_field_error, ok
from app.auth import get_contract_user
from app.basic_info_service import parse_birth_date
from app.database import get_db
from app.models import User, UserRole
from app.onboarding_common import get_clock
from app.rate_limit import FailureLimiter

router = APIRouter(prefix="/users/me", tags=["onboarding"], route_class=ContractRoute)

# Wrong passwords per account. 5 in 15 minutes is an interim default: the count, the
# period and a shared store across workers are not decided (Issue #41). Override with
# ACCOUNT_EXIT_PASSWORD_FAILURE_LIMIT / ACCOUNT_EXIT_PASSWORD_FAILURE_WINDOW_SECONDS.
PASSWORD_FAILURES = FailureLimiter.from_env("ACCOUNT_EXIT_PASSWORD", 5, 900)

UNAUTHORIZED = {"model": ContractErrorResponse,
                "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED, USER_NOT_FOUND"}
SERVER_ERROR = {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"}


def _birth_date_rule(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(422, "BIRTH_DATE_REQUIRED", "생년월일을 입력해 주세요.", {"field": "birth_date"})
    return parse_birth_date(value).isoformat()


class BirthDateCorrectionRequest(BaseModel):
    birth_date: str = Field(description="YYYY-MM-DD. Asia/Seoul 기준 만 14~120세. 저장하지 않음", examples=["2006-05-17"])
    contract_required_errors: ClassVar[dict] = {
        "birth_date": ("BIRTH_DATE_REQUIRED", "생년월일을 입력해 주세요.")}

    @field_validator("birth_date", mode="before")
    @classmethod
    def check_birth_date(cls, value):
        return as_field_error(_birth_date_rule, value)


def _password_rule(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(422, "PASSWORD_REQUIRED", "비밀번호를 입력해 주세요.", {"field": "password"})
    return value


class AccountExitRequest(BaseModel):
    password: str = Field(description="현재 비밀번호 재확인")
    contract_required_errors: ClassVar[dict] = {"password": ("PASSWORD_REQUIRED", "비밀번호를 입력해 주세요.")}

    @field_validator("password", mode="before")
    @classmethod
    def check_password(cls, value):
        return as_field_error(_password_rule, value)


def get_correction_user(user: User = Depends(get_contract_user)) -> User:
    if user.role != UserRole.USER:
        raise ContractError(403, "AGE_REVIEW_NOT_ALLOWED", "연령 확인 정보를 수정할 수 없습니다.")
    if user.account_status != ACCOUNT_AGE_REVIEW_REQUIRED:
        raise correction_not_required()
    return user


def get_exit_user(user: User = Depends(get_contract_user)) -> User:
    if user.role != UserRole.USER:
        raise ContractError(403, "ACCOUNT_DELETION_NOT_ALLOWED", "이 계정은 해당 탈퇴 절차를 이용할 수 없습니다.")
    if user.account_status != ACCOUNT_AGE_REVIEW_REQUIRED:
        raise exit_not_required()
    return user


@router.put("/age-review/birth-date",
            responses={401: UNAUTHORIZED,
                       403: {"model": ContractErrorResponse, "description": "AGE_REVIEW_NOT_ALLOWED(일반 사용자 아님)"},
                       409: {"model": ContractErrorResponse,
                             "description": "AGE_REVIEW_NOT_REQUIRED(연령 확인 필요 상태가 아님, 정정 후 재요청 포함)"},
                       422: {"model": ContractErrorResponse,
                             "description": "BIRTH_DATE_REQUIRED, INVALID_BIRTH_DATE, AGE_BELOW_MINIMUM, AGE_ABOVE_MAXIMUM"},
                       500: SERVER_ERROR})
def put_birth_date_correction(payload: BirthDateCorrectionRequest, user: User = Depends(get_correction_user),
                              db: Session = Depends(get_db), clock=Depends(get_clock)):
    data = correct_birth_date(db, user.id, parse_birth_date(payload.birth_date), clock())
    return ok(data, "연령 확인 정보를 정정했습니다.")


@router.delete("",
               responses={401: UNAUTHORIZED,
                          403: {"model": ContractErrorResponse,
                                "description": "ACCOUNT_DELETION_NOT_ALLOWED(일반 사용자 아님), PASSWORD_CONFIRMATION_FAILED"},
                          409: {"model": ContractErrorResponse,
                                "description": "AGE_REVIEW_NOT_REQUIRED, ACCOUNT_HAS_RETAINED_ACTIVITY(보존 대상 기록 또는 "
                                               "보존 정책 미확정 기록 존재)"},
                          422: {"model": ContractErrorResponse, "description": "PASSWORD_REQUIRED"},
                          429: {"model": ContractErrorResponse, "description": "TOO_MANY_PASSWORD_ATTEMPTS"},
                          500: SERVER_ERROR})
def delete_me(payload: AccountExitRequest, user: User = Depends(get_exit_user), db: Session = Depends(get_db)):
    key = str(user.id)
    wait = PASSWORD_FAILURES.blocked_for(key)
    if wait:
        raise ContractError(429, "TOO_MANY_PASSWORD_ATTEMPTS", "잠시 후 다시 시도해 주세요.",
                            headers={"Retry-After": str(wait)})
    if not password_confirmed(user, payload.password):
        PASSWORD_FAILURES.record_failure(key)
        raise ContractError(403, "PASSWORD_CONFIRMATION_FAILED", "비밀번호를 다시 확인해 주세요.", {"field": "password"})
    PASSWORD_FAILURES.clear(key)
    delete_account(db, user.id)
    response = ok({"deleted": True}, "가입이 종료되었습니다.")
    response.headers["Cache-Control"] = "no-store"
    return response
