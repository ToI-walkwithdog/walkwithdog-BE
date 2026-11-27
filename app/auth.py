"""Password hashing and revocable, opaque bearer sessions (no plaintext tokens in DB)."""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.account_status import AGE_REVIEW_REQUIRED_MESSAGE, is_active
from app.api_contract import ContractError
from app.database import get_db
from app.models import AuthSession, User

bearer = HTTPBearer(auto_error=False)
# Stored for accounts that must never log in (e.g. demo fixtures). It is not a
# scrypt hash, so verify_password always rejects it.
UNUSABLE_PASSWORD = "!"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1,
                            maxmem=256 * 1024 * 1024, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    # Never treat legacy plaintext or a missing password as a valid credential.
    try:
        scheme, salt, digest = (stored or "").split("$")
        if scheme != "scrypt" or len(salt) != 32 or len(digest) != 64:
            return False
        computed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                  n=2**17, r=8, p=1, maxmem=256 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(computed.hex(), digest)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def unauthorized():
    return HTTPException(401, "로그인이 필요하거나 세션이 만료되었습니다.",
                         headers={"WWW-Authenticate": "Bearer"})


# 401 codes of the {code, data, message} contract (Issue #32). Routes outside that
# contract keep unauthorized() above, so their `detail` responses do not change.
AUTH_ERROR_MESSAGES = {
    "AUTHENTICATION_REQUIRED": "로그인이 필요합니다.",
    "INVALID_ACCESS_TOKEN": "유효하지 않은 인증 정보입니다.",
    "SESSION_EXPIRED": "로그인 세션이 만료되었습니다.",
    "USER_NOT_FOUND": "로그인이 필요합니다.",
}
MAX_TOKEN_LENGTH = 256


class AuthFailure(Exception):
    """Why a request is not authenticated; one of AUTH_ERROR_MESSAGES."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def find_session(credentials: HTTPAuthorizationCredentials | None, db: Session) -> AuthSession:
    """Return the live session for the bearer token or raise AuthFailure. Never writes."""
    # HTTPBearer(auto_error=False) gives None for a missing header and for other schemes.
    if credentials is None:
        raise AuthFailure("AUTHENTICATION_REQUIRED")
    if len(credentials.credentials) > MAX_TOKEN_LENGTH:
        raise AuthFailure("INVALID_ACCESS_TOKEN")
    session = db.query(AuthSession).filter(
        AuthSession.token_hash == token_digest(credentials.credentials)
    ).first()
    if session is None:  # forged, revoked by logout, or never issued
        raise AuthFailure("INVALID_ACCESS_TOKEN")
    expiry = session.expires_at
    if expiry.tzinfo is None:  # SQLite tests don't preserve timezone metadata.
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        raise AuthFailure("SESSION_EXPIRED")
    return session


def get_current_session(credentials: HTTPAuthorizationCredentials = Depends(bearer),
                        db: Session = Depends(get_db)):
    try:
        return find_session(credentials, db)
    except AuthFailure:
        raise unauthorized() from None


def get_current_user(session=Depends(get_current_session), db: Session = Depends(get_db)):
    """User for the `detail`-format APIs (walks, chat, surveys, matching, shelters, admin).

    An AGE_REVIEW_REQUIRED account gets 403 here in the existing `detail` format
    (Issue #41). The routes it may still use (login, logout, GET /users/me, age
    review correction and exit) use the contract dependencies below, not this one.
    Shelter and admin accounts are always ACTIVE, so they are unaffected.
    """
    user = db.get(User, session.user_id)
    if user is None:
        raise unauthorized()
    if not is_active(user):
        raise HTTPException(403, AGE_REVIEW_REQUIRED_MESSAGE)
    return user


def contract_unauthorized(code: str) -> ContractError:
    return ContractError(401, code, AUTH_ERROR_MESSAGES[code],
                         headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})


def get_contract_session(credentials: HTTPAuthorizationCredentials = Depends(bearer),
                         db: Session = Depends(get_db)):
    """Same checks as get_current_session, answered with the contract 401 codes."""
    try:
        return find_session(credentials, db)
    except AuthFailure as failure:
        raise contract_unauthorized(failure.code) from None


def get_contract_user(session=Depends(get_contract_session), db: Session = Depends(get_db)):
    user = db.get(User, session.user_id)
    if user is None:
        raise contract_unauthorized("USER_NOT_FOUND")
    return user


def require_owner(user_id: int, user: User):
    if user_id != user.id:
        raise HTTPException(403, "본인 데이터만 접근할 수 있습니다.")
