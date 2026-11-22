from datetime import datetime
import os

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import walk_service
from app.dependencies import require_shelter_access
from app.models import Dog, User, UserRole, Walk, WalkIncident, WalkPoint, WalkRequest
from app.walk_live_schemas import (
    LiveWalkCardResponse,
    LiveWalkDetailResponse,
    LiveWalkMarkerResponse,
    LiveWalkPointResponse,
    LiveWalkUpdatesResponse,
)


def get_live_walk_stale_seconds() -> int:
    val = os.getenv("LIVE_WALK_STALE_SECONDS", "60")
    try:
        return int(val)
    except ValueError:
        return 60


def compute_walk_live_status(walk: Walk, now: datetime, stale_threshold: int) -> tuple[int, datetime | None, bool]:
    elapsed_seconds = max(0, int((now - walk_service.as_utc(walk.started_at)).total_seconds()))
    last_location_at = walk.last_point_recorded_at
    if last_location_at is not None:
        seconds_since_update = (now - walk_service.as_utc(last_location_at)).total_seconds()
        is_stale = seconds_since_update >= stale_threshold
    else:
        seconds_since_start = (now - walk_service.as_utc(walk.started_at)).total_seconds()
        is_stale = seconds_since_start >= stale_threshold
    return elapsed_seconds, last_location_at, is_stale


def build_live_walk_markers(
    db: Session,
    walk: Walk,
    points: list[WalkPoint] | None = None,
) -> list[LiveWalkMarkerResponse]:
    markers: list[LiveWalkMarkerResponse] = []

    # 1. START marker from first point coordinates
    first_point = None
    if points:
        first_point = points[0]
    else:
        first_point = (
            db.query(WalkPoint)
            .filter(WalkPoint.walk_id == walk.id)
            .order_by(WalkPoint.sequence_no.asc())
            .first()
        )

    if first_point is not None:
        markers.append(
            LiveWalkMarkerResponse(
                marker_type="START",
                latitude=first_point.latitude,
                longitude=first_point.longitude,
                occurred_at=first_point.recorded_at,
                incident_type=None,
                description=None,
            )
        )

    # 2. INCIDENT markers from walk_incidents
    incidents = (
        db.query(WalkIncident)
        .filter(WalkIncident.walk_id == walk.id)
        .order_by(WalkIncident.occurred_at.asc(), WalkIncident.id.asc())
        .all()
    )
    for inc in incidents:
        markers.append(
            LiveWalkMarkerResponse(
                marker_type="INCIDENT",
                latitude=inc.latitude,
                longitude=inc.longitude,
                occurred_at=inc.occurred_at,
                incident_type=inc.incident_type,
                description=inc.description,
            )
        )

    return markers


def get_live_walks(
    db: Session,
    actor: User,
    shelter_id: int | None = None,
) -> list[LiveWalkCardResponse]:
    if actor.role == UserRole.ADMIN:
        target_shelter_id = shelter_id if shelter_id is not None else actor.shelter_id
        if target_shelter_id is None:
            raise HTTPException(400, "관리자는 shelter_id 쿼리 파라미터를 지정해야 합니다.")
    elif actor.role == UserRole.SHELTER:
        if not actor.shelter_id:
            raise HTTPException(403, "보호소 정보가 없습니다.")
        if shelter_id is not None and shelter_id != actor.shelter_id:
            raise HTTPException(403, "다른 보호소의 산책은 조회할 수 없습니다.")
        target_shelter_id = actor.shelter_id
    else:
        raise HTTPException(403, "보호소 계정만 실시간 산책을 조회할 수 있습니다.")

    rows = (
        db.query(Walk, WalkRequest, Dog, User)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .join(Dog, WalkRequest.dog_id == Dog.id)
        .join(User, WalkRequest.user_id == User.id)
        .filter(
            Walk.status == "IN_PROGRESS",
            WalkRequest.shelter_id == target_shelter_id,
        )
        .order_by(Walk.started_at.desc(), Walk.id.desc())
        .all()
    )

    now = walk_service.utc_now()
    stale_sec = get_live_walk_stale_seconds()
    cards = []
    for walk, req, dog, walker in rows:
        elapsed_seconds, last_location_at, is_stale = compute_walk_live_status(walk, now, stale_sec)
        scheduled_duration_minutes = int(
            (walk_service.as_utc(req.requested_end_at) - walk_service.as_utc(req.requested_start_at)).total_seconds() // 60
        )
        cards.append(
            LiveWalkCardResponse(
                walk_id=walk.id,
                dog_id=dog.id,
                dog_name=dog.name,
                dog_image_url=dog.image_url,
                walker_nickname=walker.nickname,
                started_at=walk.started_at,
                elapsed_seconds=elapsed_seconds,
                scheduled_duration_minutes=scheduled_duration_minutes,
                scheduled_end_at=req.requested_end_at,
                last_latitude=walk.last_latitude,
                last_longitude=walk.last_longitude,
                last_location_at=last_location_at,
                is_stale=is_stale,
            )
        )
    return cards


