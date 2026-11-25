"""Owner profile on /10-2 (Issue #103): nickname, full name and verified phone number.

GET  returns the caller's own values and profile_version.
PATCH changes any of the three in one transaction:
  field rules (422) -> lock users FOR UPDATE, re-check role and status (403)
  -> lock volunteer_onboarding_basics FOR UPDATE -> version check (409 PROFILE_VERSION_MISMATCH)
  -> a changed phone needs this account's latest, verified, unconsumed PROFILE_PHONE_CHANGE
     proof for the same number within its 15-minute window (locked FOR UPDATE)
  -> uniqueness of nickname and phone -> write, consume the proof, profile_version + 1 -> commit.
Any failure rolls everything back, so a proof is never consumed by a failed save. A no-op
(normalized values equal to the stored ones) answers 200 without a write or a version change.

Name and phone change need an existing Step 1 row; it is never created here. users.phone
(an older, unverified field) is neither read nor written. Logs carry field names only.
"""
from datetime import datetime
import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.account_rules import normalize_nickname
from app.account_status import ensure_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.basic_info_service import normalize_full_name
from app.models import PhoneVerification, User, UserRole, VolunteerOnboardingBasic
from app.onboarding_common import as_utc
from app.phone_verification_service import (PURPOSE_PROFILE, VERIFIABLE_STATUSES, is_latest_for_user, normalize_phone,
                                            parse_verification_id, phone_already_registered,
                                            profile_basic_info_required, registered_to_other_user)

logger = logging.getLogger(__name__)

PROFILE_FIELDS = ("nickname", "full_name", "phone_number")
PATCH_KEYS = {"expected_profile_version", *PROFILE_FIELDS, "phone_verification_id"}


def invalid_payload(field=None):
    return ContractError(422, "INVALID_PROFILE_PAYLOAD", "수정할 정보를 확인해 주세요.",
                         {"field": field} if field else {})


def not_allowed():
    return ContractError(403, "PROFILE_UPDATE_NOT_ALLOWED", "내 정보를 수정할 수 없는 계정입니다.")


def version_mismatch():
    return ContractError(409, "PROFILE_VERSION_MISMATCH", "다른 기기에서 정보가 변경되었습니다. 다시 확인해 주세요.")


def verification_required():
    return ContractError(409, "PHONE_VERIFICATION_REQUIRED", "새 휴대폰 번호 인증을 완료해 주세요.",
                         {"field": "phone_verification_id"})


def nickname_taken():
    return ContractError(409, "NICKNAME_ALREADY_EXISTS", "이미 사용 중인 닉네임입니다.", {"field": "nickname"})


def require_profile_user(user: User) -> None:
    """ACTIVE general users only. No pledge or onboarding completion is required (approved #103)."""
    if user.role != UserRole.USER:
        raise not_allowed()
    ensure_active(user)


def display_phone(phone_e164: str) -> str:
    local = "0" + phone_e164[3:]
    return f"{local[:3]}-{local[3:7]}-{local[7:]}"


def profile_data(user: User, basic: VolunteerOnboardingBasic | None) -> dict:
    return {
        "nickname": user.nickname,
        "full_name": basic.full_name if basic else None,
        "phone_number": display_phone(basic.phone_e164) if basic else None,
        "phone_verified_at": as_utc(basic.phone_verified_at).isoformat() if basic else None,
        "profile_version": user.profile_version,
    }


def read_profile(db: Session, user: User) -> dict:
    """Read only."""
    require_profile_user(user)
    return profile_data(user, db.get(VolunteerOnboardingBasic, user.id))


def parse_patch(body) -> dict:
    """Validate the body before any lock. A missing field means "unchanged"; an explicit empty
    value or null is rejected by the shared rules instead of clearing the stored value."""
    if not isinstance(body, dict) or set(body) - PATCH_KEYS:
        raise invalid_payload()
    version = body.get("expected_profile_version")
    if type(version) is not int or version < 0:
        raise invalid_payload("expected_profile_version")
    changes = {"expected_profile_version": version}
    if "nickname" in body:
        changes["nickname"] = normalize_nickname(body["nickname"], required_code="NICKNAME_REQUIRED")
    if "full_name" in body:
        changes["full_name"] = normalize_full_name(body["full_name"])
    if "phone_number" in body:
        changes["phone_e164"] = normalize_phone(body["phone_number"])
    if "phone_verification_id" in body:
        if "phone_number" not in body:
            raise invalid_payload("phone_verification_id")
        verification_id = parse_verification_id(body["phone_verification_id"])
        if verification_id is None:
            raise invalid_payload("phone_verification_id")
        changes["verification_id"] = verification_id
    return changes


