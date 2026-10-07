"""Password hashing and revocable, opaque bearer sessions (no plaintext tokens in DB)."""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuthSession, User

bearer = HTTPBearer(auto_error=False)


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


def get_current_session(credentials: HTTPAuthorizationCredentials = Depends(bearer),
                        db: Session = Depends(get_db)):
    if credentials is None or len(credentials.credentials) > 256:
        raise unauthorized()
    session = db.query(AuthSession).filter(
        AuthSession.token_hash == token_digest(credentials.credentials)
    ).first()
    if session is None:
        raise unauthorized()
    expiry = session.expires_at
    if expiry.tzinfo is None:  # SQLite tests don't preserve timezone metadata.
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        raise unauthorized()
    return session


def get_current_user(session=Depends(get_current_session), db: Session = Depends(get_db)):
    user = db.get(User, session.user_id)
    if user is None:
        raise unauthorized()
    return user


def require_owner(user_id: int, user: User):
    if user_id != user.id:
        raise HTTPException(403, "본인 데이터만 접근할 수 있습니다.")
