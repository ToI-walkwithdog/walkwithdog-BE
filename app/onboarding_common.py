"""Shared onboarding rules (Issue #40), reused by the pledge (#33), Step 2 and Step 3,
the age review flow (#41) and GET /users/me (#45).

- Clock: get_clock() is a FastAPI dependency so tests inject a fixed time.
- Asia/Seoul dates: a fixed +09:00 offset. Korea has no daylight saving time, and
  this avoids depending on a time zone database (tzdata is not in requirements).
- Age: full years in Asia/Seoul. A 29 February birthday counts as 1 March in
  common years.
- Account status: app.account_status (ACTIVE or AGE_REVIEW_REQUIRED).
- Steps: first incomplete step in the order AGE_REVIEW, PLEDGE, BASIC_INFORMATION,
  PET_EXPERIENCE, WALKING_ROUTINE, or None when everything is done. Each step is checked
  on its stored record, so a skipped earlier step is still reported first. Step 3 is
  done when its volunteer_onboarding_routines row exists (Issue #44).
- Progress snapshot (#45): load_progress() reads the account status, the three step
  records and the current-version pledge in ONE statement, so PostgreSQL's default READ
  COMMITTED isolation still gives a single consistent snapshot. next_onboarding_step()
  turns a snapshot into the next step; the step APIs, required_step of their 409 and
  GET /users/me all use it.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.account_status import ACCOUNT_ACTIVE, ensure_active
from app.api_contract import ContractError, INTERNAL_SERVER_ERROR_MESSAGE
from app.auth import contract_unauthorized, get_contract_user, unauthorized
from app.database import get_db
from app.models import (User, UserPledgeAcknowledgement, UserRole, VolunteerOnboardingBasic,
                        VolunteerOnboardingExperience, VolunteerOnboardingRoutine)
from app.pledge_service import PledgeVersionNotConfigured, current_pledge_acknowledgement, current_pledge_version

KST = timezone(timedelta(hours=9), "Asia/Seoul")

STEP_AGE_REVIEW = "AGE_REVIEW"
STEP_PLEDGE = "PLEDGE"
STEP_BASIC_INFORMATION = "BASIC_INFORMATION"
STEP_PET_EXPERIENCE = "PET_EXPERIENCE"
STEP_WALKING_ROUTINE = "WALKING_ROUTINE"

ONBOARDING_NOT_ALLOWED_MESSAGE = "사용자 정보 입력 권한이 없습니다."
# Notion has a message only for required_step=BASIC_INFORMATION (Step 2 spec) and
# PET_EXPERIENCE (Step 3 specs).
PREVIOUS_STEP_MESSAGES = {
    STEP_PLEDGE: "최초 산책 서약을 먼저 완료해 주세요.",
    STEP_BASIC_INFORMATION: "기본 정보 입력을 먼저 완료해 주세요.",
    STEP_PET_EXPERIENCE: "이전 단계를 먼저 완료해 주세요.",
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


@dataclass(frozen=True)
class OnboardingProgress:
    """Stored onboarding records of one account at one moment. Read only."""
    account_status: str
    current_pledge_version: str
    pledge_acknowledged_at: datetime | None  # for the current version only
    step1_completed: bool
    step2_completed: bool
    step3_completed: bool

    @property
    def pledge_completed(self) -> bool:
        return self.pledge_acknowledged_at is not None


def load_progress(db: Session, user_id: int) -> OnboardingProgress:
    """One SELECT for the status, the current pledge record and the three step records.

    Raises PledgeVersionNotConfigured (a 500) when the server has no current pledge version,
    and 401 USER_NOT_FOUND when the account no longer exists (a concurrent sign-up exit).
    """
    version = current_pledge_version()

    def recorded(model):
        return select(model.user_id).where(model.user_id == User.id).exists()

    row = db.execute(
        select(User.account_status, UserPledgeAcknowledgement.acknowledged_at,
               recorded(VolunteerOnboardingBasic), recorded(VolunteerOnboardingExperience),
               recorded(VolunteerOnboardingRoutine))
        .select_from(User)
        .outerjoin(UserPledgeAcknowledgement, and_(UserPledgeAcknowledgement.user_id == User.id,
                                                   UserPledgeAcknowledgement.pledge_version == version))
        .where(User.id == user_id)).one_or_none()
    if row is None:
        raise contract_unauthorized("USER_NOT_FOUND")
    status, acknowledged_at, step1, step2, step3 = row
    return OnboardingProgress(status, version, acknowledged_at, bool(step1), bool(step2), bool(step3))


def next_onboarding_step(role, progress: OnboardingProgress) -> str | None:
    """The first incomplete step of a snapshot; None when done or for shelter and admin."""
    if role != UserRole.USER:
        return None
    if progress.account_status != ACCOUNT_ACTIVE:
        return STEP_AGE_REVIEW
    if not progress.pledge_completed:
        return STEP_PLEDGE
    if not progress.step1_completed:
        return STEP_BASIC_INFORMATION
    if not progress.step2_completed:
        return STEP_PET_EXPERIENCE
    if not progress.step3_completed:
        return STEP_WALKING_ROUTINE
    return None


def first_incomplete_step(db: Session, user: User) -> str | None:
    """None for shelter and admin accounts (no onboarding, no query). Read only.

    The account status is the one on `user`: the row the caller checked or locked in its
    own transaction, as before #45. The step and pledge records come from one statement.
    """
    if user.role != UserRole.USER:
        return None
    if user.account_status != ACCOUNT_ACTIVE:
        return STEP_AGE_REVIEW  # as before: no pledge version or record is needed for this answer
    progress = replace(load_progress(db, user.id), account_status=user.account_status)
    return next_onboarding_step(user.role, progress)


def require_onboarding_for_new_walk(db: Session, user: User) -> None:
    """Check new volunteer activities after role and locked account status checks."""
    try:
        step = first_incomplete_step(db, user)
    except PledgeVersionNotConfigured:
        raise HTTPException(500, INTERNAL_SERVER_ERROR_MESSAGE) from None
    except ContractError:  # load_progress() could no longer find the account.
        raise unauthorized() from None
    if step is not None:
        raise HTTPException(403, "온보딩을 먼저 완료해 주세요.")


def onboarding_status(db: Session, user: User) -> dict:
    """GET /users/me progress fields (#45) from one snapshot. Shelter and admin accounts get
    null for the onboarding fields and keep the pledge fields."""
    progress = load_progress(db, user.id)
    acknowledged_at = progress.pledge_acknowledged_at
    data = {"account_status": progress.account_status, "step1_completed": None, "step2_completed": None,
            "step3_completed": None, "pledge_completed": progress.pledge_completed,
            "current_pledge_version": progress.current_pledge_version,
            "pledge_acknowledged_at": None if acknowledged_at is None else as_utc(acknowledged_at).isoformat(),
            "onboarding_completed": None, "next_onboarding_step": None}
    if user.role == UserRole.USER:
        step = next_onboarding_step(user.role, progress)
        data.update(step1_completed=progress.step1_completed, step2_completed=progress.step2_completed,
                    step3_completed=progress.step3_completed, onboarding_completed=step is None,
                    next_onboarding_step=step)
    return data
