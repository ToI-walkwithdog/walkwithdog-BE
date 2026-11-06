"""Walk recording with the Issue #3 lock order, then the concrete Walk row.

Provisional GPS policy: known accuracy <=50m, segment speed <=5m/s, no distance
bridge over sampling gaps >300s. Rejected batches never partially persist.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from math import asin, cos, radians, sin, sqrt

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from app.dependencies import require_shelter_access
from app.models import Walk, WalkIncident, WalkMarker, WalkPoint, WalkRequest
from app.walk_request_service import (
    as_utc, locked_profile, locked_request, lock_row, validate_eligibility, validate_walk_start,
)

EARTH_RADIUS_M = 6371000.0  # Spherical approximation, not a surveyed/geodesic distance.
MAX_ACCURACY_M = Decimal("50")
MAX_SPEED_M_S = 5.0
MAX_DISTANCE_GAP_SECONDS = 300


def utc_now():
    return datetime.now(timezone.utc)


def distance_m(first, second):
    lat1, lat2 = radians(float(first.latitude)), radians(float(second.latitude))
    dlat = lat2 - lat1
    dlon = radians(float(second.longitude) - float(first.longitude))
    hav = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(min(1.0, max(0.0, hav))))


def start_walk(db, actor, payload):
    request = validate_walk_start(db, payload.walk_request_id, actor)
    now = utc_now()
    if not as_utc(request.requested_start_at) <= now < as_utc(request.requested_end_at):
        raise HTTPException(409, "승인된 예약 시간 안에서만 산책을 시작할 수 있습니다.")
    profile = locked_profile(db, request.user_id)
    validate_eligibility(db, request.dog, request.user, profile, request.requested_start_at,
                         request.requested_end_at, shelter_id=request.shelter_id, require_future=False)
    active = db.query(Walk.id).join(WalkRequest).filter(
        Walk.status == "IN_PROGRESS",
        or_(WalkRequest.dog_id == request.dog_id, WalkRequest.user_id == request.user_id),
    ).first()
    if active:
        raise HTTPException(409, "강아지 또는 봉사자가 이미 산책 중입니다.")
    walk = Walk(walk_request_id=request.id, status="IN_PROGRESS", started_at=now)
    db.add(walk)
    db.flush()
    return walk


def locked_walk(db, walk_id, actor, allow_staff=False):
    hint = db.query(Walk.walk_request_id, WalkRequest.user_id).join(WalkRequest).filter(Walk.id == walk_id).first()
    if hint is None:
        raise HTTPException(404, "Walk not found")
    action = "CANCEL" if allow_staff and actor.id != hint.user_id else "START"
    request, _, _ = locked_request(db, hint.walk_request_id, actor, action)
    walk = lock_row(db, Walk, walk_id)
    if walk is None:
        raise HTTPException(404, "Walk not found")
    if walk.walk_request_id != request.id:
        raise HTTPException(409, "산책 신청 연결이 변경되었습니다. 다시 조회하세요.")
    if walk.status != "IN_PROGRESS":
        raise HTTPException(409, "진행 중인 산책만 기록하거나 종료할 수 있습니다.")
    return walk, request


def validate_event_time(walk, recorded_at, now):
    stamp = as_utc(recorded_at)
    if not as_utc(walk.started_at) <= stamp <= now:
        raise HTTPException(409, "기록 시각은 산책 시작 이후이며 서버 현재 시각 이하여야 합니다.")
    return stamp


def point_key(point):
    return (point.latitude, point.longitude, as_utc(point.recorded_at), point.accuracy_m)


def append_points(db, walk_id, actor, payload):
    walk, _ = locked_walk(db, walk_id, actor)
    now = utc_now()
    incoming = {}
    for point in payload.points:
        if point.sequence_no in incoming and point_key(incoming[point.sequence_no]) != point_key(point):
            raise HTTPException(409, "같은 순번에 다른 좌표 내용이 전송되었습니다.")
        incoming[point.sequence_no] = point
    existing = {point.sequence_no: point for point in db.query(WalkPoint).filter(
        WalkPoint.walk_id == walk.id, WalkPoint.sequence_no.in_(list(incoming)),
    )}
    previous = db.query(WalkPoint).filter_by(walk_id=walk.id).order_by(WalkPoint.sequence_no.desc()).first()
    added = []
    for sequence, point in sorted(incoming.items()):
        if sequence in existing:
            if point_key(existing[sequence]) != point_key(point):
                raise HTTPException(409, "이미 저장된 순번의 내용을 변경할 수 없습니다.")
            continue
        expected = previous.sequence_no + 1 if previous else 0
        if sequence != expected:
            raise HTTPException(409, f"좌표 순번이 연속되지 않습니다. 다음 순번은 {expected}입니다.")
        stamp = validate_event_time(walk, point.recorded_at, now)
        if point.accuracy_m is not None and point.accuracy_m > MAX_ACCURACY_M:
            raise HTTPException(409, "GPS 오차가 허용 범위 50m를 초과합니다.")
        if previous:
            elapsed = (stamp - as_utc(previous.recorded_at)).total_seconds()
            if elapsed <= 0:
                raise HTTPException(409, "좌표 기록 시각은 순번에 따라 증가해야 합니다.")
            if distance_m(previous, point) / elapsed > MAX_SPEED_M_S:
                raise HTTPException(409, "비정상적인 GPS 위치 이동이 감지되었습니다.")
        row = WalkPoint(walk_id=walk.id, latitude=point.latitude, longitude=point.longitude,
                        recorded_at=stamp, accuracy_m=point.accuracy_m, sequence_no=sequence)
        added.append(row)
        previous = row
    db.add_all(added)
    db.flush()
    return {"walk_id": walk.id, "accepted_count": len(added),
            "duplicate_count": len(payload.points) - len(added),
            "last_sequence_no": previous.sequence_no}


def add_marker(db, walk_id, actor, payload):
    walk, _ = locked_walk(db, walk_id, actor)
    data = payload.model_dump()
    data["recorded_at"] = validate_event_time(walk, payload.recorded_at, utc_now())
    marker = WalkMarker(walk_id=walk.id, **data)
    db.add(marker)
    db.flush()
    return marker


def report_incident(db, walk_id, actor, payload):
    walk, _ = locked_walk(db, walk_id, actor, allow_staff=True)
    data = payload.model_dump()
    data["occurred_at"] = validate_event_time(walk, payload.occurred_at, utc_now())
    incident = WalkIncident(walk_id=walk.id, reported_by=actor.id, status="OPEN", **data)
    db.add(incident)
    db.flush()
    return incident


def total_distance(points):
    """Stream adjacent raw observations; skip unmeasured or implausible bridges."""
    total, previous = 0.0, None
    for point in points:
        if previous is not None:
            elapsed = (as_utc(point.recorded_at) - as_utc(previous.recorded_at)).total_seconds()
            length = distance_m(previous, point)
            reliable = all(p.accuracy_m is None or p.accuracy_m <= MAX_ACCURACY_M for p in (previous, point))
            if reliable and 0 < elapsed <= MAX_DISTANCE_GAP_SECONDS and length / elapsed <= MAX_SPEED_M_S:
                total += length
        previous = point
    return Decimal(str(total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def end_walk(db, walk_id, actor, payload):
    walk, request = locked_walk(db, walk_id, actor, allow_staff=True)
    if actor.id != request.user_id and payload.status != "INTERRUPTED":
        raise HTTPException(403, "보호소와 관리자는 안전 중단만 처리할 수 있습니다.")
    now = utc_now()
    if now < as_utc(walk.started_at):
        raise HTTPException(409, "서버 종료 시각이 산책 시작 시각보다 빠릅니다.")
    for model, timestamp in ((WalkPoint, WalkPoint.recorded_at), (WalkMarker, WalkMarker.recorded_at),
                             (WalkIncident, WalkIncident.occurred_at)):
        latest = db.query(func.max(timestamp)).filter(model.walk_id == walk.id).scalar()
        if latest and as_utc(latest) > now:
            raise HTTPException(409, "마지막 관찰 시각보다 빠르게 종료할 수 없습니다.")
    points = db.query(WalkPoint).filter_by(walk_id=walk.id).order_by(WalkPoint.sequence_no).yield_per(1000)
    walk.total_distance_m = total_distance(points)
    walk.duration_seconds = int((now - as_utc(walk.started_at)).total_seconds())
    walk.average_pace = ((Decimal(walk.duration_seconds) * 1000 / walk.total_distance_m).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
        if walk.total_distance_m > 0 and walk.duration_seconds > 0 else None)
    if walk.average_pace is not None and walk.average_pace > Decimal("9999999999.99"):
        walk.average_pace = None  # Unrepresentable pace is unknown, never truncated.
    walk.status, walk.ended_at = payload.status, now
    db.flush()
    return walk


def walk_detail(db, walk_id, actor, limit, offset):
    walk = db.query(Walk).options(joinedload(Walk.walk_request)).filter_by(id=walk_id).first()
    if walk is None:
        raise HTTPException(404, "Walk not found")
    request = walk.walk_request
    if actor.id != request.user_id:
        require_shelter_access(actor, request.shelter_id)
    result = {"walk": walk, "user_id": request.user_id, "dog_id": request.dog_id,
              "shelter_id": request.shelter_id, "limit": limit, "offset": offset}
    for name, model, ordering in (
        ("points", WalkPoint, (WalkPoint.sequence_no,)),
        ("markers", WalkMarker, (WalkMarker.recorded_at, WalkMarker.id)),
        ("incidents", WalkIncident, (WalkIncident.occurred_at, WalkIncident.id)),
    ):
        query = db.query(model).filter_by(walk_id=walk.id)
        result[{"points": "point_count", "markers": "marker_count", "incidents": "incident_count"}[name]] = query.count()
        result[name] = query.order_by(*ordering).limit(limit).offset(offset).all()
    return result
