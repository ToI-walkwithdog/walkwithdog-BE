"""Shared onboarding rules (Issue #40), reused by the pledge (#33), Step 2 and Step 3,
the age review flow (#41) and GET /users/me (#45).

- Clock: get_clock() is a FastAPI dependency so tests inject a fixed time.
- Asia/Seoul dates: a fixed +09:00 offset. Korea has no daylight saving time, and
  this avoids depending on a time zone database (tzdata is not in requirements).
- Age: full years in Asia/Seoul. A 29 February birthday counts as 1 March in
  common years.
- Account status: app.account_status (ACTIVE or AGE_REVIEW_REQUIRED).
- Steps: first incomplete step in the order AGE_REVIEW, PLEDGE, BASIC_INFORMATION,
  PET_EXPERIENCE, WALKING_ROUTINE. Step 2 and 3 are not implemented yet, so an
  account with Step 1 done always reports PET_EXPERIENCE; nothing marks them done.
"""
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends
from sqlalchemy.orm import Session

from app.account_status import ACCOUNT_ACTIVE, ensure_active
from app.api_contract import ContractError
from app.auth import get_contract_user
from app.database import get_db
from app.models import User, UserRole, VolunteerOnboardingBasic
from app.pledge_service import current_pledge_acknowledgement

KST = timezone(timedelta(hours=9), "Asia/Seoul")

STEP_AGE_REVIEW = "AGE_REVIEW"
STEP_PLEDGE = "PLEDGE"
STEP_BASIC_INFORMATION = "BASIC_INFORMATION"
STEP_PET_EXPERIENCE = "PET_EXPERIENCE"
STEP_WALKING_ROUTINE = "WALKING_ROUTINE"

ONBOARDING_NOT_ALLOWED_MESSAGE = "사용자 정보 입력 권한이 없습니다."
# Notion has a message only for required_step=BASIC_INFORMATION (Step 2 spec).
PREVIOUS_STEP_MESSAGES = {
    STEP_PLEDGE: "최초 산책 서약을 먼저 완료해 주세요.",
    STEP_BASIC_INFORMATION: "기본 정보 입력을 먼저 완료해 주세요.",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_clock():
    """Dependency: a callable returning the current aware UTC time."""
    return utc_now


def as_utc(value: datetime) -> datetime:
    # SQLite drops the time zone; PostgreSQL TIMESTAMPTZ keeps it. Both store UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def kst_date(now: datetime) -> date:
    return as_utc(now).astimezone(KST).date()


def next_kst_midnight(now: datetime) -> datetime:
    local = as_utc(now).astimezone(KST)
    return datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), KST)


def birthday_in(year: int, birth: date) -> date:
    if birth.month == 2 and birth.day == 29:
        try:
            return date(year, 2, 29)
        except ValueError:  # common year: the age goes up on 1 March
            return date(year, 3, 1)
    return date(year, birth.month, birth.day)


def age_years(birth: date, today: date) -> int:
    """Full years on `today` (an Asia/Seoul date). Same function for saving and reading."""
    years = today.year - birth.year
    return years - 1 if today < birthday_in(today.year, birth) else years


def previous_step_required(step: str) -> ContractError:
    return ContractError(409, "PREVIOUS_STEP_NOT_COMPLETED", PREVIOUS_STEP_MESSAGES[step], {"required_step": step})


def ensure_pledge_completed(db: Session, user: User) -> None:
    if current_pledge_acknowledgement(db, user.id) is None:
        raise previous_step_required(STEP_PLEDGE)


def get_onboarding_user(user: User = Depends(get_contract_user), db: Session = Depends(get_db)) -> User:
    """Step 1 order: session (401), role (403), account status (403), current pledge (409)."""
    if user.role != UserRole.USER:
        raise ContractError(403, "ONBOARDING_NOT_ALLOWED", ONBOARDING_NOT_ALLOWED_MESSAGE)
    ensure_active(user)
    ensure_pledge_completed(db, user)
    return user


def step1_completed(db: Session, user_id: int) -> bool:
    return db.get(VolunteerOnboardingBasic, user_id) is not None


def first_incomplete_step(db: Session, user: User) -> str | None:
    """None for shelter and admin accounts (no onboarding). Read only."""
    if user.role != UserRole.USER:
        return None
    if user.account_status != ACCOUNT_ACTIVE:
        return STEP_AGE_REVIEW
    if current_pledge_acknowledgement(db, user.id) is None:
        return STEP_PLEDGE
    if not step1_completed(db, user.id):
        return STEP_BASIC_INFORMATION
    # Step 2 and Step 3 add their checks here; until then PET_EXPERIENCE is never done.
    return STEP_PET_EXPERIENCE
