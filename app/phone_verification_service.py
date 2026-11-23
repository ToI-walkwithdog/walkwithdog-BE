"""SMS phone verification for onboarding Step 1 (Issue #40).

Policy (Notion, Issue #40): 6-digit CSPRNG code valid 300 s, resend after 60 s,
5 failed checks per request, saving Step 1 allowed for 15 min after success, at most
5 sends per account and per number per Asia/Seoul day. Only the latest request of
an account is usable; sending again makes older ones unusable.

Transactions (each short; none is open while the SMS provider is called):
  T1 reserve   lock users row FOR UPDATE, re-check status and pledge, resend wait,
               daily counters (atomic upsert), insert PENDING, commit
  T2 claim     PENDING -> SENDING by conditional UPDATE, commit (one send per request)
  -- provider call, no transaction --
  T3 result    SENDING -> SENT | FAILED | UNKNOWN, commit
  T4 confirm   lock the request row FOR UPDATE, check, count a failure or mark verified, commit
An UNKNOWN outcome is never resent automatically. confirm() asks the provider once
more (outside any transaction) and only proceeds if the request is then SENT.

Secrets: PHONE_VERIFICATION_HMAC_KEY (at least 32 characters) signs codes and number
keys. Codes are never stored, returned or logged; numbers and codes are never logged.
"""
from datetime import datetime, timedelta
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import uuid

from sqlalchemy import and_, or_, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.account_status import ensure_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.models import PhoneVerification, PhoneVerificationDailyCount, VolunteerOnboardingBasic
from app.onboarding_common import as_utc, ensure_pledge_completed, kst_date, next_kst_midnight
from app.sms_provider import SmsProvider, SmsProviderUnavailable

logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 300
RESEND_WAIT_SECONDS = 60
MAX_FAILED_ATTEMPTS = 5
VERIFIED_SAVE_WINDOW = timedelta(minutes=15)
DAILY_SEND_LIMIT = 5
HMAC_KEY_MIN_LENGTH = 32

PENDING, SENDING, SENT, UNKNOWN, FAILED = "PENDING", "SENDING", "SENT", "UNKNOWN", "FAILED"
PHONE_INPUT = re.compile(r"010-?\d{4}-?\d{4}")


class PhoneVerificationNotConfigured(RuntimeError):
    """PHONE_VERIFICATION_HMAC_KEY is missing or too short; nothing is stored or sent."""


def _error(status, code, message, data=None, headers=None):
    return ContractError(status, code, message, data or {}, headers=headers)


def normalize_phone(value) -> str:
    """Domestic 010 mobile numbers only; digits with optional hyphens -> +8210XXXXXXXX."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _error(422, "PHONE_NUMBER_REQUIRED", "전화번호를 입력해 주세요.", {"field": "phone_number"})
    if not isinstance(value, str) or not PHONE_INPUT.fullmatch(value.strip()):
        raise _error(422, "INVALID_PHONE_NUMBER", "올바른 전화번호를 입력해 주세요.", {"field": "phone_number"})
    return "+82" + value.strip().replace("-", "")[1:]


def mask_phone(phone_e164: str) -> str:
    local = "0" + phone_e164[3:]
    return f"{local[:3]}-****-{local[-4:]}"


def hmac_key() -> bytes:
    key = os.getenv("PHONE_VERIFICATION_HMAC_KEY") or ""
    if len(key) < HMAC_KEY_MIN_LENGTH:
        raise PhoneVerificationNotConfigured("PHONE_VERIFICATION_HMAC_KEY must be set (at least 32 characters)")
    return key.encode()


def code_digest(key: bytes, verification_id: uuid.UUID, code: str) -> str:
    # Bound to the request ID, so a digest is useless for any other request.
    return hmac.new(key, f"{verification_id}:{code}".encode(), hashlib.sha256).hexdigest()


def phone_subject(key: bytes, phone_e164: str) -> str:
    """Counter key for a number: an HMAC, so the counters table holds no phone number."""
    return hmac.new(key, f"phone:{phone_e164}".encode(), hashlib.sha256).hexdigest()


def rate_limited(retry_after: float) -> ContractError:
    seconds = max(1, math.ceil(retry_after))
    return _error(429, "PHONE_VERIFICATION_RATE_LIMITED", "잠시 후 다시 요청해 주세요.",
                  {"retry_after_seconds": seconds}, headers={"Retry-After": str(seconds)})


def delivery_unavailable() -> ContractError:
    return _error(503, "SMS_DELIVERY_UNAVAILABLE", "인증번호 발송이 지연되고 있습니다.")


def phone_already_registered() -> ContractError:
    return _error(409, "PHONE_ALREADY_REGISTERED", "이미 등록된 전화번호입니다.", {"field": "phone_number"})


def registered_to_other_user(db: Session, phone_e164: str, user_id: int) -> bool:
    return db.query(VolunteerOnboardingBasic.user_id).filter(
        VolunteerOnboardingBasic.phone_e164 == phone_e164,
        VolunteerOnboardingBasic.user_id != user_id).first() is not None


def latest_request(db: Session, *conditions) -> PhoneVerification | None:
    return (db.query(PhoneVerification).filter(*conditions)
            .order_by(PhoneVerification.created_at.desc(), PhoneVerification.id.desc()).first())


def is_latest_for_user(db: Session, verification: PhoneVerification) -> bool:
    newest = latest_request(db, PhoneVerification.user_id == verification.user_id)
    return newest is not None and newest.id == verification.id


def _take_daily_slot(db: Session, scope: str, subject: str, day, limit: int) -> bool:
    """Atomically add one send to today's counter unless it is already at the limit."""
    insert = postgresql_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    table = PhoneVerificationDailyCount.__table__
    statement = (insert(table).values(scope=scope, subject=subject, kst_date=day, send_count=1)
                 .on_conflict_do_update(index_elements=["scope", "subject", "kst_date"],
                                        set_={"send_count": table.c.send_count + 1},
                                        where=table.c.send_count < limit)
                 .returning(table.c.send_count))
    return db.execute(statement).first() is not None


