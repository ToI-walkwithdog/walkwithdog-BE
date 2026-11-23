"""First walk pledge confirmation (Issue #33): PUT /users/me/pledge.

Contract (Notion "최초 산책 서약 확인 및 기록", Issue #33 final decision of 2026-11-23):
- Only `pledge_version` is sent. The user ID and the confirmation time come from the
  server. Step 1 to 3 are not preconditions: the pledge comes right after sign-up.
- The same user and version keep one row with the first `acknowledged_at`; a repeat
  request returns that row with 200. Rows for older versions are never touched.

Not decided yet (Issue #33): where the current pledge version is managed. Until then
it is read from the PLEDGE_CURRENT_VERSION setting with no default; if it is unset or
invalid the API fails closed (500) and stores nothing.

Account status (Issue #40): an AGE_REVIEW_REQUIRED account gets 403. The route checks
it at request start, and acknowledge_pledge() checks it again on the users row it
locks FOR SHARE, in the same transaction as the insert. A Step 1 status change locks
that row FOR UPDATE, so the two are serialized: a pledge that took the lock first is
kept (pledge history is never deleted by a status change), and a pledge that comes
after the change is refused without a record.
"""
from datetime import datetime, timezone
import os

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.account_status import ensure_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.models import UserPledgeAcknowledgement

PLEDGE_VERSION_MAX_LENGTH = 32
PLEDGE_VERSION_REQUIRED_MESSAGE = "서약 버전을 확인해 주세요."
INVALID_PLEDGE_VERSION_MESSAGE = "올바른 서약 버전을 입력해 주세요."
PLEDGE_VERSION_MISMATCH_MESSAGE = "서약 내용을 다시 확인해 주세요."
PLEDGE_NOT_ALLOWED_MESSAGE = "서약할 수 없는 계정입니다."
PLEDGE_COMPLETED_MESSAGE = "서약 확인이 완료되었습니다."


class PledgeVersionNotConfigured(RuntimeError):
    """PLEDGE_CURRENT_VERSION is missing or invalid; pledges must not be recorded."""


def current_pledge_version() -> str:
    """Read the server's current pledge version on every call; raise if it is not usable."""
    version = (os.getenv("PLEDGE_CURRENT_VERSION") or "").strip()
    if not version or len(version) > PLEDGE_VERSION_MAX_LENGTH:
        raise PledgeVersionNotConfigured("PLEDGE_CURRENT_VERSION must be set (1 to 32 characters)")
    return version


def normalize_pledge_version(value) -> str:
    """422 rules for the request field. Missing, null and blank are REQUIRED; other types or over 32 are INVALID."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ContractError(422, "PLEDGE_VERSION_REQUIRED", PLEDGE_VERSION_REQUIRED_MESSAGE, {"field": "pledge_version"})
    if not isinstance(value, str) or len(value.strip()) > PLEDGE_VERSION_MAX_LENGTH:
        raise ContractError(422, "INVALID_PLEDGE_VERSION", INVALID_PLEDGE_VERSION_MESSAGE, {"field": "pledge_version"})
    return value.strip()


def find_acknowledgement(db: Session, user_id: int, pledge_version: str) -> UserPledgeAcknowledgement | None:
    return (db.query(UserPledgeAcknowledgement)
            .filter(UserPledgeAcknowledgement.user_id == user_id,
                    UserPledgeAcknowledgement.pledge_version == pledge_version)
            .one_or_none())


def current_pledge_acknowledgement(db: Session, user_id: int) -> UserPledgeAcknowledgement | None:
    """The user's record for the server's current version, or None. Read only.

    For GET /users/me (#45) and the Step 1 precondition (#40): a record for an older
    version alone does not count as a completed pledge.
    """
    return find_acknowledgement(db, user_id, current_pledge_version())


def _insert_if_absent(db: Session, user_id: int, pledge_version: str, now: datetime) -> None:
    values = {"user_id": user_id, "pledge_version": pledge_version, "acknowledged_at": now}
    insert = postgresql_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    # A concurrent request for the same user and version waits on the unique index and
    # then inserts nothing, so the first confirmation time is never overwritten.
    db.execute(insert(UserPledgeAcknowledgement).values(**values)
               .on_conflict_do_nothing(index_elements=["user_id", "pledge_version"]))


def acknowledge_pledge(db: Session, user_id: int, pledge_version: str) -> UserPledgeAcknowledgement:
    """Record the pledge once and return the stored row (first confirmation time)."""
    server_version = current_pledge_version()  # fail closed before any write
    if pledge_version != server_version:
        raise ContractError(409, "PLEDGE_VERSION_MISMATCH", PLEDGE_VERSION_MISMATCH_MESSAGE, {"field": "pledge_version"})
    try:
        # Share lock on the account row for the rest of the transaction, then the status
        # check on that locked, freshly read row (not the copy loaded at authentication).
        user = lock_user(db, user_id)
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        ensure_active(user)
        existing = find_acknowledgement(db, user_id, server_version)
        if existing is None:
            _insert_if_absent(db, user_id, server_version, datetime.now(timezone.utc))
        db.commit()
    except Exception:
        db.rollback()
        raise
    # After commit: either this request's row or the one a concurrent request stored first.
    record = find_acknowledgement(db, user_id, server_version)
    if record is None:  # cannot happen unless the row was deleted in between
        raise RuntimeError("pledge acknowledgement missing after commit")
    return record
