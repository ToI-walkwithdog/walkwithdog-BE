"""Onboarding Step 3 walking routine (Issue #44): GET and PUT /users/me/onboarding/walking-routine.

Contract: Issue #44 and the Notion Step 3 save and read specs (codes, messages, 422 data).
Duration codes follow the Notion save spec of 2026-11-24 (UP_TO_30_MIN is 30 minutes or less).

Body checks follow the Issue #44 server order: first presence and counts of all four fields
(regions 1 to 3, time slots 1 to 4, one duration, one size), then duplicates and fixed codes,
then the region master inside the saving transaction.

Save transaction (same rule as the pledge, Step 1 and Step 2):
  lock the users row FOR SHARE -> re-check ACTIVE and the earlier steps on that row ->
  check the regions under FOR SHARE (PostgreSQL) -> compare with a stored routine ->
  insert the routine, its regions and its time slots -> commit.
A status change (FOR UPDATE on users) and a region sync that deactivates a chosen region
(UPDATE on regions) wait for this save; a change committed first is seen and refused.
Concurrent saves of one account: the primary keys keep one routine; the loser re-reads it and
answers 200 for the same answer or 409 for a different one. Any failure rolls everything back.
"""
from datetime import datetime
from numbers import Real

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.account_status import is_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.models import (DOG_SIZE_CODES, TIME_SLOT_CODES, WALKING_DURATION_CODES, User, UserRole,
                        VolunteerOnboardingRoutine, VolunteerRoutineRegion, VolunteerRoutineTimeSlot)
from app.onboarding_common import (ONBOARDING_NOT_ALLOWED_MESSAGE, STEP_BASIC_INFORMATION, STEP_PET_EXPERIENCE,
                                   STEP_PLEDGE, as_utc, first_incomplete_step, previous_step_required)
from app.region_service import REGION_CODE_MAX_LENGTH, InvalidRegionCodes, require_selectable_regions

REGION_MAX = 3
TIME_SLOT_MAX = len(TIME_SLOT_CODES)
EARLIER_STEPS = (STEP_PLEDGE, STEP_BASIC_INFORMATION, STEP_PET_EXPERIENCE)

REGIONS = "region_codes"
TIME_SLOTS = "available_time_slot_codes"
DURATION = "walking_duration_code"
DOG_SIZE = "comfortable_dog_size_code"

# Notion Step 3 save spec: (code, message) per rule.
REGIONS_REQUIRED = ("REGIONS_REQUIRED", "산책 지역을 한 개 이상 선택해 주세요.")
REGION_LIMIT_EXCEEDED = ("REGION_LIMIT_EXCEEDED", "산책 지역은 최대 3개까지 선택할 수 있습니다.")
INVALID_REGION_CODE = ("INVALID_REGION_CODE", "산책 지역을 확인해 주세요.")
TIME_SLOTS_REQUIRED = ("TIME_SLOTS_REQUIRED", "산책 가능한 시간대를 선택해 주세요.")
INVALID_TIME_SLOT = ("INVALID_TIME_SLOT", "산책 시간대를 확인해 주세요.")
DURATION_REQUIRED = ("DURATION_REQUIRED", "산책 가능 시간을 선택해 주세요.")
INVALID_DURATION = ("INVALID_DURATION", "산책 시간을 확인해 주세요.")
DOG_SIZE_REQUIRED = ("DOG_SIZE_REQUIRED", "반려견 크기를 선택해 주세요.")
INVALID_DOG_SIZE = ("INVALID_DOG_SIZE", "반려견 크기를 확인해 주세요.")

SAVED_MESSAGE = "산책 루틴 입력이 완료되었습니다."
READ_MESSAGE = "산책 루틴 정보를 조회했습니다."


def walking_duration_code(minutes) -> str:
    """The duration code of one walk of `minutes` (Notion, 2026-11-24): UP_TO_30_MIN for
    0 < t <= 30, BETWEEN_30_AND_60_MIN for 30 < t < 60, OVER_60_MIN for t >= 60. The API
    receives codes, not minutes; this is the single definition of the boundaries."""
    if isinstance(minutes, bool) or not isinstance(minutes, Real) or not minutes > 0:
        raise ValueError("A walk duration must be a positive number of minutes")
    if minutes <= 30:
        return "UP_TO_30_MIN"
    if minutes < 60:
        return "BETWEEN_30_AND_60_MIN"
    return "OVER_60_MIN"


def onboarding_not_allowed() -> ContractError:
    return ContractError(403, "ONBOARDING_NOT_ALLOWED", ONBOARDING_NOT_ALLOWED_MESSAGE)


def ensure_routine_allowed(db: Session, user: User) -> None:
    """403 for other roles and for AGE_REVIEW_REQUIRED (this API, Notion), then 409 with the
    first incomplete earlier step. Called by the route and again on the locked row."""
    if user.role != UserRole.USER or not is_active(user):
        raise onboarding_not_allowed()
    step = first_incomplete_step(db, user)
    if step in EARLIER_STEPS:
        raise previous_step_required(step)


def _error(rule, field, **extra) -> ContractError:
    code, message = rule
    return ContractError(422, code, message, {"field": field, **extra})


def _missing(value) -> bool:
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def _unique_codes(value, allowed=None, max_length=None) -> bool:
    return (isinstance(value, list) and len(set(map(repr, value))) == len(value)
            and all(isinstance(code, str) and code and (allowed is None or code in allowed)
                    and (max_length is None or len(code) <= max_length) for code in value))


def validate_routine(body) -> dict:
    """Return the answer or raise the first 422 (Issue #44 order: presence and counts of every
    field, then duplicates and fixed codes). Region master checks happen when saving."""
    body = body if isinstance(body, dict) else {}
    regions, slots = body.get(REGIONS), body.get(TIME_SLOTS)
    duration, size = body.get(DURATION), body.get(DOG_SIZE)
    if _missing(regions):
        raise _error(REGIONS_REQUIRED, REGIONS)
    if isinstance(regions, list) and len(regions) > REGION_MAX:
        raise _error(REGION_LIMIT_EXCEEDED, REGIONS, max=REGION_MAX)
    if _missing(slots):
        raise _error(TIME_SLOTS_REQUIRED, TIME_SLOTS)
    if isinstance(slots, list) and len(slots) > TIME_SLOT_MAX:
        # Five or more of four codes always repeat one; Notion has no separate limit code.
        raise _error(INVALID_TIME_SLOT, TIME_SLOTS)
    if _missing(duration):
        raise _error(DURATION_REQUIRED, DURATION)
    if _missing(size):
        raise _error(DOG_SIZE_REQUIRED, DOG_SIZE)
    if not _unique_codes(regions, max_length=REGION_CODE_MAX_LENGTH):
        raise _error(INVALID_REGION_CODE, REGIONS)
    if not _unique_codes(slots, allowed=TIME_SLOT_CODES):
        raise _error(INVALID_TIME_SLOT, TIME_SLOTS)
    if not isinstance(duration, str) or duration not in WALKING_DURATION_CODES:
        raise _error(INVALID_DURATION, DURATION)
    if not isinstance(size, str) or size not in DOG_SIZE_CODES:
        raise _error(INVALID_DOG_SIZE, DOG_SIZE)
    return {REGIONS: regions, TIME_SLOTS: slots, DURATION: duration, DOG_SIZE: size}


def _stored(db: Session, routine: VolunteerOnboardingRoutine) -> dict:
    regions = sorted(row.region_code for row in db.query(VolunteerRoutineRegion)
                     .filter(VolunteerRoutineRegion.user_id == routine.user_id))
    order = {code: index for index, code in enumerate(TIME_SLOT_CODES)}
    slots = sorted((row.time_slot_code for row in db.query(VolunteerRoutineTimeSlot)
                    .filter(VolunteerRoutineTimeSlot.user_id == routine.user_id)), key=order.get)
    return {REGIONS: regions, TIME_SLOTS: slots, DURATION: routine.walking_duration_code,
            DOG_SIZE: routine.comfortable_dog_size_code, "completed_at": as_utc(routine.completed_at).isoformat()}


def read_routine(db: Session, user: User) -> dict:
    """Read only: no write, no location data."""
    routine = db.get(VolunteerOnboardingRoutine, user.id)
    return {"step3_completed": routine is not None,
            "routine": None if routine is None else _stored(db, routine),
            "next_onboarding_step": first_incomplete_step(db, user)}


def _same(stored: dict, answer: dict) -> bool:
    return (set(stored[REGIONS]) == set(answer[REGIONS]) and set(stored[TIME_SLOTS]) == set(answer[TIME_SLOTS])
            and stored[DURATION] == answer[DURATION] and stored[DOG_SIZE] == answer[DOG_SIZE])


def _replay(db: Session, user: User, routine: VolunteerOnboardingRoutine, answer: dict) -> dict:
    stored = _stored(db, routine)
    if not _same(stored, answer):
        raise ContractError(409, "WALKING_ROUTINE_ALREADY_COMPLETED", "산책 루틴 입력이 이미 완료되었습니다.")
    return {"step3_completed": True, "completed_at": stored["completed_at"],
            "next_onboarding_step": first_incomplete_step(db, user)}


def _require_regions(db: Session, codes: list[str]) -> None:
    try:
        require_selectable_regions(db, codes, lock=True)
    except InvalidRegionCodes:
        # Unknown, inactive, under an inactive SIDO, or a SIDO-level code.
        raise _error(INVALID_REGION_CODE, REGIONS) from None


def _completed_routine(db: Session, user_id: int) -> VolunteerOnboardingRoutine | None:
    """The stored routine seen before inserting (a separate function so tests can line up
    concurrent saves right after this check)."""
    return db.get(VolunteerOnboardingRoutine, user_id)


def save_routine(db: Session, user_id: int, answer: dict, now: datetime) -> dict:
    try:
        user = lock_user(db, user_id)  # FOR SHARE, then the current state of the locked row
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        ensure_routine_allowed(db, user)
        _require_regions(db, answer[REGIONS])
        existing = _completed_routine(db, user_id)
        if existing is not None:
            result = _replay(db, user, existing, answer)
            db.rollback()  # nothing to write; release the locks
            return result
        db.add(VolunteerOnboardingRoutine(
            user_id=user_id, walking_duration_code=answer[DURATION], comfortable_dog_size_code=answer[DOG_SIZE],
            completed_at=now, updated_at=now))
        db.flush()
        db.add_all(VolunteerRoutineRegion(user_id=user_id, region_code=code) for code in answer[REGIONS])
        db.add_all(VolunteerRoutineTimeSlot(user_id=user_id, time_slot_code=code) for code in answer[TIME_SLOTS])
        db.flush()
        next_step = first_incomplete_step(db, user)
        db.commit()
        return {"step3_completed": True, "completed_at": as_utc(now).isoformat(), "next_onboarding_step": next_step}
    except IntegrityError:
        db.rollback()
        # A concurrent save of this account committed first (primary key).
        existing = db.get(VolunteerOnboardingRoutine, user_id)
        if existing is None:
            raise
        user = db.get(User, user_id)
        try:
            return _replay(db, user, existing, answer)
        finally:
            db.rollback()
    except Exception:
        db.rollback()
        raise