def reserve_send(db: Session, user_id: int, phone_e164: str, now: datetime, key: bytes):
    """T1. Returns (verification_id, code). The code exists only in memory until sent."""
    try:
        user = lock_user(db, user_id, for_update=True)  # serializes this account's sends
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        ensure_active(user)
        ensure_pledge_completed(db, user)
        if registered_to_other_user(db, phone_e164, user_id):
            raise phone_already_registered()
        window_start = now - timedelta(seconds=RESEND_WAIT_SECONDS)
        for condition in (PhoneVerification.user_id == user_id, PhoneVerification.phone_e164 == phone_e164):
            recent = latest_request(db, condition, PhoneVerification.created_at > window_start)
            if recent is not None:
                raise rate_limited((as_utc(recent.created_at) - window_start).total_seconds())
        day = kst_date(now)
        # Counted when reserved: UNKNOWN and FAILED sends count too (see Issue #40 open items).
        if not (_take_daily_slot(db, "USER", str(user_id), day, DAILY_SEND_LIMIT)
                and _take_daily_slot(db, "PHONE", phone_subject(key, phone_e164), day, DAILY_SEND_LIMIT)):
            raise rate_limited((next_kst_midnight(now) - as_utc(now)).total_seconds())
        verification_id = uuid.uuid4()
        code = f"{secrets.randbelow(10 ** 6):06d}"
        db.add(PhoneVerification(
            id=verification_id, user_id=user_id, phone_e164=phone_e164,
            code_digest=code_digest(key, verification_id, code), send_status=PENDING,
            provider_request_key=verification_id, expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
            attempt_count=0, created_at=now))
        db.commit()
        return verification_id, code
    except Exception:
        db.rollback()
        raise


def _move_status(db: Session, verification_id, from_status: str, **values) -> bool:
    """Conditional UPDATE in its own short transaction; True if this call changed the row."""
    try:
        result = db.execute(update(PhoneVerification)
                            .where(PhoneVerification.id == verification_id,
                                   PhoneVerification.send_status == from_status)
                            .values(**values))
        db.commit()
        return result.rowcount == 1
    except Exception:
        db.rollback()
        raise


def send_verification(db: Session, provider: SmsProvider | None, user_id: int, phone_e164: str, now: datetime) -> dict:
    if provider is None:  # nothing reserved, nothing counted
        raise delivery_unavailable()
    key = hmac_key()  # fail closed before any write
    verification_id, code = reserve_send(db, user_id, phone_e164, now, key)
    if not _move_status(db, verification_id, PENDING, send_status=SENDING):  # T2
        raise RuntimeError("verification request was claimed twice")
    text = f"[같이걷개] 인증번호는 {code}입니다. 5분 안에 입력해 주세요."
    try:
        result = provider.send(to_e164=phone_e164, text=text, request_key=str(verification_id))
    except SmsProviderUnavailable:
        _move_status(db, verification_id, SENDING, send_status=FAILED, last_checked_at=now)
        raise delivery_unavailable() from None
    except Exception as exc:
        # Timeout, reset connection or a malformed reply: it may have been accepted.
        logger.warning("SMS send outcome unknown (%s)", type(exc).__name__)
        _move_status(db, verification_id, SENDING, send_status=UNKNOWN, last_checked_at=now)
        raise delivery_unavailable() from None
    del code, text
    if not result.accepted:
        _move_status(db, verification_id, SENDING, send_status=FAILED, last_checked_at=now)
        raise delivery_unavailable()
    _move_status(db, verification_id, SENDING, send_status=SENT, last_checked_at=now,
                 provider_message_id=result.message_id, provider_group_id=result.group_id)
    return {"verification_id": str(verification_id), "expires_in_seconds": CODE_TTL_SECONDS,
            "resend_after_seconds": RESEND_WAIT_SECONDS}


