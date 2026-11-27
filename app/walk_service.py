"""Walk recording with the Issue #3 lock order, then the concrete Walk row.

Provisional GPS policy: known accuracy <=50m, segment speed <=5m/s, no distance
bridge over sampling gaps >300s. Rejected batches never partially persist.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.dependencies import require_shelter_access
from app.models import Walk, WalkIncident, WalkMarker, WalkPoint, WalkRequest
from app.walk_models import DogWalkSetting, WalkHandover
from app.volunteer_stats import refresh_volunteer_profile
from app.gps_geometry import EARTH_RADIUS_M, distance_m
from app.gps_motion_analysis import analyze_motion
from app.gps_motion_config import MAX_SPEED_M_S
from app.shelter_code_service import usable_verification
from app.walk_request_service import (
    as_utc, locked_profile, locked_request, lock_row, start_conflict, validate_linked_eligibility, validate_walk_start,
)

MAX_ACCURACY_M = Decimal("50")
MAX_DISTANCE_GAP_SECONDS = 300


def utc_now():
    return datetime.now(timezone.utc)


def get_auto_end_after_seconds():
    return int(os.getenv("AUTO_END_AFTER_SECONDS", "3600"))


def last_activity_at(walk):
    """Server receipt time of the last new point, or the start time when none arrived."""
    return as_utc(walk.last_point_received_at or walk.started_at)


def auto_end_at(walk):
    """Issue #89: the single no-signal deadline. A walk expires strictly after this time."""
    return last_activity_at(walk) + timedelta(seconds=get_auto_end_after_seconds())


def is_walk_expired(walk, now):
    return now > auto_end_at(walk)


def get_walk_overdue_grace_minutes() -> int:
    val = os.getenv("WALK_OVERDUE_GRACE_MINUTES", "0")
    try:
        minutes = int(val)
    except ValueError:
        return 0
    return minutes if minutes >= 0 else 0


