"""Onboarding Step 1 basic information (Issue #40): GET and PUT /users/me/onboarding/basic-info.

Save order: field rules (422) -> an existing Step 1 is replayed (same input 200,
different input 409) -> future date and age range (under 14 switches the account to
AGE_REVIEW_REQUIRED in its own committed transaction, then 422; over 120 is 422 only)
-> verification request -> one transaction:
   lock users row FOR SHARE and re-check status and pledge
   -> lock the verification row FOR UPDATE
   -> insert volunteer_onboarding_basics and set phone_verifications.consumed_at
   -> commit. Any failure rolls both back.
An existing Step 1 is checked before the age rule so a retried or mismatched request
from a completed account can never change its status.

The birth date of a refused request is never stored. users.phone is not touched.
"""
from datetime import date, datetime
import re
import unicodedata

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.account_status import change_to_age_review, ensure_active, lock_user
from app.api_contract import REQUIRED_FIELD_MESSAGE, ContractError
from app.auth import contract_unauthorized
from app.models import PhoneVerification, User, VolunteerOnboardingBasic
from app.onboarding_common import (age_years, as_utc, ensure_pledge_completed,
                                   first_incomplete_step, kst_date)
from app.phone_verification_service import (is_latest_for_user, mask_phone, parse_verification_id,
                                            phone_already_registered, registered_to_other_user)

FULL_NAME_MIN_LENGTH, FULL_NAME_MAX_LENGTH = 2, 10
MINIMUM_AGE, MAXIMUM_AGE = 14, 120
BIRTH_DATE_INPUT = re.compile(r"\d{4}-\d{2}-\d{2}")


def _required(field):
    return ContractError(422, "REQUIRED_FIELD", REQUIRED_FIELD_MESSAGE, {"field": field})


def invalid_birth_date():
    return ContractError(422, "INVALID_BIRTH_DATE", "생년월일을 확인해 주세요.", {"field": "birth_date"})


def normalize_full_name(value) -> str:
    """NFC, trim the ends, keep inner spaces, 2 to 10 characters, no control characters."""
    if not isinstance(value, str) or not value.strip():
        raise _required("full_name")
    name = unicodedata.normalize("NFC", value).strip()
    if not FULL_NAME_MIN_LENGTH <= len(name) <= FULL_NAME_MAX_LENGTH or not name.isprintable():
        raise ContractError(422, "INVALID_FULL_NAME", "이름을 확인해 주세요.", {"field": "full_name"})
    return name


def parse_birth_date(value) -> date:
    """YYYY-MM-DD and a real calendar date. The future and age checks need today's date."""
    if not isinstance(value, str) or not value.strip():
        raise _required("birth_date")
    if not BIRTH_DATE_INPUT.fullmatch(value.strip()):
        raise invalid_birth_date()
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise invalid_birth_date() from None


