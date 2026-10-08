from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin
from app.auth import get_current_user, hash_password, require_owner
from app.models import Shelter, User, UserRole
from app.schemas import UserCreate, UserResponse, UserShelterUpdate

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
        password=hash_password(user.password),
        role=UserRole.USER,
        shelter_id=None,
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


@router.patch("/{user_id}/shelter", response_model=UserResponse)
def update_user_shelter(
    user_id: int,
    assignment: UserShelterUpdate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.role != UserRole.SHELTER:
        raise HTTPException(409, "보호소 계정에만 담당 보호소를 지정할 수 있습니다.")
    if assignment.shelter_id is not None and db.get(Shelter, assignment.shelter_id) is None:
        raise HTTPException(404, "Shelter not found")

    user.shelter_id = assignment.shelter_id
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int, user=Depends(get_current_user)):
    require_owner(user_id, user)
    return user
