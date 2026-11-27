"""Router for F-15 Shelter-to-Volunteer Review (SC-16, SC-09, SC-10)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.dependencies import require_role
from app.models import User, UserRole
from app.shelter_volunteer_review_schemas import (
    ShelterVolunteerReviewCreate,
    ShelterVolunteerReviewResponse,
    VolunteerReviewsResponse,
)
from app.shelter_volunteer_review_service import (
    create_shelter_volunteer_review,
    get_shelter_volunteer_review,
    list_volunteer_reviews,
)

router = APIRouter(tags=["shelter-volunteer-reviews"])
require_volunteer = require_role(UserRole.USER)


@router.post("/walks/{walk_id}/volunteer-review", response_model=ShelterVolunteerReviewResponse, status_code=201)
def post_volunteer_review(
    walk_id: int,
    payload: ShelterVolunteerReviewCreate,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """F-15 SC-16: Shelter writes a one-time volunteer review for a COMPLETED walk (editing disallowed)."""
    try:
        result = create_shelter_volunteer_review(db=db, walk_id=walk_id, actor=actor, payload=payload)
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        original = getattr(exc, "orig", None)
        code = getattr(original, "pgcode", None)
        name = getattr(getattr(original, "diag", None), "constraint_name", None)
        if code in ("55P03", "40P01", "40001"):
            raise HTTPException(409, "\ub2e4\ub978 \uc791\uc5c5\uacfc \ucda9\ub3cc\ud588\uc2b5\ub2c8\ub2e4. \ub2e4\uc2dc \uc2dc\ub3c4\ud558\uc138\uc694.") from None
        if code == "23505" and name == "uq_shelter_volunteer_reviews_walk":
            raise HTTPException(409, "\uc774\ubbf8 \uc0b0\ucc45\uc790 \ud3c9\uac00\uac00 \uc791\uc131\ub41c \uc0b0\ucc45\uc785\ub2c8\ub2e4.") from None
        raise HTTPException(500, "\uc0b0\ucc45\uc790 \ud3c9\uac00\ub97c \uc800\uc7a5\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.") from None


@router.get("/walks/{walk_id}/volunteer-review", response_model=ShelterVolunteerReviewResponse | None)
def read_volunteer_review(
    walk_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """F-15 SC-09/SC-16: Read shelter-to-volunteer review for a single walk (null if not reviewed)."""
    return get_shelter_volunteer_review(db=db, walk_id=walk_id, actor=actor)


@router.get("/volunteers/me/reviews", response_model=VolunteerReviewsResponse, tags=["volunteers"])
def read_my_volunteer_reviews(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(require_volunteer),
    db: Session = Depends(get_db),
):
    """F-15 SC-10: MyPage list and summary of shelter reviews received by the volunteer."""
    return list_volunteer_reviews(db=db, user_id=actor.id, limit=limit, offset=offset)