def nickname_used_by_other(db: Session, nickname: str, user_id: int) -> bool:
    return db.query(User.id).filter(func.lower(User.nickname) == nickname.lower(), User.id != user_id).first() is not None


def usable_profile_proof(db: Session, user_id: int, verification_id, phone_e164: str, now: datetime):
    """The locked proof for this phone change, or 409 PHONE_VERIFICATION_REQUIRED. A missing proof,
    another account's or another purpose's proof all get that same answer (Issue #103 §6)."""
    verification = (db.query(PhoneVerification).filter(PhoneVerification.id == verification_id)
                    .populate_existing().with_for_update().one_or_none())
    if (verification is None or verification.user_id != user_id
            or verification.purpose != PURPOSE_PROFILE or verification.phone_e164 != phone_e164
            or verification.verified_at is None
            or verification.send_status not in VERIFIABLE_STATUSES  # never SUPPRESSED, FAILED, PENDING (#110) or verification.consumed_at is not None
            or as_utc(now) >= as_utc(verification.verified_valid_until)
            or not is_latest_for_user(db, verification)):
        raise verification_required()
    return verification


def update_profile(db: Session, user_id: int, body, now: datetime) -> dict:
    changes = parse_patch(body)
    new_nickname, new_name, new_phone = changes.get("nickname"), changes.get("full_name"), changes.get("phone_e164")
    try:
        user = lock_user(db, user_id, for_update=True)
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        require_profile_user(user)
        basic = (db.query(VolunteerOnboardingBasic).filter(VolunteerOnboardingBasic.user_id == user_id)
                 .populate_existing().with_for_update().one_or_none())
        if user.profile_version != changes["expected_profile_version"]:
            raise version_mismatch()

        changed = []
        if new_nickname is not None and new_nickname != user.nickname:
            if nickname_used_by_other(db, new_nickname, user_id):
                raise nickname_taken()
            changed.append("nickname")
        if (new_name is not None or new_phone is not None) and basic is None:
            raise profile_basic_info_required()
        if new_name is not None and new_name != basic.full_name:
            changed.append("full_name")
        proof = None
        if new_phone is not None and new_phone != basic.phone_e164:
            if "verification_id" not in changes:
                raise verification_required()
            proof = usable_profile_proof(db, user_id, changes["verification_id"], new_phone, now)
            if registered_to_other_user(db, new_phone, user_id):
                raise phone_already_registered()
            changed.append("phone_number")

        if not changed:
            db.rollback()  # nothing to write: end the locking transaction
            return {**profile_data(user, basic), "changed_fields": []}
        if "nickname" in changed:
            user.nickname = new_nickname
        if "full_name" in changed:
            basic.full_name = new_name
        if proof is not None:
            basic.phone_e164 = new_phone
            basic.phone_verified_at = proof.verified_at  # when it was verified, not when it was saved
            proof.consumed_at = now
        if basic is not None and ({"full_name", "phone_number"} & set(changed)):
            basic.updated_at = now
        user.profile_version += 1
        db.flush()
        result = {**profile_data(user, basic), "changed_fields": changed}
        db.commit()
        # Audit without personal values: who, when and which fields (also as structured fields).
        at = as_utc(now).isoformat()
        logger.info("profile updated user_id=%s fields=%s at=%s", user_id, ",".join(changed), at,
                    extra={"audit_event": "PROFILE_UPDATED", "audit_user_id": user_id,
                           "audit_fields": list(changed), "audit_at": at})
        return result
    except IntegrityError:
        db.rollback()
        # Lost a race on a UNIQUE index: map it without saying who holds the value.
        if new_nickname is not None and nickname_used_by_other(db, new_nickname, user_id):
            raise nickname_taken() from None
        if new_phone is not None and registered_to_other_user(db, new_phone, user_id):
            raise phone_already_registered() from None
        raise
    except Exception:
        db.rollback()
        raise
