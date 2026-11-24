"""Credit eligible handovers in independent, retryable transactions."""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os

from sqlalchemy import and_, func, or_

from app.models import User, UserRole, Walk, WalkRequest
from app.walk_models import VolunteerLevelEvaluation, VolunteerLevelEvent, WalkHandover
from app.walk_request_service import as_utc, lock_row, locked_profile, utc_now

logger = logging.getLogger(__name__)
LEVEL_THRESHOLDS = (0, 3, 10, 20, 40)
POLL_SECONDS = 30
RETRY_DELAY = timedelta(seconds=60)
BATCH_SIZE = 50
RETRYABLE_LOCK_CODES = {"55P03", "40P01", "40001"}


@dataclass(frozen=True)
class LevelPolicy:
    version: str
    starts_at: datetime


def get_level_policy() -> LevelPolicy | None:
    version = os.getenv("VOLUNTEER_LEVEL_POLICY_VERSION", "").strip()
    starts_at_text = os.getenv("VOLUNTEER_LEVEL_POLICY_START_AT", "").strip()
    if not version and not starts_at_text:
        return None
    if not version or len(version) > 64 or not starts_at_text:
        raise ValueError("Volunteer level evaluation requires a policy version and a UTC start time")
    try:
        starts_at = datetime.fromisoformat(starts_at_text)
    except ValueError:
        raise ValueError("Volunteer level policy start must be an ISO 8601 UTC timestamp") from None
    if starts_at.utcoffset() != timedelta(0):
        raise ValueError("Volunteer level policy start must be an ISO 8601 UTC timestamp")
    return LevelPolicy(version=version, starts_at=starts_at)


def eligible_walk(walk, policy: LevelPolicy | None, now: datetime) -> bool:
    return (
        policy is not None
        and now >= policy.starts_at
        and as_utc(walk.started_at) >= policy.starts_at
        and walk.status == "COMPLETED"
        and walk.end_reason == "USER"
        and walk.handover_status == "SELF_REPORTED"
        and walk.handover.volunteer_user_id == walk.walk_request.user_id
        and walk.handover.level_evaluation_pending_at is not None
    )


def level_evaluation_for_summary(walk, actor):
    if actor.role != UserRole.USER:
        return "NOT_ELIGIBLE", None
    evaluation = walk.level_evaluation
    if evaluation is not None and evaluation.user_id == actor.id:
        return evaluation.status, evaluation.event if evaluation.status == "COMPLETED" else None
    return ("PENDING" if eligible_walk(walk, get_level_policy(), utc_now()) else "NOT_ELIGIBLE"), None


def pending_candidates(db, policy: LevelPolicy, now: datetime):
    evaluation = VolunteerLevelEvaluation
    return (
        db.query(Walk.id, WalkRequest.user_id)
        .join(WalkRequest, WalkRequest.id == Walk.walk_request_id)
        .join(User, User.id == WalkRequest.user_id)
        .join(WalkHandover, WalkHandover.walk_id == Walk.id)
        .outerjoin(evaluation, evaluation.walk_id == Walk.id)
        .filter(
            Walk.started_at >= policy.starts_at,
            Walk.status == "COMPLETED",
            Walk.end_reason == "USER",
            Walk.handover_required.is_(True),
            WalkHandover.handover_result == "COMPLETE",
            WalkHandover.level_evaluation_pending_at.is_not(None),
            WalkHandover.volunteer_user_id == WalkRequest.user_id,
            User.role == UserRole.USER,
            or_(
                evaluation.walk_id.is_(None),
                and_(evaluation.user_id == WalkRequest.user_id, evaluation.status == "FAILED",
                     evaluation.next_retry_at <= now),
            ),
        )
        .order_by(WalkHandover.level_evaluation_pending_at, Walk.id)
        .limit(BATCH_SIZE)
        .all()
    )


def lock_pending_walk(db, walk_id: int, user_id: int, policy: LevelPolicy, now: datetime):
    # Share the existing NOWAIT User and Walk locks. The per-user lock serializes credit and level
    # updates across workers without introducing a blocking dependency on automatic walk closure.
    user = lock_row(db, User, user_id)
    if user is None or user.role != UserRole.USER:
        return None
    walk = lock_row(db, Walk, walk_id)
    if walk is None or walk.walk_request.user_id != user_id or not eligible_walk(walk, policy, now):
        return None
    evaluation = walk.level_evaluation
    if evaluation is not None and (
        evaluation.user_id != user_id or evaluation.status == "COMPLETED" or as_utc(evaluation.next_retry_at) > now
    ):
        return None
    return walk


def save_evaluation(db, walk, policy: LevelPolicy, now: datetime, *, retry_at: datetime | None = None):
    evaluation = walk.level_evaluation
    if evaluation is None:
        evaluation = VolunteerLevelEvaluation(walk=walk, user_id=walk.walk_request.user_id)
        db.add(evaluation)
    evaluation.status = "COMPLETED" if retry_at is None else "FAILED"
    evaluation.policy_version = policy.version
    evaluation.evaluated_at = now
    evaluation.next_retry_at = retry_at
    return evaluation


def evaluate_walk(db, walk_id: int, user_id: int, policy: LevelPolicy, now: datetime) -> bool:
    walk = lock_pending_walk(db, walk_id, user_id, policy, now)
    if walk is None:
        return False
    credited = db.query(func.count(VolunteerLevelEvaluation.walk_id)).filter(
        VolunteerLevelEvaluation.user_id == user_id, VolunteerLevelEvaluation.status == "COMPLETED",
    ).scalar()
    target_level = bisect_right(LEVEL_THRESHOLDS, credited + 1)
    profile = locked_profile(db, user_id, create=True)
    evaluation = save_evaluation(db, walk, policy, now)
    if target_level > profile.level:
        evaluation.event = VolunteerLevelEvent(
            before_level=profile.level, after_level=target_level, policy_version=policy.version, earned_at=now,
        )
        profile.level = target_level
    return True


def record_failure(db, walk_id: int, user_id: int, policy: LevelPolicy, now: datetime):
    # Use the same lock and re-read: another worker may have committed a successful evaluation
    # after the failing transaction rolled back.
    walk = lock_pending_walk(db, walk_id, user_id, policy, now)
    if walk is not None:
        save_evaluation(db, walk, policy, now, retry_at=now + RETRY_DELAY)


def run_pending_evaluations(session_factory, policy: LevelPolicy, *, now: datetime | None = None) -> int:
    now = utc_now() if now is None else now
    if now < policy.starts_at:
        return 0
    with session_factory() as db:
        candidates = pending_candidates(db, policy, now)
    completed = 0
    for walk_id, user_id in candidates:
        try:
            with session_factory.begin() as db:
                evaluated = evaluate_walk(db, walk_id, user_id, policy, now)
        except Exception as exc:
            if getattr(getattr(exc, "orig", None), "pgcode", None) in RETRYABLE_LOCK_CODES:
                continue  # no write was lost; a later poll can acquire the lock
            logger.warning("Volunteer level evaluation failed for walk %s (%s)", walk_id, type(exc).__name__)
            try:
                with session_factory.begin() as db:
                    record_failure(db, walk_id, user_id, policy, now)
            except Exception as recovery_error:
                logger.warning("Volunteer level failure state could not be saved for walk %s (%s)",
                               walk_id, type(recovery_error).__name__)
        else:
            completed += int(evaluated)
    return completed
