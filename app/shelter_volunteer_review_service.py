"""Service layer for F-15 Shelter-to-Volunteer Review (SC-16, SC-09, SC-10)."""
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Shelter,
    ShelterVolunteerReview,
    ShelterVolunteerReviewTag,
    User,
    UserRole,
    Walk,
    WalkRequest,
)
from app.shelter_volunteer_review_schemas import (
    ShelterVolunteerReviewCreate,
    empty_tag_counts,
)


def serialize_shelter_volunteer_review(review: ShelterVolunteerReview | None) -> dict | None:
    if review is None:
        return None
    shelter_name = review.shelter.name if getattr(review, "shelter", None) is not None else None
    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "shelter_id": review.shelter_id,
        "shelter_name": shelter_name,
        "volunteer_user_id": review.volunteer_user_id,
        "reviewed_by": review.reviewed_by,
        "rating": Decimal(str(review.rating)).quantize(Decimal("0.1")),
        "tags": [tag.tag_code for tag in review.tags],
        "comment": review.comment,
        "created_at": review.created_at,
    }


def create_shelter_volunteer_review(
    db: Session,
    walk_id: int,
    actor: User,
    payload: ShelterVolunteerReviewCreate,
) -> dict:
    walk = (
        db.query(Walk)
        .options(joinedload(Walk.walk_request).joinedload(WalkRequest.shelter))
        .filter(Walk.id == walk_id)
        .populate_existing()
        .with_for_update(nowait=True)
        .first()
    )
    if walk is None:
        raise HTTPException(404, "Walk not found")

    request = walk.walk_request
    if actor.role == UserRole.SHELTER:
        if not actor.shelter_id or request.shelter_id != actor.shelter_id:
            raise HTTPException(403, "\ub2e4\ub978 \ubcf4\ud638\uc18c\uc758 \uc0b0\ucc45\uc740 \ud3c9\uac00\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")
    elif actor.role != UserRole.ADMIN:
        raise HTTPException(403, "\ubcf4\ud638\uc18c \ub2f4\ub2f9\uc790\ub9cc \uc0b0\ucc45\uc790 \ud3c9\uac00\ub97c \uc791\uc131\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.")

    if walk.status != "COMPLETED":
        raise HTTPException(409, "\uc815\uc0c1 \uc644\ub8cc(COMPLETED)\ub41c \uc0b0\ucc45\ub9cc \ud3c9\uac00\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.")

    existing = (
        db.query(ShelterVolunteerReview.id)
        .filter(ShelterVolunteerReview.walk_id == walk.id)
        .first()
    )
    if existing is not None:
        raise HTTPException(409, "\uc774\ubbf8 \uc0b0\ucc45\uc790 \ud3c9\uac00\uac00 \uc791\uc131\ub41c \uc0b0\ucc45\uc785\ub2c8\ub2e4. \uc791\uc131 \ud6c4 \uc218\uc815\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")

    review = ShelterVolunteerReview(
        walk_id=walk.id,
        shelter_id=request.shelter_id,
        volunteer_user_id=request.user_id,
        reviewed_by=actor.id,
        rating=payload.rating,
        comment=payload.comment,
        tags=[ShelterVolunteerReviewTag(tag_code=code) for code in payload.tags],
    )
    db.add(review)
    db.flush()

    loaded = (
        db.query(ShelterVolunteerReview)
        .options(
            joinedload(ShelterVolunteerReview.tags),
            joinedload(ShelterVolunteerReview.shelter),
        )
        .filter(ShelterVolunteerReview.id == review.id)
        .one()
    )
    return serialize_shelter_volunteer_review(loaded)


def get_shelter_volunteer_review(db: Session, walk_id: int, actor: User) -> dict | None:
    walk = (
        db.query(Walk)
        .options(joinedload(Walk.walk_request))
        .filter(Walk.id == walk_id)
        .first()
    )
    if walk is None:
        raise HTTPException(404, "Walk not found")

    request = walk.walk_request
    if actor.role == UserRole.USER:
        if request.user_id != actor.id:
            raise HTTPException(403, "\ubcf8\uc778\uc758 \uc0b0\ucc45 \ud3c9\uac00\ub9cc \uc870\ud68c\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.")
    elif actor.role == UserRole.SHELTER:
        if not actor.shelter_id or request.shelter_id != actor.shelter_id:
            raise HTTPException(403, "\ub2e4\ub978 \ubcf4\ud638\uc18c\uc758 \uc0b0\ucc45 \ud3c9\uac00\ub294 \uc870\ud68c\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")
    elif actor.role != UserRole.ADMIN:
        raise HTTPException(403, "\uad8c\ud55c\uc774 \uc5c6\uc2b5\ub2c8\ub2e4.")

    review = (
        db.query(ShelterVolunteerReview)
        .options(
            joinedload(ShelterVolunteerReview.tags),
            joinedload(ShelterVolunteerReview.shelter),
        )
        .filter(ShelterVolunteerReview.walk_id == walk.id)
        .first()
    )
    return serialize_shelter_volunteer_review(review)


def get_volunteer_review_summary(db: Session, user_id: int) -> dict:
    total_count, avg_rating = (
        db.query(
            func.count(ShelterVolunteerReview.id),
            func.avg(ShelterVolunteerReview.rating),
        )
        .filter(ShelterVolunteerReview.volunteer_user_id == user_id)
        .one()
    )
    total_count = int(total_count or 0)
    formatted_avg = (
        Decimal(str(avg_rating)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if total_count > 0 and avg_rating is not None
        else None
    )

    counts = empty_tag_counts()
    tag_rows = (
        db.query(ShelterVolunteerReviewTag.tag_code, func.count(ShelterVolunteerReviewTag.id))
        .join(ShelterVolunteerReview, ShelterVolunteerReviewTag.review_id == ShelterVolunteerReview.id)
        .filter(ShelterVolunteerReview.volunteer_user_id == user_id)
        .group_by(ShelterVolunteerReviewTag.tag_code)
        .all()
    )
    for tag_code, cnt in tag_rows:
        if tag_code in counts:
            counts[tag_code] = int(cnt)

    return {
        "total_review_count": total_count,
        "average_rating": formatted_avg,
        "tag_counts": counts,
    }


def list_volunteer_reviews(db: Session, user_id: int, limit: int, offset: int) -> dict:
    summary = get_volunteer_review_summary(db, user_id)
    rows = (
        db.query(ShelterVolunteerReview)
        .options(
            joinedload(ShelterVolunteerReview.tags),
            joinedload(ShelterVolunteerReview.shelter),
        )
        .filter(ShelterVolunteerReview.volunteer_user_id == user_id)
        .order_by(ShelterVolunteerReview.created_at.desc(), ShelterVolunteerReview.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return {
        "user_id": user_id,
        "summary": summary,
        "total": summary["total_review_count"],
        "limit": limit,
        "offset": offset,
        "reviews": [serialize_shelter_volunteer_review(r) for r in rows],
    }
