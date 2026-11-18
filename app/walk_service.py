"""Walk recording with the Issue #3 lock order, then the concrete Walk row.

Provisional GPS policy: known accuracy <=50m, segment speed <=5m/s, no distance
bridge over sampling gaps >300s. Rejected batches never partially persist.
"""
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.dependencies import require_shelter_access
from app.models import Walk, WalkIncident, WalkMarker, WalkPoint, WalkRequest
from app.volunteer_stats import refresh_volunteer_profile
from app.gps_geometry import EARTH_RADIUS_M, distance_m
from app.gps_motion_analysis import analyze_motion
from app.gps_motion_config import MAX_SPEED_M_S
from app.walk_request_service import (
    as_utc, locked_profile, locked_request, lock_row, validate_eligibility, validate_walk_start,
)

MAX_ACCURACY_M = Decimal("50")
MAX_DISTANCE_GAP_SECONDS = 300


def utc_now():
    return datetime.now(timezone.utc)


def get_auto_end_after_seconds():
    return int(os.getenv("AUTO_END_AFTER_SECONDS", "3600"))


def is_walk_expired(walk, now):
    last_active = as_utc(walk.last_point_received_at or walk.started_at)
    return (now - last_active).total_seconds() > get_auto_end_after_seconds()


INCIDENT_TYPE_LABELS = {
    "LOST_DOG": "도주",
    "INJURY": "부상",
    "POOP_NO_BAG": "똥 쌈(배변봉투 없음)",
    "ATE_FOREIGN_OBJECT": "이상한 것 주워먹음",
    "ABNORMAL_BEHAVIOR": "이상행동",
    "SAFETY_THREAT": "안전 위협",
    "OTHER": "기타",
}


def get_incident_duplicate_window_seconds():
    val = os.getenv("INCIDENT_DUPLICATE_WINDOW_SECONDS", "180")
    try:
        return int(val)
    except ValueError:
        return 180


def get_incident_late_report_hours():
    val = os.getenv("INCIDENT_LATE_REPORT_HOURS", "24")
    try:
        return int(val)
    except ValueError:
        return 24


