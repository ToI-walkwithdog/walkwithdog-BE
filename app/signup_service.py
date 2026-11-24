"""Sign-up persistence. Pre-checks give precise errors; unique indexes are the final guard."""
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api_contract import ContractError
from app.auth import hash_password
from app.models import User, UserConsent, UserRole
from app.policy_documents import active_signup_privacy_policy
from app.privacy_policy import CONSENT_ACTION_AGREE, PrivacyInput, check_against_policy
from app.schemas import SignupUserData

DUPLICATE_ERRORS = {
    "login_id": ("LOGIN_ID_ALREADY_EXISTS", "이미 사용 중인 아이디입니다."),
    "nickname": ("NICKNAME_ALREADY_EXISTS", "이미 사용 중인 닉네임입니다."),
}
# Index names from app.models and migration 016. Migration 017 drops the email ones.
UNIQUE_CONSTRAINT_FIELDS = {
    "uq_users_login_id_lower": "login_id",
    "uq_users_nickname_lower": "nickname",
}


def is_taken(db: Session, field: str, value: str) -> bool:
    column = getattr(User, field)
    # Same expression as the unique index, so the check and the constraint agree.
    return db.query(User.id).filter(func.lower(column) == func.lower(value)).first() is not None


def duplicate_error(field: str) -> ContractError:
    code, message = DUPLICATE_ERRORS[field]
    return ContractError(409, code, message, {"field": field})


def first_taken_field(db: Session, values: dict) -> str | None:
    return next((field for field in DUPLICATE_ERRORS if is_taken(db, field, values[field])), None)


def conflicted_field(exc: IntegrityError) -> str | None:
    diagnostics = getattr(exc.orig, "diag", None)
    name = getattr(diagnostics, "constraint_name", None)  # psycopg2
    if name in UNIQUE_CONSTRAINT_FIELDS:
        return UNIQUE_CONSTRAINT_FIELDS[name]
    detail = str(exc.orig)  # SQLite names the index or column in the message
    for constraint, field in UNIQUE_CONSTRAINT_FIELDS.items():
        if constraint in detail:
            return field
    return None


def register_user(db: Session, login_id: str, nickname: str, password: str, privacy: PrivacyInput) -> SignupUserData:
    """Store the normalized account, age declaration and actual consents together."""
    consent_types = check_against_policy(privacy, active_signup_privacy_policy())
    values = {"login_id": login_id, "nickname": nickname}
    try:
        taken = first_taken_field(db, values)
        if taken:
            raise duplicate_error(taken)

        password_hash = hash_password(password)
        now = datetime.now(timezone.utc)
        user = User(**values, password=password_hash, role=UserRole.USER, shelter_id=None,
                    age_14_plus_confirmed_at=now)
        db.add(user)
        try:
            db.flush()  # Assign the user ID and check the account's unique indexes.
        except IntegrityError as exc:
            field = conflicted_field(exc)
            if field is None:
                db.rollback()
                field = first_taken_field(db, values)
            if field is None:
                raise
            raise duplicate_error(field) from None

        db.add_all([
            UserConsent(user_id=user.id, consent_type=consent_type, document_version=privacy.document_version,
                        action=CONSENT_ACTION_AGREE, recorded_at=now)
            for consent_type in consent_types
        ])
        data = SignupUserData.model_validate(user, from_attributes=True)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return data