def get_live_walk_detail(
    db: Session,
    walk_id: int,
    actor: User,
) -> LiveWalkDetailResponse:
    row = (
        db.query(Walk, WalkRequest, Dog, User)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .join(Dog, WalkRequest.dog_id == Dog.id)
        .join(User, WalkRequest.user_id == User.id)
        .filter(Walk.id == walk_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "진행 중인 산책을 찾을 수 없습니다.")

    walk, req, dog, walker = row

    if walk.status != "IN_PROGRESS":
        raise HTTPException(404, "진행 중인 산책을 찾을 수 없습니다.")

    require_shelter_access(actor, req.shelter_id)

    points = (
        db.query(WalkPoint)
        .filter(WalkPoint.walk_id == walk.id)
        .order_by(WalkPoint.sequence_no.asc())
        .all()
    )
    route = [
        LiveWalkPointResponse(
            sequence_no=p.sequence_no,
            latitude=p.latitude,
            longitude=p.longitude,
            recorded_at=p.recorded_at,
        )
        for p in points
    ]
    markers = build_live_walk_markers(db, walk, points=points)

    now = walk_service.utc_now()
    stale_sec = get_live_walk_stale_seconds()
    elapsed_seconds, last_location_at, is_stale = compute_walk_live_status(walk, now, stale_sec)
    scheduled_duration_minutes = int(
        (walk_service.as_utc(req.requested_end_at) - walk_service.as_utc(req.requested_start_at)).total_seconds() // 60
    )

    return LiveWalkDetailResponse(
        walk_id=walk.id,
        dog_id=dog.id,
        dog_name=dog.name,
        dog_image_url=dog.image_url,
        walker_nickname=walker.nickname,
        started_at=walk.started_at,
        elapsed_seconds=elapsed_seconds,
        scheduled_duration_minutes=scheduled_duration_minutes,
        scheduled_end_at=req.requested_end_at,
        last_latitude=walk.last_latitude,
        last_longitude=walk.last_longitude,
        last_location_at=last_location_at,
        is_stale=is_stale,
        route=route,
        markers=markers,
        walker_phone=walker.phone,
    )


def get_live_walk_updates(
    db: Session,
    walk_id: int,
    after_seq: int | None,
    actor: User,
) -> LiveWalkUpdatesResponse:
    row = (
        db.query(Walk, WalkRequest)
        .join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .filter(Walk.id == walk_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "진행 중인 산책을 찾을 수 없습니다.")

    walk, req = row

    if walk.status != "IN_PROGRESS":
        raise HTTPException(404, "진행 중인 산책을 찾을 수 없습니다.")

    require_shelter_access(actor, req.shelter_id)

    query = db.query(WalkPoint).filter(WalkPoint.walk_id == walk.id)
    if after_seq is not None:
        query = query.filter(WalkPoint.sequence_no > after_seq)
    points = query.order_by(WalkPoint.sequence_no.asc()).all()

    route = [
        LiveWalkPointResponse(
            sequence_no=p.sequence_no,
            latitude=p.latitude,
            longitude=p.longitude,
            recorded_at=p.recorded_at,
        )
        for p in points
    ]
    markers = build_live_walk_markers(db, walk)

    now = walk_service.utc_now()
    stale_sec = get_live_walk_stale_seconds()
    _, last_location_at, is_stale = compute_walk_live_status(walk, now, stale_sec)

    return LiveWalkUpdatesResponse(
        walk_id=walk.id,
        is_stale=is_stale,
        last_location_at=last_location_at,
        last_latitude=walk.last_latitude,
        last_longitude=walk.last_longitude,
        route=route,
        markers=markers,
    )
