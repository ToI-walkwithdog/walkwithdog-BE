"""Onboarding Step 2 routes (Issue #42). Both routes: Bearer session (401), role=user
(403 ONBOARDING_NOT_ALLOWED), ACTIVE account (403 AGE_REVIEW_REQUIRED), current pledge and
Step 1 (409 PREVIOUS_STEP_NOT_COMPLETED with the first incomplete step), then the body.
The save re-checks the status and the earlier steps on the locked users row."""
from typing import ClassVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.orm import Session

from app.account_status import ensure_active
from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, as_field_error, ok
from app.auth import get_contract_user
from app.database import get_db
from app.dog_experience_service import read_experience, save_experience, validate_experience
from app.models import User, UserRole
from app.onboarding_common import (ONBOARDING_NOT_ALLOWED_MESSAGE, STEP_BASIC_INFORMATION, STEP_PLEDGE,
                                   first_incomplete_step, get_clock, previous_step_required)

router = APIRouter(prefix="/users/me", tags=["onboarding"], route_class=ContractRoute)

COMMON_ERRORS = {
    401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED"},
    403: {"model": ContractErrorResponse, "description": "ONBOARDING_NOT_ALLOWED(일반 사용자 외), AGE_REVIEW_REQUIRED"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
}


def get_step2_user(user: User = Depends(get_contract_user), db: Session = Depends(get_db)) -> User:
    if user.role != UserRole.USER:
        raise ContractError(403, "ONBOARDING_NOT_ALLOWED", ONBOARDING_NOT_ALLOWED_MESSAGE)
    ensure_active(user)
    step = first_incomplete_step(db, user)
    if step in (STEP_PLEDGE, STEP_BASIC_INFORMATION):
        raise previous_step_required(step)
    return user


class DogExperienceRequest(BaseModel):
    """The whole body is checked at once so the 422 order follows Issue #42. Unknown
    fields (including the old dog_size_codes) are ignored for now (open item)."""
    model_config = ConfigDict(extra="ignore")

    has_experience: bool
    experienced_breeds_text: str | None = None
    dog_size_code: str | None = None
    walking_frequency: str | None = None
    perceived_difficulty: str | None = None
    challenge_codes: list[str] = []

    # A missing or unreadable body never reaches the validator; ContractRoute uses this.
    contract_required_errors: ClassVar[dict] = {
        "has_experience": ("EXPERIENCE_REQUIRED", "반려견 경험 여부를 선택해 주세요.")}

    @model_validator(mode="before")
    @classmethod
    def check(cls, value):
        return as_field_error(validate_experience, value)


@router.get("/onboarding/dog-experience",
            responses={**COMMON_ERRORS,
                       409: {"model": ContractErrorResponse,
                             "description": "PREVIOUS_STEP_NOT_COMPLETED(required_step=PLEDGE 또는 BASIC_INFORMATION)"}})
def get_dog_experience(user: User = Depends(get_step2_user), db: Session = Depends(get_db)):
    response = ok(read_experience(db, user.id), "반려견 경험 정보를 조회했습니다.")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.put("/onboarding/dog-experience",
            responses={**COMMON_ERRORS,
                       409: {"model": ContractErrorResponse,
                             "description": "PREVIOUS_STEP_NOT_COMPLETED, DOG_EXPERIENCE_ALREADY_COMPLETED"},
                       422: {"model": ContractErrorResponse,
                             "description": "EXPERIENCE_REQUIRED, EXPERIENCE_DETAILS_NOT_ALLOWED, EXPERIENCED_BREED_REQUIRED, "
                                            "INVALID_EXPERIENCED_BREED, DOG_SIZE_REQUIRED, INVALID_DOG_SIZE, "
                                            "WALKING_FREQUENCY_REQUIRED, INVALID_WALKING_FREQUENCY, DIFFICULTY_REQUIRED, "
                                            "INVALID_DIFFICULTY, INVALID_CHALLENGE, CHALLENGE_NONE_CONFLICT"}})
def put_dog_experience(payload: DogExperienceRequest, user: User = Depends(get_step2_user),
                       db: Session = Depends(get_db), clock=Depends(get_clock)):
    data = save_experience(db, user.id, payload.model_dump(), clock())
    return ok(data, "반려견 경험 입력이 완료되었습니다.")
