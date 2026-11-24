"""Shelter on-site start codes (Issue #87).

Flow: a shelter manager issues a 6-digit code for an approved reservation that is inside its
time window -> the volunteer verifies it -> POST /walks uses that verification and creates
the Walk in one transaction.

- Callers commit or roll back. Every write first takes the reservation locks of
  walk_request_service.locked_request() (Dog -> User -> WalkRequest, NOWAIT), so issuing,
  verifying, starting and cancelling the same reservation are serialized.
- A code expires at min(issue + 10 minutes, reservation end). Re-issuing revokes the previous
  code and its verification and never resets the failure limit.
- Five consecutive failures by the same volunteer on the same reservation block it for five
  minutes. Failures are returned as an outcome, not raised, so the router can commit the
  counter before answering 422 or 429.
- Only an HMAC of the code (keyed by SHELTER_CODE_HMAC_KEY and bound to the reservation ID) is
  stored. The code itself appears only in the issuing response and is never logged.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import math
import os
import re
import secrets

from fastapi import HTTPException

from app.account_status import ensure_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.models import UserRole
from app.shelter_code_models import FAILURE_LIMIT, ShelterCodeAttempt, ShelterCodeIssuance
from app.walk_request_service import as_utc, has_walk_record, locked_request, validate_linked_eligibility

CODE_TTL = timedelta(minutes=10)
LOCK_DURATION = timedelta(minutes=5)
HMAC_KEY_MIN_LENGTH = 32
CODE_PATTERN = re.compile(r"[0-9]{6}")


class ShelterCodeNotConfigured(RuntimeError):
    """SHELTER_CODE_HMAC_KEY is missing or too short; nothing is issued, verified or stored."""


def not_allowed():
    return ContractError(403, "WALK_REQUEST_NOT_ALLOWED", "이 예약의 보호소 코드를 처리할 권한이 없습니다.")


def not_found():
    return ContractError(404, "WALK_REQUEST_NOT_FOUND", "예약을 찾을 수 없습니다.")


def not_eligible():
    return ContractError(409, "WALK_REQUEST_NOT_ELIGIBLE", "지금은 보호소 코드를 사용할 수 없는 예약입니다. 예약 상태를 확인해 주세요.")


def retryable():
    return ContractError(409, "WALK_REQUEST_RETRYABLE", "같은 내용으로 다시 시도해 주세요.")


def invalid_format():
    return ContractError(422, "INVALID_SHELTER_CODE_FORMAT", "6자리 숫자 코드를 입력해 주세요.", {"field": "shelter_code"})


def invalid_request_id():
    return ContractError(422, "INVALID_WALK_REQUEST_ID", "예약 정보를 확인해 주세요.", {"field": "request_id"})


def invalid_code():
    return ContractError(422, "SHELTER_CODE_INVALID", "코드가 맞지 않거나 만료됐어요.")


def rate_limited(seconds):
    seconds = max(1, seconds)
    return ContractError(429, "SHELTER_CODE_RATE_LIMITED", "잠시 후 다시 입력해 주세요.",
                         {"retry_after_seconds": seconds}, headers={"Retry-After": str(seconds)})


def code_key():
    key = os.getenv("SHELTER_CODE_HMAC_KEY") or ""
    if len(key) < HMAC_KEY_MIN_LENGTH:
        raise ShelterCodeNotConfigured("SHELTER_CODE_HMAC_KEY must be set (at least 32 characters)")
    return key.encode()


def code_digest(key, request_id, code):
    return hmac.new(key, f"{request_id}:{code}".encode(), hashlib.sha256).hexdigest()


def utc_string(value):
    return as_utc(value).isoformat()


def contract_errors(call):
    """Run a shared walk-request helper and translate its `detail` errors to this contract."""
    try:
        return call()
    except HTTPException as exc:
        if exc.status_code == 403:
            raise not_allowed() from None
        if exc.status_code == 404:
            raise not_found() from None
        if exc.status_code in (409, 422):
            raise not_eligible() from None
        raise
    except ContractError as exc:
        if exc.status_code in (409, 422):
            raise not_eligible() from None
        raise


def lock_reservation(db, actor, request_id, action):
    request, dog, volunteer = contract_errors(lambda: locked_request(db, request_id, actor, action))
    return request, dog, volunteer


def ensure_code_window(db, request, dog, volunteer, now):
    """APPROVED, start <= now < end, no Walk of any status, and the start eligibility rules."""
    if (request.status != "APPROVED" or not as_utc(request.requested_start_at) <= now < as_utc(request.requested_end_at)
            or has_walk_record(db, request)):
        raise not_eligible()
    contract_errors(lambda: validate_linked_eligibility(db, request, dog, volunteer, require_future=False))


def current_issuance(db, request_id):
    return (db.query(ShelterCodeIssuance)
            .filter(ShelterCodeIssuance.walk_request_id == request_id, ShelterCodeIssuance.revoked_at.is_(None))
            .populate_existing().one_or_none())


def issue_code(db, actor, request_id, now):
    if actor.role not in (UserRole.SHELTER, UserRole.ADMIN):
        raise not_allowed()
    key = code_key()
    request, dog, volunteer = lock_reservation(db, actor, request_id, "ISSUE_CODE")
    ensure_code_window(db, request, dog, volunteer, now)
    previous = current_issuance(db, request.id)
    if previous is not None:
        previous.revoked_at = now  # Its verification, if any, can no longer start a walk.
        db.flush()
    code = f"{secrets.randbelow(10 ** 6):06d}"
    issuance = ShelterCodeIssuance(
        walk_request_id=request.id, volunteer_user_id=request.user_id, issued_by=actor.id,
        code_digest=code_digest(key, request.id, code), issued_at=now,
        expires_at=min(now + CODE_TTL, as_utc(request.requested_end_at)),
    )
    db.add(issuance)
    db.flush()
    return {"request_id": request.id, "shelter_code": code, "expires_at": utc_string(issuance.expires_at)}


@dataclass
class VerifyOutcome:
    data: dict | None = None
    error: ContractError | None = None


def lock_volunteer(db, actor):
    """Contract-format counterpart of require_active_writer(): users row first, re-read status."""
    if actor.role != UserRole.USER:
        raise not_allowed()
    ensure_active(actor)
    locked = lock_user(db, actor.id, for_update=True, nowait=True)
    if locked is None:
        raise contract_unauthorized("USER_NOT_FOUND")
    ensure_active(locked)


def verify_code(db, actor, request_id, raw_code, now):
    """Return the outcome; failures that changed the counter must be committed by the caller.

    Order: role and account status (403) -> code format (422) -> reservation (403/404/409)
    -> rate limit (429) -> comparison. Only the comparison changes the failure count.
    """
    if actor.role != UserRole.USER:
        raise not_allowed()
    ensure_active(actor)
    if not isinstance(raw_code, str) or CODE_PATTERN.fullmatch(raw_code) is None:
        raise invalid_format()
    key = code_key()
    lock_volunteer(db, actor)
    request, dog, volunteer = lock_reservation(db, actor, request_id, "START")
    ensure_code_window(db, request, dog, volunteer, now)
    attempt = (db.query(ShelterCodeAttempt).filter_by(walk_request_id=request.id, user_id=actor.id)
               .populate_existing().one_or_none())
    if attempt is None:
        attempt = ShelterCodeAttempt(walk_request_id=request.id, user_id=actor.id, failure_count=0, updated_at=now)
        db.add(attempt)
    if attempt.locked_until is not None:
        remaining = (as_utc(attempt.locked_until) - now).total_seconds()
        if remaining > 0:
            return VerifyOutcome(error=rate_limited(math.ceil(remaining)))
        attempt.failure_count, attempt.locked_until = 0, None  # The block is over: start a new count.
    issuance = current_issuance(db, request.id)
    valid = (issuance is not None and issuance.volunteer_user_id == actor.id and issuance.used_at is None
             and now < as_utc(issuance.expires_at)
             and hmac.compare_digest(issuance.code_digest, code_digest(key, request.id, raw_code)))
    attempt.updated_at = now
    if not valid:
        attempt.failure_count += 1
        if attempt.failure_count >= FAILURE_LIMIT:
            attempt.locked_until = now + LOCK_DURATION
            return VerifyOutcome(error=rate_limited(int(LOCK_DURATION.total_seconds())))
        return VerifyOutcome(error=invalid_code())
    if issuance.verified_at is None:
        issuance.verified_at = now  # A repeated valid submission returns the same verification.
    attempt.failure_count, attempt.locked_until = 0, None
    return VerifyOutcome(data={"request_id": request.id, "verified_until": utc_string(issuance.expires_at)})


def usable_verification(db, request, volunteer_id, now):
    """The current, verified, unused and unexpired issuance POST /walks may consume, or None."""
    issuance = current_issuance(db, request.id)
    if (issuance is None or issuance.volunteer_user_id != volunteer_id or issuance.verified_at is None
            or issuance.used_at is not None or now >= as_utc(issuance.expires_at)):
        return None
    return issuance
