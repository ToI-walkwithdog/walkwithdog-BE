from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, hash_password, require_owner
from app.models import User
from app.schemas import UserCreate, UserResponse

router = APIRouter(
    prefix="/users",
    tags=["users"]
)


@router.post("/", response_model=UserResponse)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    email = str(user.email).lower()
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "이미 등록된 이메일입니다.")
    new_user = User(
        email=email,
        nickname=user.nickname,
        password=hash_password(user.password)
    )

    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "이미 등록된 이메일입니다.")
    db.refresh(new_user)

    return new_user


@router.get("/me", response_model=UserResponse)
def get_me(user=Depends(get_current_user)):
    return user


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int, user=Depends(get_current_user)):
    require_owner(user_id, user)
    return user
