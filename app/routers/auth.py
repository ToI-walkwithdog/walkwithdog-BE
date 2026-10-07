import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_session, get_current_user, hash_password, token_digest, verify_password
from app.database import get_db
from app.models import AuthSession, User
from app.schemas import LoginRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])
SESSION_SECONDS = 3600
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest, response: Response, db: Session = Depends(get_db)):
    users = db.query(User).filter(func.lower(User.email) == str(request.email).lower()).all()
    user = users[0] if len(users) == 1 else None
    stored = user.password if user and (user.password or "").startswith("scrypt$") else DUMMY_HASH
    valid = verify_password(request.password, stored)
    if not user or stored == DUMMY_HASH or not valid:
        raise HTTPException(401, "이메일 또는 비밀번호를 확인해주세요.",
                            headers={"WWW-Authenticate": "Bearer"})
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=token_digest(token), user_id=user.id,
                       expires_at=datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)))
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(access_token=token, expires_in=SESSION_SECONDS)


@router.post("/logout", status_code=204)
def logout(session=Depends(get_current_session), db: Session = Depends(get_db)):
    db.delete(session)
    db.commit()
    return Response(status_code=204)


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user
