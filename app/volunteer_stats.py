"""Issue #6: volunteer statistics are re-aggregated from COMPLETED walks (idempotent).

Called inside the end-walk transaction, after the Issue #3/#4 locks
(Dog -> User -> WalkRequest -> Walk) are held, so the VolunteerProfile lock is last in the
global lock order. Re-aggregating instead of incrementing means retries, duplicate closes
and legacy rows can never double count: running it twice yields the same profile.
"""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import distinct, func

from app.models import User, Walk, WalkRequest
from app.walk_request_service import lock_row, locked_profile


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
    """Recompute totals without changing the legacy level.

    Backfills acquire User -> Profile; end_walk already holds User, so re-locking
    that same row is safe. NOWAIT conflicts roll back instead of mixing snapshots.
    """
    if lock_row(db, User, user_id) is None:
        raise ValueError("User not found")
    profile = locked_profile(db, user_id, create=True)
    totals = completed_totals(db, user_id)
    for name, value in totals.items():
        setattr(profile, name, value)
    db.flush()
    return profile