def require_verification_id(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _required("phone_verification_id")
    return value.strip()


def profile_data(basic: VolunteerOnboardingBasic, now: datetime) -> dict:
    return {"full_name": basic.full_name, "birth_date": basic.birth_date.isoformat(),
            "age_years": age_years(basic.birth_date, kst_date(now)),
            "phone_masked": mask_phone(basic.phone_e164),
            "phone_verified_at": as_utc(basic.phone_verified_at).isoformat(),
            "completed_at": as_utc(basic.completed_at).isoformat()}


def read_basic_info(db: Session, user_id: int, now: datetime) -> dict:
    """Read only: no write and no SMS."""
    basic = db.get(VolunteerOnboardingBasic, user_id)
    if basic is None:
        return {"step1_completed": False, "profile": None, "next_step": next_step(db, user_id)}
    return {"step1_completed": True, "profile": profile_data(basic, now), "next_step": next_step(db, user_id)}


def next_step(db: Session, user_id: int) -> str | None:
    """The shared first incomplete step (#45), not a fixed PET_EXPERIENCE: an account that has
    finished later steps is told so. The field keeps its Step 1 name `next_step` (Notion)."""
    user = db.get(User, user_id)
    if user is None:  # deleted by a concurrent sign-up exit
        raise contract_unauthorized("USER_NOT_FOUND")
    return first_incomplete_step(db, user)


def saved_response(user_id: int, basic: VolunteerOnboardingBasic, now: datetime, step: str | None) -> dict:
    return {"user_id": user_id, **profile_data(basic, now), "step1_completed": True, "next_step": step}


def _replay(db: Session, user_id: int, basic: VolunteerOnboardingBasic, full_name: str, birth: date,
            verification_id, now: datetime) -> dict:
    """Completed Step 1: the same request again gets the first result, anything else 409."""
    verification = db.get(PhoneVerification, verification_id) if verification_id else None
    same_phone = (verification is not None and verification.user_id == user_id
                  and verification.phone_e164 == basic.phone_e164)
    if basic.full_name == full_name and basic.birth_date == birth and same_phone:
        return saved_response(user_id, basic, now, next_step(db, user_id))
    raise ContractError(409, "BASIC_INFO_ALREADY_COMPLETED", "기본 정보가 이미 등록되었습니다.")


def save_basic_info(db: Session, user_id: int, full_name: str, birth: date, raw_verification_id: str,
                    now: datetime) -> dict:
    verification_id = parse_verification_id(raw_verification_id)
    existing = db.get(VolunteerOnboardingBasic, user_id)
    if existing is not None:
        return _replay(db, user_id, existing, full_name, birth, verification_id, now)

    today = kst_date(now)
    if birth > today:
        raise invalid_birth_date()
    age = age_years(birth, today)
    if age < MINIMUM_AGE:
        db.rollback()  # end the read transaction; the status change commits on its own
        change_to_age_review(db, user_id, now)
        raise ContractError(422, "AGE_BELOW_MINIMUM", "만 14세 이상부터 이용할 수 있습니다.",
                            {"field": "birth_date", "minimum_age": MINIMUM_AGE})
    if age > MAXIMUM_AGE:
        raise ContractError(422, "AGE_ABOVE_MAXIMUM", "생년월일을 다시 확인해 주세요.",
                            {"field": "birth_date", "maximum_age": MAXIMUM_AGE})

    not_found = ContractError(404, "VERIFICATION_NOT_FOUND", "전화번호 인증 정보를 찾을 수 없습니다.")
    if verification_id is None:
        raise not_found
    try:
        user = lock_user(db, user_id)  # FOR SHARE: waits for, or blocks, a status change
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        ensure_active(user)
        ensure_pledge_completed(db, user)
        verification = (db.query(PhoneVerification).filter(PhoneVerification.id == verification_id)
                        .populate_existing().with_for_update().one_or_none())
        if verification is None or verification.user_id != user_id:
            raise not_found
        # A concurrent request of this account may have saved while this one waited for
        # the lock; answer it as a replay instead of reporting the consumed request.
        saved = (db.query(VolunteerOnboardingBasic).filter(VolunteerOnboardingBasic.user_id == user_id)
                 .populate_existing().one_or_none())
        if saved is not None:
            db.rollback()
            return _replay(db, user_id, db.get(VolunteerOnboardingBasic, user_id), full_name, birth,
                           verification_id, now)
        if (verification.verified_at is None or verification.consumed_at is not None
                or as_utc(now) >= as_utc(verification.verified_valid_until)
                or not is_latest_for_user(db, verification)):
            raise ContractError(409, "PHONE_VERIFICATION_REQUIRED", "전화번호 인증을 완료해 주세요.",
                                {"field": "phone_verification_id"})
        if registered_to_other_user(db, verification.phone_e164, user_id):
            raise phone_already_registered()
        basic = VolunteerOnboardingBasic(user_id=user_id, full_name=full_name, birth_date=birth,
                                         phone_e164=verification.phone_e164,
                                         phone_verified_at=verification.verified_at,
                                         completed_at=now, updated_at=now)
        db.add(basic)
        verification.consumed_at = now
        db.commit()
        return saved_response(user_id, basic, now, next_step(db, user_id))
    except IntegrityError:
        db.rollback()
        # Lost a race: the same account saved first (primary key), or another account
        # registered the number first (UNIQUE phone_e164). Anything else stays a 500.
        existing = db.get(VolunteerOnboardingBasic, user_id)
        if existing is not None:
            return _replay(db, user_id, existing, full_name, birth, verification_id, now)
        verification = db.get(PhoneVerification, verification_id)
        if verification is not None and registered_to_other_user(db, verification.phone_e164, user_id):
            raise phone_already_registered() from None
        raise
    except Exception:
        db.rollback()
        raise