def reconcile_unknown(db: Session, provider: SmsProvider | None, verification: PhoneVerification, now: datetime) -> None:
    """Ask the provider about an UNKNOWN send. Never resends. No transaction is open meanwhile."""
    db.commit()  # end the read transaction before the external call
    if provider is None:
        return
    try:
        accepted = provider.lookup(request_key=str(verification.provider_request_key),
                                   message_id=verification.provider_message_id,
                                   group_id=verification.provider_group_id)
    except Exception as exc:
        logger.warning("SMS lookup failed (%s)", type(exc).__name__)
        accepted = None
    if accepted is None:
        _move_status(db, verification.id, UNKNOWN, last_checked_at=now)
    else:
        _move_status(db, verification.id, UNKNOWN, send_status=SENT if accepted else FAILED, last_checked_at=now)


def parse_verification_id(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def confirm_code(db: Session, provider: SmsProvider | None, user_id: int, verification_id: uuid.UUID,
                 code: str, now: datetime) -> dict:
    not_found = _error(404, "VERIFICATION_NOT_FOUND", "인증 요청을 찾을 수 없습니다.")
    verification = db.get(PhoneVerification, verification_id)
    if verification is None or verification.user_id != user_id:  # someone else's is "not found" too
        raise not_found
    if verification.send_status == UNKNOWN:
        reconcile_unknown(db, provider, verification, now)
    key = hmac_key()
    try:
        verification = (db.query(PhoneVerification).filter(PhoneVerification.id == verification_id)
                        .populate_existing().with_for_update().one_or_none())  # T4
        if verification is None:
            raise not_found
        matches = hmac.compare_digest(verification.code_digest, code_digest(key, verification.id, code))
        if verification.consumed_at is not None:
            raise _error(409, "VERIFICATION_ALREADY_CONSUMED", "새 인증번호를 요청해 주세요.")
        if verification.send_status != SENT:
            raise _error(409, "VERIFICATION_NOT_SENT", "인증번호를 다시 요청해 주세요.")
        expired = _error(410, "VERIFICATION_EXPIRED", "인증번호가 만료되었습니다.")
        if not is_latest_for_user(db, verification):  # a newer request replaced it
            raise expired
        if verification.verified_at is not None:
            # A retry after a lost success response: same answer, no failure counted.
            if not matches:
                raise _error(422, "VERIFICATION_CODE_INVALID", "인증번호가 일치하지 않습니다.", {"field": "code"})
            if as_utc(now) >= as_utc(verification.verified_valid_until):
                raise expired
            db.commit()
            return {"verified": True, "verified_at": as_utc(verification.verified_at).isoformat()}
        if as_utc(now) >= as_utc(verification.expires_at):
            raise expired
        if verification.attempt_count >= MAX_FAILED_ATTEMPTS:
            raise _error(429, "VERIFICATION_ATTEMPTS_EXCEEDED", "인증 시도 횟수를 초과했습니다.")
        if not matches:
            verification.attempt_count += 1
            db.commit()  # the failure counts even though the request is answered with 422
            raise _error(422, "VERIFICATION_CODE_INVALID", "인증번호가 일치하지 않습니다.", {"field": "code"})
        verification.verified_at = now
        verification.verified_valid_until = now + VERIFIED_SAVE_WINDOW
        db.commit()
        return {"verified": True, "verified_at": as_utc(now).isoformat()}
    except Exception:
        db.rollback()
        raise


def purge_phone_verifications(db: Session, now: datetime, *, apply: bool) -> dict:
    """Delete requests that can no longer be used, and counters older than yesterday.

    Unusable: consumed, verified but past the 15-minute save window, or never verified
    and past the 5-minute code expiry. Running this at least once a day keeps such rows
    under 24 hours old. How it is scheduled is still open (Issue #40).
    """
    unusable = or_(PhoneVerification.consumed_at.isnot(None),
                   and_(PhoneVerification.verified_at.isnot(None), PhoneVerification.verified_valid_until <= now),
                   and_(PhoneVerification.verified_at.is_(None), PhoneVerification.expires_at <= now))
    old_counters = PhoneVerificationDailyCount.kst_date < kst_date(now) - timedelta(days=1)
    counts = {"phone_verifications": db.query(PhoneVerification).filter(unusable).count(),
              "phone_verification_daily_counts": db.query(PhoneVerificationDailyCount).filter(old_counters).count()}
    if apply:
        try:
            db.query(PhoneVerification).filter(unusable).delete(synchronize_session=False)
            db.query(PhoneVerificationDailyCount).filter(old_counters).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return counts
