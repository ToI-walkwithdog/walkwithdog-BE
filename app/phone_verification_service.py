"""SMS phone verification for onboarding Step 1 (Issue #40) and profile phone change (#103).

Policy (Notion, Issue #40): 6-digit CSPRNG code valid 300 s, resend after 60 s,
5 failed checks per request, saving allowed for 15 min after success, at most
5 sends per account and per number per Asia/Seoul day. Only the latest request of
an account is usable; sending again makes older ones unusable.

Registration is never disclosed by sending (Issue #110). A request for a number that
another account has registered is accepted exactly like any other: it takes a delivery
slot, is charged to both daily counters and the resend wait, and answers the same 200.
It is stored as SUPPRESSED: its code is generated, hashed and thrown away, no provider is
called, and it can never be verified. Ownership-proven saves still answer
PHONE_ALREADY_REGISTERED, because only the number's owner can reach them.

Request path (each transaction short):
  slot     take one delivery slot of the bounded pool, or 503 before anything is written
  T1       lock users FOR UPDATE, re-check status and pledge (or Step 1), charge the
           account and number counters (atomic upserts; the number row lock serializes
           one number across accounts), then the resend waits, then registration;
           insert PENDING or SUPPRESSED, commit. Any refusal rolls back the charge.
  answer   200 {verification_id, expires_in_seconds, resend_after_seconds}: the send was
           ACCEPTED by the server, not delivered. PENDING is handed to the pool.
Delivery job (own session, no transaction open while the provider is called):
  T2       lock users FOR SHARE, then the request FOR UPDATE (the users -> phone_verifications
           order of every other path); send only if it still exists, is PENDING, unexpired,
           the account's latest and the account is ACTIVE: PENDING -> SENDING, commit.
           Otherwise PENDING -> FAILED without calling the provider.
  call     provider.send() outside any transaction. Never resent automatically.
  T3       conditional on SENDING: SENT | FAILED | UNKNOWN, or SENT whenever the request was
           verified meanwhile (the matching code proves delivery). Verification fields are
           never touched, so a late provider answer cannot undo a verification.
Confirm (database only, no provider call): a wrong code answers 422 and counts a failure
whatever the delivery status; a matching code verifies SENDING, SENT and UNKNOWN requests;
PENDING, FAILED and SUPPRESSED never verify.

Purpose (Issue #103): each request records ONBOARDING or PROFILE_PHONE_CHANGE, set by the
endpoint. A proof is only usable for its own purpose. The latest-request rule, the resend
wait and the daily limits stay per account and per number across both purposes.

Secrets: PHONE_VERIFICATION_HMAC_KEY (at least 32 characters) signs codes and number
keys. Codes live in memory only: never stored, returned or logged; numbers are never logged.

Mock SMS test environment (Issue #124): with SMS_MOCK_FIXED_CODE (see app.sms_provider) every
request's code is that value instead of a random one; it is stored, bound to the request, as an
HMAC like any code. It proves nothing about owning a number, so in that mode only the internal
synthetic test numbers 010-0000-XXXX are accepted: others answer 422 before a slot is taken or
anything is written, and while the setting has any value no proof for another number can be
saved. Every rule above (async delivery, SUPPRESSED, limits, expiry, attempts) is unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import threading
import uuid

from sqlalchemy import and_, case, or_, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.account_status import ensure_active, is_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.models import PhoneVerification, PhoneVerificationDailyCount, VolunteerOnboardingBasic
from app.onboarding_common import as_utc, ensure_pledge_completed, kst_date, next_kst_midnight, utc_now
from app.sms_provider import (SmsProvider, SmsProviderUnavailable, fixed_code_configured, is_synthetic_test_number,
                              mock_fixed_code)

logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 300
RESEND_WAIT_SECONDS = 60
MAX_FAILED_ATTEMPTS = 5
VERIFIED_SAVE_WINDOW = timedelta(minutes=15)
DAILY_SEND_LIMIT = 5
HMAC_KEY_MIN_LENGTH = 32

PENDING, SENDING, SENT, UNKNOWN, FAILED = "PENDING", "SENDING", "SENT", "UNKNOWN", "FAILED"
SUPPRESSED = "SUPPRESSED"  # registered to another account: accepted, never sent, never verifiable (#110)
VERIFIABLE_STATUSES = (SENDING, SENT, UNKNOWN)  # a matching code proves delivery for these
DISPATCH_WORKERS, DISPATCH_QUEUE = 2, 20  # initial limits of the in-process delivery pool (#110)
PURPOSE_ONBOARDING, PURPOSE_PROFILE = "ONBOARDING", "PROFILE_PHONE_CHANGE"
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


def synthetic_test_number_required() -> ContractError:
    return _error(422, "INVALID_PHONE_NUMBER", "테스트 환경에서는 010-0000으로 시작하는 테스트 번호만 사용할 수 있습니다.",
                  {"field": "phone_number"})


def proof_number_allowed(phone_e164: str) -> bool:
    """False for a number outside the synthetic test range while SMS_MOCK_FIXED_CODE has any
    value, so no proof (even one made before the mode was turned on) saves such a number."""
    return not fixed_code_configured() or is_synthetic_test_number(phone_e164)


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


def profile_basic_info_required() -> ContractError:
    return _error(409, "PROFILE_BASIC_INFO_REQUIRED", "기본 정보 입력을 먼저 완료해 주세요.")


def reserve_send(db: Session, user_id: int, phone_e164: str, now: datetime, key: bytes,
                 purpose: str = PURPOSE_ONBOARDING, *, fixed_code: str | None = None):
    """T1. Returns (verification_id, code, suppressed). The code exists only in memory; it is
    `fixed_code` in the mock test mode (#124), otherwise random.

    Every request runs the same checks in the same order; registration is looked at only
    after the counters and resend waits, so it never changes the answer (Issue #110)."""
    try:
        user = lock_user(db, user_id, for_update=True)  # serializes this account's sends
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        ensure_active(user)
        if purpose == PURPOSE_ONBOARDING:
            ensure_pledge_completed(db, user)
        elif db.get(VolunteerOnboardingBasic, user_id) is None:  # a phone change needs Step 1 first
            raise profile_basic_info_required()
        day = kst_date(now)
        # Charged first: the number counter row lock serializes concurrent requests for one
        # number across accounts, so the resend wait below sees the other's committed request.
        # Counted when reserved: UNKNOWN, FAILED and SUPPRESSED sends count too.
        user_slot = _take_daily_slot(db, "USER", str(user_id), day, DAILY_SEND_LIMIT)
        phone_slot = _take_daily_slot(db, "PHONE", phone_subject(key, phone_e164), day, DAILY_SEND_LIMIT)
        window_start = now - timedelta(seconds=RESEND_WAIT_SECONDS)
        for condition in (PhoneVerification.user_id == user_id, PhoneVerification.phone_e164 == phone_e164):
            recent = latest_request(db, condition, PhoneVerification.created_at > window_start)
            if recent is not None:  # same precedence as before: the resend wait first
                raise rate_limited((as_utc(recent.created_at) - window_start).total_seconds())
        if not (user_slot and phone_slot):
            raise rate_limited((next_kst_midnight(now) - as_utc(now)).total_seconds())
        suppressed = registered_to_other_user(db, phone_e164, user_id)
        verification_id = uuid.uuid4()
        code = fixed_code if fixed_code is not None else f"{secrets.randbelow(10 ** 6):06d}"
        db.add(PhoneVerification(
            id=verification_id, user_id=user_id, phone_e164=phone_e164,
            code_digest=code_digest(key, verification_id, code), send_status=SUPPRESSED if suppressed else PENDING,
            provider_request_key=verification_id, expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
            attempt_count=0, created_at=now, purpose=purpose))
        db.commit()  # any refusal above rolled back the counters with everything else
        return verification_id, code, suppressed
    except Exception:
        db.rollback()
        raise


class DeliverySlot:
    """One taken place of the pool. release() frees it once; later calls do nothing, so the
    job and the request path can both release it safely on every path (Issue #110)."""

    def __init__(self, dispatcher: "SmsDispatcher"):
        self._dispatcher = dispatcher
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._dispatcher._free()


class SmsDispatcher:
    """Bounded in-process delivery pool (Issue #110): `workers` threads and at most `queue`
    waiting jobs. A request takes a slot before anything is written (only the settings and the
    number itself are checked first, #124), so overload answers the same 503 whatever the number is. The job releases its slot when it ends; the request path
    releases it when nothing was queued (idempotent). inline=True runs jobs at once (tests).
    A graceful shutdown runs the queued jobs first; a forced kill loses them."""

    def __init__(self, workers: int = DISPATCH_WORKERS, queue: int = DISPATCH_QUEUE, *, inline: bool = False):
        self.capacity = workers + queue
        self._in_use = 0
        self._lock = threading.Lock()
        self._inline = inline
        self._executor = None if inline else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sms-delivery")

    def available(self) -> int:
        with self._lock:
            return self.capacity - self._in_use

    def try_acquire(self) -> DeliverySlot | None:
        with self._lock:
            if self._in_use >= self.capacity:
                return None
            self._in_use += 1
            return DeliverySlot(self)

    def _free(self) -> None:
        with self._lock:
            if self._in_use <= 0:
                raise RuntimeError("delivery slot freed twice")
            self._in_use -= 1

    def submit(self, job, slot: DeliverySlot) -> None:
        """Queue `job` on `slot`; the job releases it when it ends. Raises if nothing was queued."""
        def run():
            try:
                job()
            except Exception as exc:  # the type only: a message could carry a number
                logger.warning("SMS delivery job failed (%s)", type(exc).__name__)
            finally:
                slot.release()

        if self._inline:
            run()
        else:
            self._executor.submit(run)


_dispatcher: SmsDispatcher | None = None
_dispatcher_lock = threading.Lock()


def get_sms_dispatcher() -> SmsDispatcher:
    """Dependency: the process-wide delivery pool, created on first use."""
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = SmsDispatcher()
        return _dispatcher


def _claim_for_delivery(db: Session, verification_id, now: datetime) -> bool:
    """T2: PENDING -> SENDING only if the request may still be sent; otherwise -> FAILED."""
    try:
        user_id = db.query(PhoneVerification.user_id).filter(PhoneVerification.id == verification_id).scalar()
        if user_id is None:
            db.rollback()
            return False  # deleted (sign-up exit): nothing to send
        user = lock_user(db, user_id)  # FOR SHARE first: the users -> phone_verifications order
        verification = (db.query(PhoneVerification).filter(PhoneVerification.id == verification_id)
                        .populate_existing().with_for_update().one_or_none())
        if verification is None or verification.send_status != PENDING:
            db.rollback()
            return False  # gone, or dispatched already
        if (user is None or not is_active(user) or as_utc(now) >= as_utc(verification.expires_at)
                or not is_latest_for_user(db, verification)):
            verification.send_status, verification.last_checked_at = FAILED, now
            db.commit()
            logger.info("SMS delivery skipped: request no longer sendable")
            return False
        verification.send_status = SENDING
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def _record_result(db: Session, verification_id, status: str, now: datetime, **ids) -> None:
    """T3: conditional on SENDING. A request verified meanwhile is SENT whatever the provider
    answered; verification fields are never written here."""
    try:
        db.execute(update(PhoneVerification)
                   .where(PhoneVerification.id == verification_id, PhoneVerification.send_status == SENDING)
                   .values(send_status=case((PhoneVerification.verified_at.isnot(None), SENT), else_=status),
                           last_checked_at=now, **ids))
        db.commit()
    except Exception:
        db.rollback()
        raise


def deliver_verification(bind, provider: SmsProvider, verification_id, phone_e164: str, code: str, clock) -> None:
    """The delivery job: its own session on the request's database, no automatic resend."""
    with sessionmaker(bind=bind)() as db:
        if not _claim_for_delivery(db, verification_id, clock()):
            return
        text = f"[같이걷개] 인증번호는 {code}입니다. 5분 안에 입력해 주세요."
        del code
        ids = {}
        try:
            result = provider.send(to_e164=phone_e164, text=text, request_key=str(verification_id))
        except SmsProviderUnavailable:
            status = FAILED
        except Exception as exc:
            # Timeout, reset connection or a malformed reply: it may have been accepted.
            logger.warning("SMS send outcome unknown (%s)", type(exc).__name__)
            status = UNKNOWN
        else:
            status = SENT if result.accepted else FAILED
            if result.accepted:
                ids = {"provider_message_id": result.message_id, "provider_group_id": result.group_id}
        del text
        _record_result(db, verification_id, status, clock(), **ids)


def send_verification(db: Session, provider: SmsProvider | None, user_id: int, phone_e164: str, now: datetime,
                      purpose: str = PURPOSE_ONBOARDING, *, dispatcher: SmsDispatcher | None = None,
                      clock=None) -> dict:
    """Accept a send request. 200 means accepted by the server, not delivered (Issue #110)."""
    if provider is None:  # nothing reserved, nothing counted, whatever the number is
        raise delivery_unavailable()
    try:
        fixed_code = mock_fixed_code()
    except SmsProviderUnavailable:  # a fixed code where it may not be used: same 503, nothing written
        raise delivery_unavailable() from None
    key = hmac_key()  # fail closed before any write
    if fixed_code is not None and not is_synthetic_test_number(phone_e164):
        raise synthetic_test_number_required()  # decided by the number alone, before any write
    dispatcher = dispatcher or get_sms_dispatcher()
    slot = dispatcher.try_acquire()
    if slot is None:  # overloaded: decided before the number is looked at
        raise delivery_unavailable()
    queued = False
    try:
        verification_id, code, suppressed = reserve_send(db, user_id, phone_e164, now, key, purpose,
                                                         fixed_code=fixed_code)
        if not suppressed:
            bind, job_clock = db.get_bind(), clock or utc_now
            job = (lambda code=code: deliver_verification(bind, provider, verification_id, phone_e164, code,
                                                           job_clock))
            try:
                dispatcher.submit(job, slot)
                queued = True
            except Exception as exc:  # the row stays PENDING; the user asks again after 60 s
                logger.warning("SMS delivery could not be queued (%s)", type(exc).__name__)
            del job
        del code, fixed_code
    finally:
        if not queued:
            slot.release()
    return {"verification_id": str(verification_id), "expires_in_seconds": CODE_TTL_SECONDS,
            "resend_after_seconds": RESEND_WAIT_SECONDS}


def parse_verification_id(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def confirm_code(db: Session, user_id: int, verification_id: uuid.UUID, code: str, now: datetime,
                 purpose: str = PURPOSE_ONBOARDING) -> dict:
    """T4, database only. The answer depends on the code, the attempts, expiry and the
    latest-request rule, never on the delivery status (Issue #110)."""
    not_found = _error(404, "VERIFICATION_NOT_FOUND", "인증 요청을 찾을 수 없습니다.")
    verification = db.get(PhoneVerification, verification_id)
    # Someone else's request, or one made for the other purpose, is "not found" too (#103).
    if verification is None or verification.user_id != user_id or verification.purpose != purpose:
        raise not_found
    key = hmac_key()
    invalid = _error(422, "VERIFICATION_CODE_INVALID", "인증번호가 일치하지 않습니다.", {"field": "code"})
    try:
        verification = (db.query(PhoneVerification).filter(PhoneVerification.id == verification_id)
                        .populate_existing().with_for_update().one_or_none())
        if verification is None:
            raise not_found
        matches = hmac.compare_digest(verification.code_digest, code_digest(key, verification.id, code))
        if verification.consumed_at is not None:
            raise _error(409, "VERIFICATION_ALREADY_CONSUMED", "새 인증번호를 요청해 주세요.")
        expired = _error(410, "VERIFICATION_EXPIRED", "인증번호가 만료되었습니다.")
        if not is_latest_for_user(db, verification):  # a newer request replaced it
            raise expired
        if verification.verified_at is not None:
            # A retry after a lost success response: same answer, no failure counted.
            if not matches:
                raise invalid
            if as_utc(now) >= as_utc(verification.verified_valid_until):
                raise expired
            db.commit()
            return {"verified": True, "verified_at": as_utc(verification.verified_at).isoformat()}
        if as_utc(now) >= as_utc(verification.expires_at):
            raise expired
        if verification.attempt_count >= MAX_FAILED_ATTEMPTS:
            raise _error(429, "VERIFICATION_ATTEMPTS_EXCEEDED", "인증 시도 횟수를 초과했습니다.")
        if not matches or verification.send_status not in VERIFIABLE_STATUSES:
            # A wrong code, or a request that was never sent (PENDING, FAILED, SUPPRESSED):
            # the same answer either way, and the failure counts.
            verification.attempt_count += 1
            db.commit()
            raise invalid
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
