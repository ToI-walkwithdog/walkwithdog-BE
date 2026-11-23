"""Router for F-14 First Walk Guide (SC-03 Home & SC-04 Guide)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.walk_guide_schemas import FirstWalkGuideResponse, FirstWalkGuideStatusResponse
from app.walk_guide_service import (
    complete_first_walk_guide,
    get_first_walk_guide,
    get_first_walk_guide_status,
)

router = APIRouter(prefix="/walk-guides", tags=["walk-guides"])


@router.get("/first-walk", response_model=FirstWalkGuideResponse)
def read_first_walk_guide(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """F-14 SC-03/SC-04: First walk guide 4-slide cards and completion/recommendation status."""
    return get_first_walk_guide(db, actor)


@router.get("/first-walk/status", response_model=FirstWalkGuideStatusResponse)
def read_first_walk_guide_status(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """F-14 SC-03: Home bottom guide completion and inexperienced user recommendation status."""
    return get_first_walk_guide_status(db, actor)


@router.post("/first-walk/complete", response_model=FirstWalkGuideStatusResponse)
def confirm_first_walk_guide(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """F-14 SC-04: Mark guide completed when tapping confirm on the last slide."""
    result = complete_first_walk_guide(db, actor)
    db.commit()
    return result