def start_walk(db, actor, payload):
    request = validate_walk_start(db, payload.walk_request_id, actor)
    now = utc_now()
    close_stale_walks(db, now=now, user_id=actor.id)
    if not as_utc(request.requested_start_at) <= now < as_utc(request.requested_end_at):
        raise HTTPException(409, "승인된 예약 시간 안에서만 산책을 시작할 수 있습니다.")
    locked_profile(db, request.user_id)
    validate_eligibility(db, request.dog, request.user, request.requested_start_at,
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
    if is_walk_expired(walk, now):
        raise HTTPException(409, "산책 세션이 만료되었습니다.")
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
    if added:
        db.add_all(added)
        last_point = added[-1]
        walk.last_latitude = last_point.latitude
        walk.last_longitude = last_point.longitude
        walk.last_accuracy_m = last_point.accuracy_m
        walk.last_point_recorded_at = last_point.recorded_at
        walk.last_point_received_at = now
    db.flush()
    return {"walk_id": walk.id, "accepted_count": len(added),
            "duplicate_count": len(payload.points) - len(added),
            "last_sequence_no": previous.sequence_no if previous else 0}


def add_marker(db, walk_id, actor, payload):
    walk, _ = locked_walk(db, walk_id, actor)
    data = payload.model_dump()
    data["recorded_at"] = validate_event_time(walk, payload.recorded_at, utc_now())
    marker = WalkMarker(walk_id=walk.id, **data)
    db.add(marker)
    db.flush()
    return marker


def locked_walk_for_incident(db, walk_id, actor, occurred_at):
    hint = db.query(Walk.walk_request_id, WalkRequest.user_id).join(WalkRequest).filter(Walk.id == walk_id).first()
    if hint is None:
        raise HTTPException(404, "Walk not found")
    action = "CANCEL" if actor.id != hint.user_id else "START"
    request, _, _ = locked_request(db, hint.walk_request_id, actor, action)
    walk = lock_row(db, Walk, walk_id)
    if walk is None:
        raise HTTPException(404, "Walk not found")
    if walk.walk_request_id != request.id:
        raise HTTPException(409, "산책 신청 연결이 변경되었습니다. 다시 조회하세요.")

    now = utc_now()
    stamp = as_utc(occurred_at)
    started = as_utc(walk.started_at)

    if walk.status == "IN_PROGRESS":
        if not (started <= stamp <= now):
            raise HTTPException(409, "기록 시각은 산책 시작 이후이며 서버 현재 시각 이하여야 합니다.")
    elif walk.status in ("COMPLETED", "INTERRUPTED"):
        if walk.ended_at is None:
            raise HTTPException(409, "종료 시각 정보가 없는 산책입니다.")
        ended = as_utc(walk.ended_at)
        late_limit_seconds = get_incident_late_report_hours() * 3600
        if (now - ended).total_seconds() > late_limit_seconds:
            raise HTTPException(409, "종료된 산책의 신고 가능 시간이 초과되었습니다.")
        if not (started <= stamp <= ended):
            raise HTTPException(409, "종료된 산책의 기록 시각은 산책 시작 이후이며 종료 시각 이하여야 합니다.")
    else:
        raise HTTPException(409, "취소되었거나 유효하지 않은 산책 상태입니다.")

    return walk, request, stamp


def is_same_incident(existing, walk_id, incident_type, description, lat, lng, occurred_utc):
    if existing.walk_id != walk_id or existing.incident_type != incident_type:
        return False
    if existing.description != description:
        return False
    if as_utc(existing.occurred_at) != occurred_utc:
        return False
    ex_lat = float(existing.latitude) if existing.latitude is not None else None
    ex_lng = float(existing.longitude) if existing.longitude is not None else None
    tgt_lat = float(lat) if lat is not None else None
    tgt_lng = float(lng) if lng is not None else None
    if (ex_lat is None) != (tgt_lat is None) or (ex_lng is None) != (tgt_lng is None):
        return False
    if ex_lat is not None and abs(ex_lat - tgt_lat) > 1e-5:
        return False
    if ex_lng is not None and abs(ex_lng - tgt_lng) > 1e-5:
        return False
    return True


def report_incident(db, walk_id, actor, payload):
    occurred_utc = as_utc(payload.occurred_at)
    desc = payload.description.strip() if payload.description and payload.description.strip() else INCIDENT_TYPE_LABELS.get(payload.incident_type, payload.incident_type)

    if payload.client_report_id:
        existing = db.query(WalkIncident).filter_by(
            reported_by=actor.id, client_report_id=payload.client_report_id
        ).first()
        if existing:
            lat = payload.latitude if payload.latitude is not None else existing.latitude
            lng = payload.longitude if payload.longitude is not None else existing.longitude
            if is_same_incident(existing, walk_id, payload.incident_type, desc, lat, lng, occurred_utc):
                return existing, False
            raise HTTPException(409, "이미 동일한 client_report_id로 다른 내용의 신고가 접수되었습니다.")

    walk, request, occurred_utc = locked_walk_for_incident(db, walk_id, actor, payload.occurred_at)

    if payload.latitude is None and payload.longitude is None:
        lat, lng = walk.last_latitude, walk.last_longitude
    else:
        lat, lng = payload.latitude, payload.longitude

    if payload.client_report_id:
        existing = db.query(WalkIncident).filter_by(
            reported_by=actor.id, client_report_id=payload.client_report_id
        ).first()
        if existing:
            if is_same_incident(existing, walk.id, payload.incident_type, desc, lat, lng, occurred_utc):
                return existing, False
            raise HTTPException(409, "이미 동일한 client_report_id로 다른 내용의 신고가 접수되었습니다.")

    dup_window = get_incident_duplicate_window_seconds()
    existing_type_incidents = db.query(WalkIncident).filter(
        WalkIncident.walk_id == walk.id,
        WalkIncident.incident_type == payload.incident_type,
    ).all()
    for inc in existing_type_incidents:
        diff_sec = abs((as_utc(inc.occurred_at) - occurred_utc).total_seconds())
        if diff_sec <= dup_window:
            if not payload.confirm_duplicate:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "DUPLICATE_RECENT",
                        "message": f"최근 {dup_window}초 이내에 동일한 유형의 신고가 접수되었습니다.",
                        "duplicate_incident_id": inc.id,
                    },
                )

    incident = WalkIncident(
        walk_id=walk.id,
        reported_by=actor.id,
        incident_type=payload.incident_type,
        description=desc,
        latitude=lat,
        longitude=lng,
        occurred_at=occurred_utc,
        status="OPEN",
        client_report_id=payload.client_report_id,
    )
    db.add(incident)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if payload.client_report_id:
            existing = db.query(WalkIncident).filter_by(
                reported_by=actor.id, client_report_id=payload.client_report_id
            ).first()
            if existing and is_same_incident(existing, walk.id, payload.incident_type, desc, lat, lng, occurred_utc):
                return existing, False
            raise HTTPException(409, "이미 동일한 client_report_id로 다른 내용의 신고가 접수되었습니다.")
        raise

    return incident, True


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


