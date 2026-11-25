"""My page walk preferences on /10-3 (Issue #105): preferred walk duration and dog size.

GET  returns the stored preferences and, per field, the value that applies:
     stored preference -> completed onboarding Step 3 value -> null, with its source.
PATCH changes duration_code and/or size_code in one transaction:
  body rules (422) -> lock users FOR UPDATE, re-check role and status (403)
  -> read the preference row -> version check (409 PREFERENCE_VERSION_MISMATCH, even when the
     values are equal) -> no-op when nothing changes -> write, version + 1 -> commit.
The users lock serializes every write of one account, so two first saves from version 0
end as one 200 and one 409. Any DB error, including lock timeouts, deadlocks and
serialization failures, rolls everything back and surfaces as the contract's 500.

Never written here: the Step 3 routine and its regions and time slots, onboarding status,
the experience dog size, survey durations, volunteer profile and walk request eligibility.
No region: the app has no region selection yet (Notion, 2026-11-24). Logs carry field names only.
"""
from datetime import datetime
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.account_status import ensure_active, lock_user
from app.api_contract import ContractError
from app.auth import contract_unauthorized
from app.models import (DOG_SIZE_CODES, WALKING_DURATION_CODES, User, UserRole, VolunteerOnboardingRoutine,
                        VolunteerWalkPreference)
from app.onboarding_common import as_utc

logger = logging.getLogger(__name__)

FIELDS = ("duration_code", "size_code")
PATCH_KEYS = {"expected_version", *FIELDS}
ALLOWED = {"duration_code": WALKING_DURATION_CODES, "size_code": DOG_SIZE_CODES}
INVALID_CODE = {"duration_code": ("INVALID_DURATION", "산책 시간을 확인해 주세요."),
                "size_code": ("INVALID_DOG_SIZE", "반려견 크기를 확인해 주세요.")}
SOURCE_PREFERENCE, SOURCE_ONBOARDING, SOURCE_NONE = "PREFERENCE", "ONBOARDING", "NONE"
ONBOARDING_FIELD = {"duration_code": "walking_duration_code", "size_code": "comfortable_dog_size_code"}


def invalid_payload(field=None) -> ContractError:
    return ContractError(422, "INVALID_PREFERENCE_PAYLOAD", "선호 설정 값을 확인해 주세요.",
                         {"field": field} if field else {})


def not_allowed() -> ContractError:
    return ContractError(403, "PREFERENCE_NOT_ALLOWED", "산책 선호를 설정할 수 없는 계정입니다.")


def version_mismatch() -> ContractError:
    return ContractError(409, "PREFERENCE_VERSION_MISMATCH", "다른 기기에서 선호 설정이 변경되었습니다. 다시 확인해 주세요.")


def require_preference_user(user: User) -> None:
    """ACTIVE general users only. No pledge or onboarding completion is required (Issue #105)."""
    if user.role != UserRole.USER:
        raise not_allowed()
    ensure_active(user)


def preference_data(preference: VolunteerWalkPreference | None,
                    routine: VolunteerOnboardingRoutine | None) -> dict:
    effective, sources = {}, {}
    for field in FIELDS:
        stored = getattr(preference, field) if preference is not None else None
        fallback = getattr(routine, ONBOARDING_FIELD[field]) if routine is not None else None
        if stored is not None:
            effective[field], sources[field] = stored, SOURCE_PREFERENCE
        elif fallback is not None:
            effective[field], sources[field] = fallback, SOURCE_ONBOARDING
        else:
            effective[field], sources[field] = None, SOURCE_NONE
    return {
        "configured": preference is not None,
        "version": preference.version if preference is not None else 0,
        "preferences": None if preference is None else {
            "duration_code": preference.duration_code, "size_code": preference.size_code,
            "updated_at": as_utc(preference.updated_at).isoformat()},
        "effective_preferences": effective,
        "sources": sources,
    }


def read_preferences(db: Session, user: User) -> dict:
    """Read only: never creates a preference row or copies Step 3 values."""
    require_preference_user(user)
    return preference_data(db.get(VolunteerWalkPreference, user.id), db.get(VolunteerOnboardingRoutine, user.id))


def parse_patch(body) -> dict:
    """Validate the body before any lock. A missing field means "unchanged"; null is refused
    instead of clearing the stored value."""
    if not isinstance(body, dict) or set(body) - PATCH_KEYS:
        raise invalid_payload()
    version = body.get("expected_version")
    if type(version) is not int or version < 0:
        raise invalid_payload("expected_version")
    if not set(body) & set(FIELDS):
        raise invalid_payload()
    changes = {"expected_version": version}
    for field in FIELDS:
        if field not in body:
            continue
        value = body[field]
        if value is None:
            raise invalid_payload(field)
        if not isinstance(value, str) or value not in ALLOWED[field]:
            code, message = INVALID_CODE[field]
            raise ContractError(422, code, message, {"field": field})
        changes[field] = value
    return changes


def current_preference(db: Session, user_id: int) -> VolunteerWalkPreference | None:
    """The row seen under the users lock (a separate function so tests can line up
    concurrent requests right after this read)."""
    return (db.query(VolunteerWalkPreference).filter(VolunteerWalkPreference.user_id == user_id)
            .populate_existing().one_or_none())


def update_preferences(db: Session, user_id: int, body, now: datetime) -> dict:
    changes = parse_patch(body)
    try:
        user = lock_user(db, user_id, for_update=True)
        if user is None:
            raise contract_unauthorized("USER_NOT_FOUND")
        require_preference_user(user)
        preference = current_preference(db, user_id)
        if (preference.version if preference is not None else 0) != changes["expected_version"]:
            raise version_mismatch()
        changed = [field for field in FIELDS if field in changes
                   and (preference is None or getattr(preference, field) != changes[field])]
        routine = db.get(VolunteerOnboardingRoutine, user_id)
        if not changed:
            result = {**preference_data(preference, routine), "changed_fields": []}
            db.rollback()  # nothing to write: end the locking transaction
            return result
        if preference is None:
            preference = VolunteerWalkPreference(user_id=user_id, version=1, updated_at=now,
                                                 **{field: changes[field] for field in changed})
            db.add(preference)
        else:
            for field in changed:
                setattr(preference, field, changes[field])
            preference.version += 1
            preference.updated_at = now
        db.flush()
        result = {**preference_data(preference, routine), "changed_fields": changed}
        db.commit()
        at = as_utc(now).isoformat()
        logger.info("walk preferences updated user_id=%s fields=%s at=%s", user_id, ",".join(changed), at,
                    extra={"audit_event": "WALK_PREFERENCES_UPDATED", "audit_user_id": user_id,
                           "audit_fields": list(changed), "audit_at": at})
        return result
    except IntegrityError:
        db.rollback()
        # Another first save won the user_id primary key (the users lock makes this unreachable;
        # the key is the last guard). Any other violation stays a 500 instead of hiding as 409.
        if db.query(VolunteerWalkPreference.user_id).filter(VolunteerWalkPreference.user_id == user_id).first():
            raise version_mismatch() from None
        raise
    except Exception:
        db.rollback()
        raise
