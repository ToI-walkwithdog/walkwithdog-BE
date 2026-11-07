"""Issue #6: volunteer statistics are re-aggregated from COMPLETED walks (idempotent).

Called inside the end-walk transaction, after the Issue #3/#4 locks
(Dog -> User -> WalkRequest -> Walk) are held, so the VolunteerProfile lock is last in the
global lock order. Re-aggregating instead of incrementing means retries, duplicate closes
and legacy rows can never double count: running it twice yields the same profile.
"""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import distinct, func

from app.models import Walk, WalkRequest
from app.walk_request_service import locked_profile

# PROVISIONAL policy: the Level 1/2 promotion criteria of Issue #6 still need a product
# decision. Keep them here, in one place, so changing them touches no other code.
LEVEL_2_MIN_COMPLETED_WALKS = 5
LEVEL_2_MIN_UNIQUE_DOGS = 2


def level_for(total_walk_count, unique_dog_count):
    if total_walk_count >= LEVEL_2_MIN_COMPLETED_WALKS and unique_dog_count >= LEVEL_2_MIN_UNIQUE_DOGS:
        return 2
    return 1


def completed_totals(db, user_id):
    """Only COMPLETED walks are valid volunteer records; every other status is excluded."""
    count, distance, seconds, dogs = (
        db.query(
            func.count(Walk.id),
            func.coalesce(func.sum(Walk.total_distance_m), 0),
            func.coalesce(func.sum(Walk.duration_seconds), 0),
            func.count(distinct(WalkRequest.dog_id)),
        )
        .select_from(Walk)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .filter(WalkRequest.user_id == user_id, Walk.status == "COMPLETED")
        .one()
    )
    return {
        "total_walk_count": int(count),
        "total_distance_m": Decimal(str(distance)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "total_duration_seconds": int(seconds),
        "unique_dog_count": int(dogs),
    }


def refresh_volunteer_profile(db, user_id):
    """Recompute totals from walk rows; the level never goes down automatically."""
    profile = locked_profile(db, user_id, create=True)
    totals = completed_totals(db, user_id)
    for name, value in totals.items():
        setattr(profile, name, value)
    profile.level = max(profile.level, level_for(totals["total_walk_count"], totals["unique_dog_count"]))
    db.flush()
    return profile
