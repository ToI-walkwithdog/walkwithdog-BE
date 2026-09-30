# app/routers/auth.py
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, SessionToken
from app.schemas import LoginRequest, LoginResponse, UserResponse
from app.dependencies import get_current_user

router = APIRouter(
    prefix="/auth",
    tags=["auth"]
)


def hash_password(password: str) -> str:
    return hashlib.scrypt(
        password.encode(),
        salt=b"ieojugae_salt",
        n=16384, r=8, p=1
    ).hex()


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()

    if not user or user.password != hash_password(request.password):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다.")

    # 기존 세션 비활성화
    db.query(SessionToken).filter(
        SessionToken.user_id == user.id,
        SessionToken.is_active == True
    ).update({"is_active": False})

    # 새 토큰 발급
    token = secrets.token_hex(32)
    session = SessionToken(
        user_id=user.id,
        token=token,
        is_active=True,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    db.add(session)
    db.commit()

    return {
        "token": token,
        "user": UserResponse(id=user.id, email=user.email, nickname=user.nickname)
    }


@router.post("/logout")
def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    db.query(SessionToken).filter(
        SessionToken.user_id == current_user.id,
        SessionToken.is_active == True
    ).update({"is_active": False})
    db.commit()

    return {"message": "로그아웃 되었습니다."}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user