def finalize_walk_statistics(db, walk, end_time, status, end_reason, user_id):
    points = db.query(WalkPoint).filter_by(walk_id=walk.id).order_by(WalkPoint.sequence_no).yield_per(1000)
    legacy_distance = total_distance(points)
    motion_points = (db.query(WalkPoint).filter_by(walk_id=walk.id)
                     .order_by(WalkPoint.recorded_at, WalkPoint.sequence_no).yield_per(1000))
    motion = analyze_motion(motion_points, walk.started_at, end_time, distance_fn=distance_m, already_ordered=True)
    walk.total_distance_m = legacy_distance
    walk.duration_seconds = motion.duration_seconds
    walk.moving_duration_seconds = motion.moving_duration_seconds
    walk.stopped_duration_seconds = motion.stopped_duration_seconds
    walk.unknown_duration_seconds = motion.unknown_duration_seconds
    walk.moving_distance_m = motion.moving_distance_m
    walk.average_pace = motion.average_pace
    walk.status = status
    walk.ended_at = end_time
    walk.end_reason = end_reason
    db.flush()
    if walk.status == "COMPLETED":
        # Issue #6: same transaction, so a failed refresh also rolls the walk end back.
        refresh_volunteer_profile(db, user_id)
    return walk


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
    end_reason = "USER" if actor.id == request.user_id else "STAFF"
    return finalize_walk_statistics(db, walk, now, payload.status, end_reason, request.user_id)


def close_stale_walks(db, now=None, user_id=None):
    """Closes all expired IN_PROGRESS walks idempotently."""
    if now is None:
        now = utc_now()
    timeout_seconds = get_auto_end_after_seconds()
    query = db.query(Walk.id).join(WalkRequest).filter(Walk.status == "IN_PROGRESS")
    if user_id is not None:
        query = query.filter(WalkRequest.user_id == user_id)
    candidate_ids = [r[0] for r in query.all()]
    closed = []
    for walk_id in candidate_ids:
        walk = lock_row(db, Walk, walk_id)
        if walk is None or walk.status != "IN_PROGRESS":
            continue
        last_active = as_utc(walk.last_point_received_at or walk.started_at)
        if (now - last_active).total_seconds() > timeout_seconds:
            request = lock_row(db, WalkRequest, walk.walk_request_id)
            end_time = last_active
            finalize_walk_statistics(db, walk, end_time, "COMPLETED", "AUTO_TIMEOUT", request.user_id)
            closed.append(walk)
    db.flush()
    return closed


def get_active_walk(db, actor):
    now = utc_now()
    close_stale_walks(db, now=now, user_id=actor.id)
    walk = (
        db.query(Walk)
        .join(WalkRequest)
        .filter(WalkRequest.user_id == actor.id, Walk.status == "IN_PROGRESS")
        .first()
    )
    if walk is None:
        raise HTTPException(404, "진행 중인 산책이 없습니다.")
    last_seq = db.query(func.max(WalkPoint.sequence_no)).filter_by(walk_id=walk.id).scalar()
    elapsed_seconds = int((now - as_utc(walk.started_at)).total_seconds())
    return {
        "walk_id": walk.id,
        "started_at": walk.started_at,
        "last_latitude": walk.last_latitude,
        "last_longitude": walk.last_longitude,
        "last_accuracy_m": walk.last_accuracy_m,
        "last_point_recorded_at": walk.last_point_recorded_at,
        "last_sequence_no": last_seq,
        "elapsed_seconds": elapsed_seconds,
    }


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
