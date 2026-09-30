# app/dependencies.py
from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from app.database import get_db
from app.models import User, UserRole, SessionToken


def get_current_user(
    authorization: str = Header(..., description="Bearer {토큰}"),
    db: Session = Depends(get_db)
) -> User:
    """토큰으로 현재 로그인 유저 반환"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="토큰 형식이 올바르지 않습니다.")

    token = authorization.replace("Bearer ", "")

    session = db.query(SessionToken).filter(
        SessionToken.token == token,
        SessionToken.is_active == True
    ).first()

    if not session:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    # 만료 체크
    if session.expires_at < datetime.now(timezone.utc):
        session.is_active = False
        db.commit()
        raise HTTPException(status_code=401, detail="세션이 만료되었습니다. 다시 로그인해주세요.")

    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="유저를 찾을 수 없습니다.")

    return user


def require_role(*roles: UserRole):
    """특정 역할만 접근 허용"""
    def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="권한이 없습니다.")
        return current_user
    return checker


# 자주 쓰는 의존성 미리 정의
require_login   = require_role(UserRole.USER, UserRole.SHELTER, UserRole.ADMIN)
require_shelter = require_role(UserRole.SHELTER, UserRole.ADMIN)
require_admin   = require_role(UserRole.ADMIN)