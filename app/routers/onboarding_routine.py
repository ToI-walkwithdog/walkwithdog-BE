"""Onboarding Step 3 routes (Issue #44). Both routes: Bearer session (401), role=user and an
ACTIVE account (403 ONBOARDING_NOT_ALLOWED; this API also uses it for AGE_REVIEW_REQUIRED),
the current pledge, Step 1 and Step 2 (409 PREVIOUS_STEP_NOT_COMPLETED with the first
incomplete step), then the body. The save re-checks all of it on the locked users row."""
from typing import ClassVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.orm import Session

from app.api_contract import ContractErrorResponse, ContractRoute, as_field_error, ok
from app.auth import get_contract_user
from app.database import get_db
from app.models import User
from app.onboarding_common import get_clock
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


class WalkingRoutineRequest(BaseModel):
    """The whole body is checked at once so the 422 order follows Issue #44. Unknown fields
    (a selection source, coordinates, a user ID or a time) are ignored and never stored."""
    model_config = ConfigDict(extra="ignore")

    region_codes: list[str]
    available_time_slot_codes: list[str]
    walking_duration_code: str
    comfortable_dog_size_code: str

    # A missing or unreadable body never reaches the validator; ContractRoute uses this.
    contract_required_errors: ClassVar[dict] = {
        REGIONS: REGIONS_REQUIRED, TIME_SLOTS: TIME_SLOTS_REQUIRED,
        DURATION: DURATION_REQUIRED, DOG_SIZE: DOG_SIZE_REQUIRED}

    @model_validator(mode="before")
    @classmethod
    def check(cls, value):
        return as_field_error(validate_routine, value)


@router.get("/onboarding/walking-routine",
            responses={**COMMON_ERRORS, 409: {"model": ContractErrorResponse, "description": PREVIOUS_STEP}})
def get_walking_routine(user: User = Depends(get_routine_user), db: Session = Depends(get_db)):
    response = ok(read_routine(db, user), READ_MESSAGE)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.put("/onboarding/walking-routine",
            responses={**COMMON_ERRORS,
                       409: {"model": ContractErrorResponse,
                             "description": f"{PREVIOUS_STEP}, WALKING_ROUTINE_ALREADY_COMPLETED"},
                       422: {"model": ContractErrorResponse,
                             "description": "REGIONS_REQUIRED, REGION_LIMIT_EXCEEDED, INVALID_REGION_CODE, "
                                            "TIME_SLOTS_REQUIRED, INVALID_TIME_SLOT, DURATION_REQUIRED, "
                                            "INVALID_DURATION, DOG_SIZE_REQUIRED, INVALID_DOG_SIZE"}})
def put_walking_routine(payload: WalkingRoutineRequest, user: User = Depends(get_routine_user),
                        db: Session = Depends(get_db), clock=Depends(get_clock)):
    data = save_routine(db, user.id, payload.model_dump(), clock())
    return ok(data, SAVED_MESSAGE)
