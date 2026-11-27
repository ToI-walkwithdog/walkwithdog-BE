"""Account status (Issue #40): users.account_status is ACTIVE or AGE_REVIEW_REQUIRED.

Kept free of onboarding and pledge imports so the pledge (#33), Step 1 to 3 and the
age review flow (#41) can all use it.

Lock order everywhere: the users row first, then rows that belong to the user.
FOR SHARE (pledge, Step 1 save) and FOR UPDATE (status change, SMS send reservation)
conflict, so a status change waits for writers that already passed the ACTIVE check
on the locked row, and writers that start later see the new status.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api_contract import ContractError
from app.models import User, UserRole

ACCOUNT_ACTIVE = "ACTIVE"
ACCOUNT_AGE_REVIEW_REQUIRED = "AGE_REVIEW_REQUIRED"
AGE_REVIEW_REQUIRED_MESSAGE = "연령 확인 정보를 먼저 수정해 주세요."


def age_review_required() -> ContractError:
    return ContractError(403, "AGE_REVIEW_REQUIRED", AGE_REVIEW_REQUIRED_MESSAGE)


def is_active(user: User) -> bool:
    return user.account_status == ACCOUNT_ACTIVE


def ensure_active(user: User) -> None:
    """403 AGE_REVIEW_REQUIRED unless ACTIVE. A request-start check alone is not enough:
    call it again on the row returned by lock_user() in the writing transaction."""
    if not is_active(user):
        raise age_review_required()


def lock_user(db: Session, user_id: int, *, for_update: bool = False, nowait: bool = False) -> User | None:
    """Re-read the account row under a row lock held until commit or rollback.

    populate_existing() refreshes an object the session already loaded, so the status
    read here is the one the lock protects, not the copy loaded at authentication.
    """
    query = db.query(User).filter(User.id == user_id).populate_existing()
    return query.with_for_update(read=not for_update, nowait=nowait).one_or_none()


def require_active_writer(db: Session, user: User, *, for_update: bool = False, nowait: bool = False) -> None:
    """For writes of the `detail`-format APIs (walks, walk requests, reviews, chat, surveys,
    matching): lock the actor's users row FOR SHARE in the current transaction and re-read
    the status before anything is written.

    A status change (FOR UPDATE) that committed first is seen here and the write is
    refused with the same 403 as get_current_user, so nothing is stored. A write that took
    the share lock first finishes before the status change can proceed. Shelter and admin
    accounts never change status, so they are not locked. Call it before the business rows
    are locked (users row first) and never across an external call.
    """
    if user.role != UserRole.USER:
        return
    locked = lock_user(db, user.id, for_update=for_update, nowait=nowait)
    if locked is None:  # deleted by a concurrent sign-up exit
        raise HTTPException(401, "로그인이 필요하거나 세션이 만료되었습니다.", headers={"WWW-Authenticate": "Bearer"})
    if not is_active(locked):
        raise HTTPException(403, AGE_REVIEW_REQUIRED_MESSAGE)


def change_to_age_review(db: Session, user_id: int, now: datetime) -> None:
    """Own transaction: lock the row FOR UPDATE, set AGE_REVIEW_REQUIRED, commit.

    Pledge records and consents are kept. age_review_required_at keeps the first time.
    """
    try:
        user = lock_user(db, user_id, for_update=True)
        if user is not None and user.account_status == ACCOUNT_ACTIVE:
            user.account_status = ACCOUNT_AGE_REVIEW_REQUIRED
            if user.age_review_required_at is None:
                user.age_review_required_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise
