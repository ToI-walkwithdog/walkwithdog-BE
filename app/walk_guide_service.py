"""Service layer for F-14 First Walk Guide (SC-03 & SC-04)."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Survey, User, UserRole, Walk, WalkRequest

NO_EXPERIENCE_VALUES = {
    "NONE",
    "NO",
    "NO_EXPERIENCE",
    "FALSE",
    "0",
    "\uc5c6\uc74c",
    "\uacbd\ud5d8 \uc5c6\uc74c",
    "\uacbd\ud5d8\uc5c6\uc74c",
    "\ubc18\ub824\uacac \uacbd\ud5d8 \uc5c6\uc74c",
    "\ubc18\ub824\uacbd\ud5d8\uc5c6\uc74c",
}

RECOMMENDATION_MESSAGE = "\ubc18\ub824\uacac \uacbd\ud5d8\uc774 \uc5c6\ub294 \uc0ac\uc6a9\uc790\ub294 \uccab \uc0b0\ucc45 \uc608\uc57d \uc804\uc5d0 \uac00\uc774\ub4dc\ub97c \ub05d\uae4c\uc9c0 \uc5f4\ub78c\ud558\ub294 \uac83\uc744 \uad8c\uc720\ud569\ub2c8\ub2e4."

FIRST_WALK_GUIDE_SLIDES = [
    {
        "step": 1,
        "category": "HANDLING",
        "title": "\uac15\uc544\uc9c0 \ub2e4\ub8e8\ub294 \ubc95",
        "summary": "\ucd9c\ubc1c \uc804 \ud558\ub124\uc2a4\uc640 \ub9ac\ub4dc\uc904 \uacb0\ucc29 \uc0c1\ud0dc\ub97c \ud655\uc778\ud558\uace0, \uac15\uc544\uc9c0\uc758 \uc18d\ub3c4\uc5d0 \ub9de\ucdb0 \uc5ec\uc720 \uc788\uac8c \uac78\uc5b4\uc8fc\uc138\uc694.",
        "tips": [
            "\ubaa9\uc904\u00b7\ud558\ub124\uc2a4\uac00 \ud5d0\uac81\uc9c0 \uc54a\uc740\uc9c0 \uc190\uac00\ub77d \ub450 \uac1c \uc5ec\uc720\ub85c \uc810\uac80\ud569\ub2c8\ub2e4.",
            "\uac11\uc791\uc2a4\ub7fd\uac8c \uc904\uc744 \uc7a1\uc544\ub2f9\uae30\uc9c0 \uc54a\uace0 \ubd80\ub4dc\ub7fd\uac8c \ubc29\ud5a5\uc744 \uc548\ub0b4\ud569\ub2c8\ub2e4.",
            "\ub0af\uc120 \ud658\uacbd\uc5d0\uc11c\ub294 \uac15\uc544\uc9c0\uac00 \ub0c4\uc0c8\ub97c \ub9e1\uc73c\uba70 \uc801\uc751\ud560 \uc2dc\uac04\uc744 \ucda9\ubd84\ud788 \uc90d\ub2c8\ub2e4.",
        ],
    },
    {
        "step": 2,
        "category": "PROHIBITED_ACTIONS",
        "title": "\uae08\uc9c0 \ud589\ub3d9",
        "summary": "\uc0b0\ucc45 \uc911 \ub9ac\ub4dc\uc904\uc744 \ub193\uac70\ub098 \ub2e4\ub978 \uac15\uc544\uc9c0\u00b7\uc0ac\ub78c\uc5d0\uac8c \uc0ac\uc804 \ud655\uc778 \uc5c6\uc774 \uc811\ucd09\uc2dc\ud0a4\uc9c0 \ub9c8\uc138\uc694.",
        "tips": [
            "\uc5b4\ub5a4 \uc0c1\ud669\uc5d0\uc11c\ub3c4 \ub9ac\ub4dc\uc904 \uc190\uc7a1\uc774\ub97c \uc190\ubaa9\uc5d0\uc11c \ub193\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4.",
            "\ubcf4\ud638\uc18c \ud5c8\uac00 \uc5c6\uc774 \uac04\uc2dd\uc774\ub098 \uc0ac\ub78c \uc74c\uc2dd\uc744 \uae09\uc5ec\ud558\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4.",
            "\ub2e4\ub978 \ubc18\ub824\uacac\u00b7\ubcf4\ud589\uc790\uc640 \ub9c8\uc8fc\uce60 \ub54c\ub294 \ucda9\ubd84\ud55c \uc548\uc804\uac70\ub9ac\ub97c \uc720\uc9c0\ud569\ub2c8\ub2e4.",
        ],
    },
    {
        "step": 3,
        "category": "WASTE_DISPOSAL",
        "title": "\ubc30\ubcc0 \ucc98\ub9ac",
        "summary": "\ubc30\ubcc0 \ubd09\ud22c\ub97c \ud56d\uc0c1 \uc9c0\ucc38\ud558\uace0 \ubc30\ubcc0 \uc989\uc2dc \uae68\ub057\uc774 \uc218\uac70\ud574 \uc9c0\uc815\ub41c \uc7a5\uc18c\uc5d0 \ucc98\ub9ac\ud574 \uc8fc\uc138\uc694.",
        "tips": [
            "\uc0b0\ucc45 \uc2dc\uc791 \uc804 \ubc30\ubcc0 \ubd09\ud22c\uc640 \ubb3c\ud2f0\uc288 \uc18c\uc9c0 \uc5ec\ubd80\ub97c \ud655\uc778\ud569\ub2c8\ub2e4.",
            "\ubc30\ubcc0 \ud6c4 \uc989\uc2dc \ubd09\ud22c\ub85c \uc218\uac70\ud574 \ubc00\ubd09\ud55c \ub4a4 \uc4f0\ub808\uae30\ud1b5 \ub610\ub294 \ubcf4\ud638\uc18c \uc9c0\uc815 \uc704\uce58\uc5d0 \ubc84\ub9bd\ub2c8\ub2e4.",
            "\uc124\uc0ac\ub098 \ud608\ubcc0 \ub4f1 \uc774\uc0c1 \uc9d5\ud6c4\uac00 \ubcf4\uc774\uba74 \uc0b0\ucc45 \uba54\ubaa8\u00b7\ud2b9\uc774\uc0ac\ud56d\uc73c\ub85c \ubcf4\ud638\uc18c\uc5d0 \uc804\ub2ec\ud569\ub2c8\ub2e4.",
        ],
    },
    {
        "step": 4,
        "category": "EMERGENCY_REPORT",
        "title": "\uc2e0\uace0 \ubc84\ud2bc \uc704\uce58",
        "summary": "\ubd80\uc0c1\u00b7\ubd84\uc2e4\u00b7\ub3cc\ubc1c \uc0c1\ud669 \ubc1c\uc0dd \uc2dc \uc0b0\ucc45 \ud654\uba74 \uc6b0\uce21 \uc0c1\ub2e8(\uae34\uae09 \uc2e0\uace0) \ubc84\ud2bc\uc744 \ub20c\ub7ec \uc989\uc2dc \ubcf4\ud638\uc18c\uc5d0 \uc54c\ub9ac\uc138\uc694.",
        "tips": [
            "\uc9c4\ud589 \uc911\uc778 \uc0b0\ucc45 \ud654\uba74 \uc0c1\ub2e8 '\uae34\uae09 \uc2e0\uace0' \ubc84\ud2bc\uc5d0\uc11c \uc0ac\uace0 \uc720\ud615\uacfc \ud604\uc7ac \uc704\uce58\ub97c \ubc14\ub85c \uc804\uc1a1\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.",
            "\ub9ac\ub4dc\uc904 \uc774\ud0c8\u00b7\ubd84\uc2e4 \ubc1c\uc0dd \uc2dc \ubb34\ub9ac\ud558\uac8c \ucd94\uaca9\ud558\uae30\ubcf4\ub2e4 \uc989\uc2dc \uc2e0\uace0 \ud6c4 \ubcf4\ud638\uc18c \uc548\ub0b4\ub97c \ub530\ub985\ub2c8\ub2e4.",
            "\uac00\uc774\ub4dc\ub97c \ubaa8\ub450 \ud655\uc778\ud588\ub2e4\uba74 \uc544\ub798 '\ud655\uc778\ud588\uc5b4\uc694' \ubc84\ud2bc\uc744 \ub20c\ub7ec \uc5f4\ub78c\uc744 \uc644\ub8cc\ud574 \uc8fc\uc138\uc694.",
        ],
    },
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def has_no_dog_experience(db: Session, user_id: int) -> bool:
    latest_survey = (
        db.query(Survey)
        .filter(Survey.user_id == user_id)
        .order_by(Survey.created_at.desc(), Survey.id.desc())
        .first()
    )
    if latest_survey is None or not latest_survey.dog_experience:
        return False
    normalized = latest_survey.dog_experience.strip().upper()
    return normalized in NO_EXPERIENCE_VALUES


def has_prior_reservation_or_walk(db: Session, user_id: int) -> bool:
    has_active_or_approved_request = (
        db.query(WalkRequest.id)
        .filter(
            WalkRequest.user_id == user_id,
            WalkRequest.status.in_(("PENDING", "APPROVED")),
        )
        .first()
        is not None
    )
    if has_active_or_approved_request:
        return True

    has_walk = (
        db.query(Walk.id)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .filter(WalkRequest.user_id == user_id)
        .first()
        is not None
    )
    return has_walk


def get_first_walk_guide_status(db: Session, user: User) -> dict:
    is_completed = user.first_walk_guide_completed_at is not None
    should_recommend = False
    if user.role == UserRole.USER and not is_completed:
        if has_no_dog_experience(db, user.id) and not has_prior_reservation_or_walk(db, user.id):
            should_recommend = True

    return {
        "is_completed": is_completed,
        "completed_at": user.first_walk_guide_completed_at,
        "should_recommend_guide": should_recommend,
        "recommendation_message": RECOMMENDATION_MESSAGE if should_recommend else None,
    }


def get_first_walk_guide(db: Session, user: User) -> dict:
    status = get_first_walk_guide_status(db, user)
    return {
        "guide_version": "v1",
        "title": "\uccab \uc0b0\ucc45 \uac00\uc774\ub4dc",
        "subtitle": "\ucc98\uc74c \uc0b0\ucc45 \ubd09\uc0ac\ub97c \uc2dc\uc791\ud558\ub294 \ubd84\ub4e4\uc744 \uc704\ud55c 4\uac00\uc9c0 \ud575\uc2ec \uc218\uce59",
        "total_slides": len(FIRST_WALK_GUIDE_SLIDES),
        "slides": FIRST_WALK_GUIDE_SLIDES,
        **status,
    }


def complete_first_walk_guide(db: Session, user: User) -> dict:
    if user.first_walk_guide_completed_at is None:
        user.first_walk_guide_completed_at = utc_now()
        db.add(user)
        db.flush()
    return get_first_walk_guide_status(db, user)
