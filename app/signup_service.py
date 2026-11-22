"""Sign-up persistence. Pre-checks give precise errors; unique indexes are the final guard."""
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api_contract import ContractError
from app.auth import hash_password
from app.models import User, UserRole

DUPLICATE_ERRORS = {
    "login_id": ("LOGIN_ID_ALREADY_EXISTS", "이미 사용 중인 아이디입니다."),
    "nickname": ("NICKNAME_ALREADY_EXISTS", "이미 사용 중인 닉네임입니다."),
    "email": ("EMAIL_ALREADY_EXISTS", "이미 사용 중인 이메일입니다."),
}
# Index and constraint names from app.models and migration 016. users_email_key is
# the pre-existing column constraint that PostgreSQL created for email.
UNIQUE_CONSTRAINT_FIELDS = {
    "uq_users_login_id_lower": "login_id",
    "uq_users_nickname_lower": "nickname",
    "uq_users_email_lower": "email",
    "users_email_key": "email",
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
    return "email" if "users.email" in detail else None


def register_user(db: Session, login_id: str, nickname: str, email: str, password: str) -> User:
    """Insert one account in a single transaction; values are already normalized."""
    values = {"login_id": login_id, "nickname": nickname, "email": email}
    taken = first_taken_field(db, values)
    if taken:
        raise duplicate_error(taken)
    user = User(**values, password=hash_password(password), role=UserRole.USER, shelter_id=None)
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # A concurrent sign-up won the race. Report the field it actually took.
        field = conflicted_field(exc) or first_taken_field(db, values)
        if field is None:
            raise
        raise duplicate_error(field) from None
    db.refresh(user)
    return user
