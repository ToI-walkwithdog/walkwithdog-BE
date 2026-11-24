import os

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.models import Shelter, User, UserRole, Walk, WalkIncident, WalkMarker, WalkPoint, WalkRequest, WalkReview, WalkReviewTag
from app.walk_models import WalkPrivateMemo
from app.walk_record_schemas import WalkIncidentResponse, WalkMarkerResponse
from app.walk_review_schemas import (
    SummaryMetrics,
    SummaryPoint,
    SummaryWalkInfo,
    WalkPrivateMemoWrite,
    WalkReviewCreate,
    WalkReviewResponse,
    WalkSummaryResponse,
)
from app.walk_service import lock_handover_walk, utc_now


def get_walk_summary_max_points() -> int:
    val = os.getenv("WALK_SUMMARY_MAX_POINTS", "2000")
    try:
        return int(val)
    except ValueError:
        return 2000


def format_shelter_link(url: str | None) -> str | None:
    if not url:
        return None
    trimmed = url.strip()
    if not trimmed:
        return None
    if not (trimmed.startswith("http://") or trimmed.startswith("https://")):
        return None
    return trimmed


def get_walk_summary(db: Session, walk_id: int, actor: User) -> WalkSummaryResponse:
    walk = (
        db.query(Walk)
        .options(
            joinedload(Walk.walk_request).joinedload(WalkRequest.shelter),
            joinedload(Walk.review).joinedload(WalkReview.tags),
            joinedload(Walk.handover),
        )
        .filter(Walk.id == walk_id)
        .first()
    )
    if walk is None:
        raise HTTPException(404, "Walk not found")
    if walk.walk_request.user_id != actor.id:
        raise HTTPException(403, "본인의 산책만 요약을 조회할 수 있습니다.")
    if walk.status not in ("COMPLETED", "INTERRUPTED"):
        raise HTTPException(409, "종료된 산책만 요약을 조회할 수 있습니다.")

    max_points = get_walk_summary_max_points()
    points_total = db.query(func.count(WalkPoint.id)).filter(WalkPoint.walk_id == walk.id).scalar() or 0

    if points_total <= max_points:
        rows = (
            db.query(WalkPoint.latitude, WalkPoint.longitude)
            .filter(WalkPoint.walk_id == walk.id)
            .order_by(WalkPoint.sequence_no.asc())
            .all()
        )
        is_downsampled = False
    else:
        # 천장 나눗셈으로 균등한 샘플링 간격 계산 (첫 점과 마지막 점 보존)
        step = max(1, -(-points_total // max_points))
        rows = (
            db.query(WalkPoint.latitude, WalkPoint.longitude)
            .filter(
                WalkPoint.walk_id == walk.id,
                or_(
                    WalkPoint.sequence_no % step == 0,
                    WalkPoint.sequence_no == points_total - 1,
                ),
            )
            .order_by(WalkPoint.sequence_no.asc())
            .all()
        )
        is_downsampled = True

    points = [SummaryPoint(latitude=lat, longitude=lon) for lat, lon in rows]
    points_returned = len(points)

    # marker_count는 walk_markers만 카운트 (위험 신고는 미포함)
    raw_markers = (
        db.query(WalkMarker)
        .filter(WalkMarker.walk_id == walk.id)
        .order_by(WalkMarker.recorded_at.asc(), WalkMarker.id.asc())
        .all()
    )
    marker_count = len(raw_markers)
    markers = [WalkMarkerResponse.model_validate(m) for m in raw_markers]

    raw_incidents = (
        db.query(WalkIncident)
        .filter(WalkIncident.walk_id == walk.id)
        .order_by(WalkIncident.occurred_at.asc(), WalkIncident.id.asc())
        .all()
    )
    incidents = [WalkIncidentResponse.model_validate(inc) for inc in raw_incidents]

    review_resp = None
    if walk.review is not None:
        tag_codes = [t.tag_code for t in walk.review.tags]
        review_resp = WalkReviewResponse(
            id=walk.review.id,
            walk_id=walk.id,
            rating=walk.review.rating,
            condition_tags=tag_codes,
            comment=walk.review.comment,
            created_at=walk.review.created_at,
            updated_at=walk.review.updated_at,
        )

    shelter_url = walk.walk_request.shelter.website_url if walk.walk_request.shelter else None
    shelter_link = format_shelter_link(shelter_url)

    summary_metrics = SummaryMetrics(
        duration_seconds=walk.duration_seconds,
        total_distance_m=walk.total_distance_m,
        marker_count=marker_count,
    )
    walk_info = SummaryWalkInfo(
        id=walk.id,
        status=walk.status,
        started_at=walk.started_at,
        ended_at=walk.ended_at,
    )

    return WalkSummaryResponse(
        walk=walk_info,
        handover_status=walk.handover_status,
        private_memo=walk.private_memo if actor.role == UserRole.USER else None,
        summary=summary_metrics,
        points=points,
        points_total=points_total,
        points_returned=points_returned,
        is_downsampled=is_downsampled,
        markers=markers,
        incidents=incidents,
        shelter_link=shelter_link,
        review=review_resp,
    )


def upsert_walk_private_memo(
    db: Session, walk_id: int, actor: User, payload: WalkPrivateMemoWrite,
) -> tuple[WalkPrivateMemo, bool]:
    # This lock already handles owner checks and serializes this write with handover and other memo requests.
    walk = lock_handover_walk(db, walk_id, actor)
    if walk.status not in ("COMPLETED", "INTERRUPTED"):
        raise HTTPException(409, "종료된 산책만 개인 메모를 저장할 수 있습니다.")
    if walk.handover_status not in ("SELF_REPORTED", "ISSUE_REPORTED", "NOT_REQUIRED"):
        raise HTTPException(409, "인계를 접수한 뒤 개인 메모를 저장할 수 있습니다.")

    memo = walk.private_memo
    if memo is not None and memo.memo == payload.memo:
        return memo, False

    current_version = memo.version if memo is not None else 0
    if payload.expected_version != current_version:
        raise HTTPException(409, "메모가 변경되었습니다. 최신 메모를 조회해 주세요.")

    now = utc_now()
    created = memo is None
    if created:
        memo = WalkPrivateMemo(
            walk_id=walk.id, author_user_id=actor.id, memo=payload.memo, version=1,
            created_at=now, updated_at=now,
        )
        db.add(memo)
    else:
        memo.memo = payload.memo
        memo.version += 1
        memo.updated_at = now
    db.flush()
    return memo, created


def upsert_walk_review(db: Session, walk_id: int, actor: User, payload: WalkReviewCreate) -> tuple[WalkReviewResponse, bool]:
    walk = (
        db.query(Walk)
        .options(
            joinedload(Walk.walk_request),
            joinedload(Walk.review).joinedload(WalkReview.tags),
        )
        .filter(Walk.id == walk_id)
        .first()
    )
    if walk is None:
        raise HTTPException(404, "Walk not found")
    if walk.walk_request.user_id != actor.id:
        raise HTTPException(403, "본인의 산책만 평가할 수 있습니다.")
    if walk.status not in ("COMPLETED", "INTERRUPTED"):
        raise HTTPException(409, "종료된 산책만 평가할 수 있습니다.")

    review = walk.review
    is_created = False
    if review is None:
        review = WalkReview(
            walk_id=walk.id,
            rating=payload.rating,
            comment=payload.comment,
        )
        db.add(review)
        for code in payload.condition_tags:
            review.tags.append(WalkReviewTag(tag_code=code))
        db.flush()
        is_created = True
    else:
        review.rating = payload.rating
        review.comment = payload.comment
        review.tags.clear()
        for code in payload.condition_tags:
            review.tags.append(WalkReviewTag(tag_code=code))
        db.flush()

    tags = [t.tag_code for t in review.tags]
    response = WalkReviewResponse(
        id=review.id,
        walk_id=walk.id,
        rating=review.rating,
        condition_tags=tags,
        comment=review.comment,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )
    return response, is_created


def get_walk_review(db: Session, walk_id: int, actor: User) -> WalkReviewResponse | None:
    walk = (
        db.query(Walk)
        .options(
            joinedload(Walk.walk_request),
            joinedload(Walk.review).joinedload(WalkReview.tags),
        )
        .filter(Walk.id == walk_id)
        .first()
    )
    if walk is None:
        raise HTTPException(404, "Walk not found")
    if walk.walk_request.user_id != actor.id:
        raise HTTPException(403, "본인의 산책만 평가를 조회할 수 있습니다.")

    if walk.review is None:
        return None

    tags = [t.tag_code for t in walk.review.tags]
    return WalkReviewResponse(
        id=walk.review.id,
        walk_id=walk.id,
        rating=walk.review.rating,
        condition_tags=tags,
        comment=walk.review.comment,
        created_at=walk.review.created_at,
        updated_at=walk.review.updated_at,
    )
