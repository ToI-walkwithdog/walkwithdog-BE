"""Issue #6: owner map, public dog map, volunteer profile and walk history (read-only).

Owner endpoints require the volunteer account itself (other users, shelters and admins
get 403 before anything is looked up). The dog map is the only cross-user view and only
exposes the masked data built in app.walk_map_service.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_owner
from app.database import get_db
from app.dependencies import require_role
from app.models import Dog, User, UserRole, VolunteerProfile
from app.walk_map_schemas import (
    DogMapResponse, UserMapResponse, UserProfileResponse, UserWalksResponse,
)
from app.walk_map_service import dog_map, user_map, user_walks
from app.walk_request_schemas import VolunteerProfileResponse

router = APIRouter(tags=["walk-maps"])
require_volunteer = require_role(UserRole.USER)


@router.get("/users/{user_id}/map", response_model=UserMapResponse)
def my_map(user_id: int, limit: int = Query(10, ge=1, le=30), offset: int = Query(0, ge=0),
           actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    require_owner(user_id, actor)
    return user_map(db, user_id, limit, offset)


@router.get("/users/{user_id}/profile", response_model=UserProfileResponse)
def my_profile(user_id: int, actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    require_owner(user_id, actor)
    # Same source row and defaults as GET /volunteers/me, so both views always agree.
    profile = db.get(VolunteerProfile, user_id)
    base = VolunteerProfileResponse.model_validate(profile) if profile is not None \
        else VolunteerProfileResponse(user_id=user_id)
    return UserProfileResponse(**base.model_dump(), nickname=actor.nickname)


@router.get("/users/{user_id}/walks", response_model=UserWalksResponse)
def my_walks(user_id: int,
             status: Literal["IN_PROGRESS", "COMPLETED", "INTERRUPTED", "CANCELLED"] | None = None,
             limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
             actor: User = Depends(require_volunteer), db: Session = Depends(get_db)):
    require_owner(user_id, actor)
    return user_walks(db, user_id, status, limit, offset)


@router.get("/dogs/{dog_id}/map", response_model=DogMapResponse)
def public_dog_map(dog_id: int, limit: int = Query(10, ge=1, le=30), offset: int = Query(0, ge=0),
                   _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if db.get(Dog, dog_id) is None:
        raise HTTPException(404, "Dog not found")
    return dog_map(db, dog_id, limit, offset)