def compute_walk_overdue(walk, now=None) -> tuple[bool, int]:
    """진행 중인 산책이 예정 종료 시각(+ 유예 시간)을 초과했는지 계산한다."""
    if walk.status != "IN_PROGRESS" or walk.expected_end_at is None:
        return False, 0
    current = as_utc(now) if now is not None else utc_now()
    deadline = as_utc(walk.expected_end_at) + timedelta(minutes=get_walk_overdue_grace_minutes())
    if current <= deadline:
        return False, 0
    overdue_seconds = (current - deadline).total_seconds()
    return True, max(0, int(overdue_seconds // 60))


def resolve_planned_duration_minutes(db, request, date_slot=None) -> int:
    """A dated walk uses the scheduled duration and any explicit maximum."""
    setting = db.query(DogWalkSetting).filter(DogWalkSetting.dog_id == request.dog_id).first()
    if date_slot is not None:
        minutes = date_slot.duration_minutes
        if setting is not None and setting.max_walk_minutes is not None:
            minutes = min(minutes, setting.max_walk_minutes)
        return minutes
    if setting is not None and setting.recommended_duration_minutes is not None:
        return setting.recommended_duration_minutes
    fallback = int((as_utc(request.requested_end_at) - as_utc(request.requested_start_at)).total_seconds() // 60)
    return max(1, fallback)


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
        raise start_conflict("WALK_START_BOOKING_NOT_ELIGIBLE", "승인된 예약 시간 안에서만 산책을 시작할 수 있습니다.")
    locked_profile(db, request.user_id)
    try:
        date_slot = validate_linked_eligibility(db, request, request.dog, request.user, require_future=False)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        raise start_conflict("WALK_START_BOOKING_NOT_ELIGIBLE", exc.detail) from None
    active = db.query(Walk.id).join(WalkRequest).filter(
        Walk.status == "IN_PROGRESS",
        or_(WalkRequest.dog_id == request.dog_id, WalkRequest.user_id == request.user_id),
    ).first()
    if active:
        raise start_conflict("WALK_START_ALREADY_STARTED", "강아지 또는 봉사자가 이미 산책 중입니다.")
    # Issue #87: a current, verified, unused shelter code is required; it is used in this transaction.
    verification = usable_verification(db, request, actor.id, now)
    if verification is None:
        raise start_conflict("WALK_START_CODE_REQUIRED", "보호소 코드를 다시 확인해 주세요.")
    planned_duration_minutes = resolve_planned_duration_minutes(db, request, date_slot)
    expected_end_at = now + timedelta(minutes=planned_duration_minutes)
    walk = Walk(
        walk_request_id=request.id,
        status="IN_PROGRESS",
        started_at=now,
        planned_duration_minutes=planned_duration_minutes,
        expected_end_at=expected_end_at,
        handover_required=True,
    )
    db.add(walk)
    db.flush()
    verification.used_at = now  # Rolled back together with the Walk if the start fails to commit.
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
            # Issue #89: an implausible jump (> MAX_SPEED_M_S) is kept as a raw observation.
            # total_distance() and analyze_motion() already leave such segments out, so one
            # bad GPS fix no longer blocks every later batch.
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


def get_marker_max_accuracy_m():
    try:
        value = Decimal(os.getenv("WALK_MARKER_MAX_ACCURACY_M", "50"))
    except ArithmeticError:
        return Decimal("50")
    return value if value.is_finite() and value >= 0 else Decimal("50")


def get_marker_max_per_walk():
    try:
        value = int(os.getenv("WALK_MARKER_MAX_PER_WALK", "500"))
    except ValueError:
        return 500
    return value if value > 0 else 500


# Issue #89 location windows (Notion initial values, approved): a body position counts as
# CURRENT when observed at most 30 s before the tap; otherwise the walk's own stored point
# from the last 2 minutes is LAST_KNOWN. Incidents accept a stored point up to 30 s old.
MARKER_CURRENT_MAX_AGE = timedelta(seconds=30)
MARKER_LAST_KNOWN_MAX_AGE = timedelta(minutes=2)
INCIDENT_POSITION_MAX_AGE = timedelta(seconds=30)


def recent_point(db, walk_id, at, max_age, max_accuracy):
    """The latest stored observation of this walk in [at - max_age, at] with a known, good accuracy."""
    return (db.query(WalkPoint)
            .filter(WalkPoint.walk_id == walk_id, WalkPoint.recorded_at <= at, WalkPoint.recorded_at >= at - max_age,
                    WalkPoint.accuracy_m.isnot(None), WalkPoint.accuracy_m <= max_accuracy)
            .order_by(WalkPoint.recorded_at.desc(), WalkPoint.sequence_no.desc()).first())


def resolve_marker_location(db, walk, payload, recorded_at):
    """Return (latitude, longitude, location_source, position_recorded_at); never a guessed position."""
    max_accuracy = get_marker_max_accuracy_m()
    if payload.latitude is not None:
        observed = None if payload.position_recorded_at is None else as_utc(payload.position_recorded_at)
        if observed is None:  # earlier clients: keep the existing accuracy-only rule
            usable = payload.accuracy_m is None or payload.accuracy_m <= max_accuracy
        else:
            usable = (payload.accuracy_m is not None and payload.accuracy_m <= max_accuracy
                      and timedelta(0) <= recorded_at - observed <= MARKER_CURRENT_MAX_AGE)
        if usable:
            return payload.latitude, payload.longitude, "CURRENT", observed
    point = recent_point(db, walk.id, recorded_at, MARKER_LAST_KNOWN_MAX_AGE, max_accuracy)
    if point is None:
        raise HTTPException(409, {"code": "MARKER_LOCATION_UNAVAILABLE",
                                  "message": "최근 위치를 확인할 수 없어 마커를 저장하지 않았습니다."})
    return point.latitude, point.longitude, "LAST_KNOWN", as_utc(point.recorded_at)


def decimal_text(value):
    return None if value is None else format(Decimal(value).normalize(), "f")


def marker_fingerprint(payload):
    """SHA-256 of the normalized request body, fixed by the first request with this client_marker_id."""
    body = {
        "marker_type": payload.marker_type, "memo": payload.memo,
        "recorded_at": as_utc(payload.recorded_at).isoformat(),
        "latitude": decimal_text(payload.latitude), "longitude": decimal_text(payload.longitude),
        "accuracy_m": decimal_text(payload.accuracy_m),
        "position_recorded_at": None if payload.position_recorded_at is None
        else as_utc(payload.position_recorded_at).isoformat(),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_walk_owner(db, walk_id, actor):
    """Authentication and ownership before any replay lookup; the same errors as locked_walk()."""
    hint = db.query(WalkRequest.user_id).join(Walk, Walk.walk_request_id == WalkRequest.id).filter(
        Walk.id == walk_id).first()
    if hint is None:
        raise HTTPException(404, "Walk not found")
    if actor.id != hint.user_id:
        raise HTTPException(403, "본인 신청만 처리할 수 있습니다.")


def marker_replay(db, walk_id, key, fingerprint):
    existing = (db.query(WalkMarker).filter(WalkMarker.walk_id == walk_id, WalkMarker.client_marker_id == key)
                .populate_existing().first())
    if existing is not None and existing.request_fingerprint != fingerprint:
        raise HTTPException(409, "같은 client_marker_id로 다른 내용의 마커가 이미 저장되었습니다.")
    return existing


def add_marker(db, walk_id, actor, payload):
    """Return (marker, created). A stored key with the same body is answered before state and
    limit checks, so a retry after the walk ended or reached the limit still gets 200."""
    key, fingerprint = payload.client_marker_id, None
    if key is not None:
        require_walk_owner(db, walk_id, actor)
        fingerprint = marker_fingerprint(payload)
        existing = marker_replay(db, walk_id, key, fingerprint)
        if existing is not None:
            return existing, False
    walk, _ = locked_walk(db, walk_id, actor)
    now = utc_now()
    if is_walk_expired(walk, now):
        raise HTTPException(409, "산책 세션이 만료되었습니다.")
    recorded_at = validate_event_time(walk, payload.recorded_at, now)
    if key is not None:  # a concurrent request may have stored it before we took the lock
        existing = marker_replay(db, walk.id, key, fingerprint)
        if existing is not None:
            return existing, False
    count = db.query(func.count(WalkMarker.id)).filter(WalkMarker.walk_id == walk.id).scalar()
    if count >= get_marker_max_per_walk():
        raise HTTPException(409, "산책당 저장할 수 있는 마커 수를 초과했습니다.")
    latitude, longitude, location_source, position_recorded_at = resolve_marker_location(db, walk, payload, recorded_at)
    marker = WalkMarker(
        walk_id=walk.id, marker_type=payload.marker_type, memo=payload.memo,
        latitude=latitude, longitude=longitude, recorded_at=recorded_at, location_source=location_source,
        client_marker_id=key, request_fingerprint=fingerprint, position_recorded_at=position_recorded_at)
    db.add(marker)
    db.flush()
    return marker, True


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


def flush_new_row(db, row):
    """Issue #89: on PostgreSQL a unique race rolls back only a SAVEPOINT, so this request keeps its
    locks and earlier work. pysqlite cannot nest a SAVEPOINT in SQLAlchemy's deferred transaction
    (its RELEASE may commit), so the SQLite test engine keeps the previous full rollback."""
    if db.get_bind().dialect.name == "postgresql":
        with db.begin_nested():
            db.add(row)
            db.flush()
        return
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise


def same_report(existing, walk_id, payload, description, occurred_utc):
    """A retry with omitted coordinates matches the stored coordinates, whatever they are now (#89)."""
    lat = payload.latitude if payload.latitude is not None else existing.latitude
    lng = payload.longitude if payload.longitude is not None else existing.longitude
    return is_same_incident(existing, walk_id, payload.incident_type, description, lat, lng, occurred_utc)


def stored_report(db, actor, payload):
    return db.query(WalkIncident).filter_by(
        reported_by=actor.id, client_report_id=payload.client_report_id
    ).populate_existing().first()


def report_incident(db, walk_id, actor, payload):
    occurred_utc = as_utc(payload.occurred_at)
    # OTHER always carries the user's text (schema, #89); other types keep the default label.
    desc = payload.description or INCIDENT_TYPE_LABELS.get(payload.incident_type, payload.incident_type)
    conflict = "이미 동일한 client_report_id로 다른 내용의 신고가 접수되었습니다."

    if payload.client_report_id:
        existing = stored_report(db, actor, payload)
        if existing:
            if same_report(existing, walk_id, payload, desc, occurred_utc):
                return existing, False
            raise HTTPException(409, conflict)

    walk, request, occurred_utc = locked_walk_for_incident(db, walk_id, actor, payload.occurred_at)

    if payload.latitude is None and payload.longitude is None:
        # Issue #89: only a stored observation of this walk from the 30 s before the incident,
        # never the walk's last position of any age. No usable fix: the report is kept without one.
        point = recent_point(db, walk.id, occurred_utc, INCIDENT_POSITION_MAX_AGE, get_marker_max_accuracy_m())
        lat, lng = (point.latitude, point.longitude) if point is not None else (None, None)
    else:
        lat, lng = payload.latitude, payload.longitude

    if payload.client_report_id:
        existing = stored_report(db, actor, payload)
        if existing:
            if same_report(existing, walk.id, payload, desc, occurred_utc):
                return existing, False
            raise HTTPException(409, conflict)

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
    try:
        flush_new_row(db, incident)
    except IntegrityError:
        if payload.client_report_id:
            existing = stored_report(db, actor, payload)
            if existing and same_report(existing, walk.id, payload, desc, occurred_utc):
                return existing, False
            raise HTTPException(409, conflict) from None
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
    if is_walk_expired(walk, now):
        # Issue #89: a walk that already passed its no-signal deadline keeps that result. It is
        # settled as COMPLETED/AUTO_TIMEOUT at the last activity time, never as USER or STAFF.
        return finalize_walk_statistics(db, walk, last_activity_at(walk), "COMPLETED", "AUTO_TIMEOUT",
                                        request.user_id)
    if now < as_utc(walk.started_at):
        raise HTTPException(409, "서버 종료 시각이 산책 시작 시각보다 빠릅니다.")
    for model, timestamp in ((WalkPoint, WalkPoint.recorded_at), (WalkMarker, WalkMarker.recorded_at),
                             (WalkIncident, WalkIncident.occurred_at)):
        latest = db.query(func.max(timestamp)).filter(model.walk_id == walk.id).scalar()
        if latest and as_utc(latest) > now:
            raise HTTPException(409, "마지막 관찰 시각보다 빠르게 종료할 수 없습니다.")
    end_reason = "USER" if actor.id == request.user_id else "STAFF"
    return finalize_walk_statistics(db, walk, now, payload.status, end_reason, request.user_id)


def lock_handover_walk(db, walk_id, actor):
    require_walk_owner(db, walk_id, actor)
    # Wait for an in-flight end or first submission; lock only the Walk row.
    walk = db.query(Walk).filter_by(id=walk_id).populate_existing().with_for_update().first()
    if walk is None:
        raise HTTPException(404, "Walk not found")
    if walk.walk_request.user_id != actor.id:
        raise HTTPException(403, "본인 신청만 처리할 수 있습니다.")
    return walk


def save_handover(db, walk_id, actor, payload):
    walk = lock_handover_walk(db, walk_id, actor)
    completed = walk.status == "COMPLETED" and walk.end_reason in ("USER", "AUTO_TIMEOUT")
    if not walk.handover_required or not (completed or walk.status == "INTERRUPTED"):
        raise HTTPException(409, "인계를 접수할 수 없는 산책입니다.")

    complete = payload.handover_result == "COMPLETE"
    if complete and not completed:
        raise HTTPException(409, "중단된 산책은 인계 문제만 접수할 수 있습니다.")
    if complete and walk.end_reason == "AUTO_TIMEOUT" and not payload.acknowledge_auto_timeout:
        raise HTTPException(422, "정상 인계를 접수하려면 자동 종료 사실을 확인해야 합니다.")

    fields = payload.model_dump()
    handovers = db.query(WalkHandover).filter_by(walk_id=walk.id)
    existing = handovers.one_or_none()
    if existing is None:
        now = utc_now()
        handover = WalkHandover(
            walk_id=walk.id, volunteer_user_id=actor.id, **fields,
            self_confirmed_at=now if complete else None,
            issue_reported_at=None if complete else now,
            level_evaluation_pending_at=now if complete and walk.end_reason == "USER" else None,
        )
        try:
            flush_new_row(db, handover)
        except IntegrityError as exc:
            if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "uq_walk_handovers_walk":
                raise
            existing = handovers.one_or_none()
            if existing is None:
                raise
        else:
            return handover, True

    if any(getattr(existing, name) != value for name, value in fields.items()):
        raise HTTPException(409, "이 산책에 다른 인계 내용이 이미 접수되었습니다.")
    return existing, False


def close_stale_walks(db, now=None, user_id=None):
    """Closes all expired IN_PROGRESS walks idempotently."""
    if now is None:
        now = utc_now()
    query = db.query(Walk.id, Walk.started_at, Walk.last_point_received_at).join(WalkRequest).filter(
        Walk.status == "IN_PROGRESS")
    if user_id is not None:
        query = query.filter(WalkRequest.user_id == user_id)
    # Issue #89: lock only walks that already look expired, so settling never contends with
    # live walks; the same deadline (auto_end_at) is checked again under the row lock.
    candidate_ids = [row.id for row in query.all() if is_walk_expired(row, now)]
    closed = []
    for walk_id in candidate_ids:
        walk = lock_row(db, Walk, walk_id)
        if walk is None or walk.status != "IN_PROGRESS" or not is_walk_expired(walk, now):
            continue
        request = lock_row(db, WalkRequest, walk.walk_request_id)
        finalize_walk_statistics(db, walk, last_activity_at(walk), "COMPLETED", "AUTO_TIMEOUT", request.user_id)
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
    is_overdue, overdue_minutes = compute_walk_overdue(walk, now=now)
    return {
        "walk_id": walk.id,
        "walk_request_id": walk.walk_request_id,
        "started_at": walk.started_at,
        "planned_duration_minutes": walk.planned_duration_minutes,
        "expected_end_at": walk.expected_end_at,
        "is_overdue": is_overdue,
        "overdue_minutes": overdue_minutes,
        "last_latitude": walk.last_latitude,
        "last_longitude": walk.last_longitude,
        "last_accuracy_m": walk.last_accuracy_m,
        "last_point_recorded_at": walk.last_point_recorded_at,
        "last_point_received_at": walk.last_point_received_at,
        "auto_end_at": auto_end_at(walk),
        "last_sequence_no": last_seq,
        "elapsed_seconds": elapsed_seconds,
    }


def walk_detail(db, walk_id, actor, limit, offset):
    walk = db.query(Walk).options(joinedload(Walk.walk_request), joinedload(Walk.handover)).filter_by(id=walk_id).first()
    if walk is None:
        raise HTTPException(404, "Walk not found")
    request = walk.walk_request
    if actor.id != request.user_id:
        require_shelter_access(actor, request.shelter_id)
    result = {"walk": walk, "user_id": request.user_id, "dog_id": request.dog_id,
              "shelter_id": request.shelter_id, "limit": limit, "offset": offset,
              "handover_status": walk.handover_status, "handover": walk.handover}
    for name, model, ordering in (
        ("points", WalkPoint, (WalkPoint.sequence_no,)),
        ("markers", WalkMarker, (WalkMarker.recorded_at, WalkMarker.id)),
        ("incidents", WalkIncident, (WalkIncident.occurred_at, WalkIncident.id)),
    ):
        query = db.query(model).filter_by(walk_id=walk.id)
        result[{"points": "point_count", "markers": "marker_count", "incidents": "incident_count"}[name]] = query.count()
        result[name] = query.order_by(*ordering).limit(limit).offset(offset).all()
    return result
