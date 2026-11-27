"""Onboarding Step 3 routes (Issue #44). Both routes: Bearer session (401), role=user and an
ACTIVE account (403 ONBOARDING_NOT_ALLOWED; this API also uses it for AGE_REVIEW_REQUIRED),
the current pledge, Step 1 and Step 2 (409 PREVIOUS_STEP_NOT_COMPLETED with the first
incomplete step), then the body. The save re-checks all of it on the locked users row."""
from typing import ClassVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.orm import Session

from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, as_field_error, ok
from app.auth import get_contract_user
from app.database import get_db
from app.models import User
from app.onboarding_common import get_clock
from app.privacy_policy import DOCUMENT_VERSION_MAX_LENGTH
from app.walking_routine_service import (DOG_SIZE, DOG_SIZE_REQUIRED, DURATION, DURATION_REQUIRED, READ_MESSAGE,
                                         REGIONS, REGIONS_REQUIRED, SAVED_MESSAGE, TIME_SLOTS, TIME_SLOTS_REQUIRED,
                                         ensure_routine_allowed, read_routine, save_routine, validate_routine)

router = APIRouter(prefix="/users/me", tags=["onboarding"], route_class=ContractRoute)

COMMON_ERRORS = {
    401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED"},
    403: {"model": ContractErrorResponse, "description": "ONBOARDING_NOT_ALLOWED(일반 사용자 외, AGE_REVIEW_REQUIRED 계정)"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
}
PREVIOUS_STEP = "PREVIOUS_STEP_NOT_COMPLETED(required_step=PLEDGE, BASIC_INFORMATION 또는 PET_EXPERIENCE)"


def get_routine_user(user: User = Depends(get_contract_user), db: Session = Depends(get_db)) -> User:
    ensure_routine_allowed(db, user)
    return user


class DocumentAgreement(BaseModel):
    model_config = ConfigDict(strict=True)

    document_version: str = Field(min_length=1, max_length=DOCUMENT_VERSION_MAX_LENGTH)
    agreed: bool

    @field_validator("document_version")
    @classmethod
    def nonblank_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A document version is required")
        return value


class RoutineAgreements(BaseModel):
    terms: DocumentAgreement
    privacy: DocumentAgreement


def validate_agreements(value: object) -> RoutineAgreements:
    try:
        agreements = RoutineAgreements.model_validate(value)
    except ValidationError as exc:
        location = exc.errors(include_input=False, include_url=False, include_context=False)[0]["loc"]
        field = ".".join(("agreements", *location))
        raise ContractError(422, "INVALID_AGREEMENTS_PAYLOAD", "필수 동의 정보를 확인해 주세요.",
                            {"field": field}) from None
    if not agreements.terms.agreed:
        raise ContractError(422, "TERMS_AGREEMENT_REQUIRED", "이용약관에 동의해 주세요.",
                            {"field": "agreements.terms.agreed"})
    if not agreements.privacy.agreed:
        raise ContractError(422, "PRIVACY_CONFIRMATION_REQUIRED", "개인정보 처리 관련 필수 항목을 확인해 주세요.",
                            {"field": "agreements.privacy.agreed"})
    return agreements


class WalkingRoutineRequest(BaseModel):
    """The whole body is checked at once so the 422 order follows Issue #44. Unknown fields
    (a selection source, coordinates, a user ID or a time) are ignored and never stored."""
    model_config = ConfigDict(extra="ignore")

    region_codes: list[str]
    available_time_slot_codes: list[str]
    walking_duration_code: str
    comfortable_dog_size_code: str
    agreements: RoutineAgreements

    # A missing or unreadable body never reaches the validator; ContractRoute uses this.
    contract_required_errors: ClassVar[dict] = {
        REGIONS: REGIONS_REQUIRED, TIME_SLOTS: TIME_SLOTS_REQUIRED,
        DURATION: DURATION_REQUIRED, DOG_SIZE: DOG_SIZE_REQUIRED}

    @model_validator(mode="before")
    @classmethod
    def check(cls, value):
        answer = as_field_error(validate_routine, value)
        answer["agreements"] = as_field_error(validate_agreements, value.get("agreements"))
        return answer


@router.get("/onboarding/walking-routine",
            responses={**COMMON_ERRORS, 409: {"model": ContractErrorResponse, "description": PREVIOUS_STEP}})
def get_walking_routine(user: User = Depends(get_routine_user), db: Session = Depends(get_db)):
    response = ok(read_routine(db, user), READ_MESSAGE)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.put("/onboarding/walking-routine",
            responses={**COMMON_ERRORS,
                       409: {"model": ContractErrorResponse,
                             "description": f"{PREVIOUS_STEP}, WALKING_ROUTINE_ALREADY_COMPLETED, POLICY_VERSION_MISMATCH"},
                       422: {"model": ContractErrorResponse,
                             "description": "REGIONS_REQUIRED, REGION_LIMIT_EXCEEDED, INVALID_REGION_CODE, "
                                            "TIME_SLOTS_REQUIRED, INVALID_TIME_SLOT, DURATION_REQUIRED, "
                                            "INVALID_DURATION, DOG_SIZE_REQUIRED, INVALID_DOG_SIZE, "
                                            "INVALID_AGREEMENTS_PAYLOAD, TERMS_AGREEMENT_REQUIRED, "
                                            "PRIVACY_CONFIRMATION_REQUIRED"},
                       503: {"model": ContractErrorResponse, "description": "POLICY_DOCUMENT_UNAVAILABLE"}})
def put_walking_routine(payload: WalkingRoutineRequest, user: User = Depends(get_routine_user),
                        db: Session = Depends(get_db), clock=Depends(get_clock)):
    data = save_routine(db, user.id, payload.model_dump(), clock())
    return ok(data, SAVED_MESSAGE)
