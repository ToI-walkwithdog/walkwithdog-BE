"""Walk request policy; callers commit/rollback once and keep locks until commit.

Writer lock order: Dog -> applicant User -> existing WalkRequest -> VolunteerProfile.
NOWAIT avoids unbounded waits; all locks are database row locks, never process locks.
"""
from datetime import datetime, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.api_contract import ContractError
from app.dependencies import require_shelter_access
from app.models import Dog, DogWalkSetting, User, UserRole, VolunteerOnboardingExperience, VolunteerProfile, Walk, WalkRequest
from app.onboarding_common import require_walk_onboarding
from app.walk_date_slots import (ACTIVE, check_booking_slot, dog_not_available, occupied, utc_string)
from app.walk_request_schemas import ManagedWalkRequestResponse

SEOUL = ZoneInfo("Asia/Seoul")

WALK_FREQUENCY_LABELS = {
    "DAILY": "거의 매일",
    "WEEKLY_3_4": "주 3~4회",
    "WEEKLY_1_2": "주 1~2회",
    "RARELY": "거의 없음",
}


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    if value.tzinfo is None:  # UTC persisted by this service; SQLite test compatibility.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lock_row(db, model, identifier):
    return (db.query(model).filter(model.id == identifier).populate_existing()
            .with_for_update(nowait=True).first())


def lock_resources(db, dog_id, user_id):
    dog = lock_row(db, Dog, dog_id)
    if dog is None:
        raise HTTPException(404, "Dog not found")
    volunteer = lock_row(db, User, user_id)
    if volunteer is None:
        raise HTTPException(404, "User not found")
    return dog, volunteer


def locked_profile(db, user_id, create=False):
    profile = (db.query(VolunteerProfile).filter_by(user_id=user_id).populate_existing()
               .with_for_update(nowait=True).first())
    if profile is None and create:
        profile = VolunteerProfile(user_id=user_id)
        db.add(profile)
        db.flush()
    return profile


def walkable_setting(db, dog, volunteer, shelter_id=None):
    if volunteer.role != UserRole.USER:
        raise HTTPException(409, "현재 신청자가 봉사자 계정이 아닙니다.")
    if dog.shelter_id is None:
        raise HTTPException(409, "보호소가 지정되지 않은 강아지입니다.")
    if shelter_id is not None and dog.shelter_id != shelter_id:
        raise HTTPException(409, "신청 이후 강아지 소속 보호소가 변경되었습니다. 운영 확인이 필요합니다.")
    setting = db.query(DogWalkSetting).filter_by(dog_id=dog.id).populate_existing().first()
    if setting is None or not setting.walkable:
        raise HTTPException(409, "현재 산책 가능한 강아지가 아닙니다.")
    return setting


def validate_eligibility(db, dog, volunteer, start, end, shelter_id=None, *, require_future=True):
    setting = walkable_setting(db, dog, volunteer, shelter_id)
    start, end = as_utc(start), as_utc(end)
    if require_future and start <= utc_now():
        raise HTTPException(409, "산책 시작 시각은 현재보다 미래여야 합니다.")
    local_start, local_end = start.astimezone(SEOUL), end.astimezone(SEOUL)
    if local_start.date() != local_end.date():
        raise HTTPException(409, "한국 현지 날짜가 넘어가는 산책은 신청할 수 없습니다.")
    # Query slots afresh under the parent Dog lock, shared with Issue #2 writers.
    db.expire(setting, ["slots"])
    if not any(slot.day_of_week == local_start.weekday()
               and slot.start_time <= local_start.time()
               and local_end.time() <= slot.end_time for slot in setting.slots):
        raise HTTPException(409, "신청 시간이 하나의 산책 운영 시간대에 포함되어야 합니다.")


def check_active_overlap(db, user_id, dog_id, start, end):
    existing = db.query(WalkRequest.id).filter(
        WalkRequest.user_id == user_id, WalkRequest.dog_id == dog_id,
        WalkRequest.status.in_(ACTIVE), WalkRequest.requested_start_at < end,
        WalkRequest.requested_end_at > start,
    ).first()
    if existing:
        raise HTTPException(409, "같은 강아지에 대한 활성 신청 시간이 중복되거나 겹칩니다.")


def check_approved_conflicts(db, user_id, dog_id, start, end, exclude_id=None):
    query = db.query(WalkRequest.dog_id, WalkRequest.user_id).filter(
        WalkRequest.status == "APPROVED", WalkRequest.requested_start_at < end,
        WalkRequest.requested_end_at > start,
        or_(WalkRequest.dog_id == dog_id, WalkRequest.user_id == user_id),
    )
    if exclude_id is not None:
        query = query.filter(WalkRequest.id != exclude_id)
    conflict = query.first()
    if conflict:
        subject = "강아지" if conflict.dog_id == dog_id else "봉사자"
        raise HTTPException(409, f"{subject}의 승인된 산책 예약과 시간이 겹칩니다.")


def create_request(db, actor, payload):
    dog, volunteer = lock_resources(db, payload.dog_id, actor.id)
    if volunteer.role != UserRole.USER:
        raise HTTPException(403, "일반 사용자만 산책을 신청할 수 있습니다.")
    locked_profile(db, volunteer.id, create=True)
    start, end = as_utc(payload.requested_start_at), as_utc(payload.requested_end_at)
    validate_eligibility(db, dog, volunteer, start, end)
    if occupied(db, dog.id, start, end, date_only=True):
        raise HTTPException(409, "날짜별 산책 예약과 시간이 겹칩니다.")
    check_active_overlap(db, volunteer.id, dog.id, start, end)
    check_approved_conflicts(db, volunteer.id, dog.id, start, end)
    request = WalkRequest(user_id=volunteer.id, dog=dog, shelter_id=dog.shelter_id,
                          requested_start_at=start, requested_end_at=end, status="PENDING")
    db.add(request)
    db.flush()
    return request


def date_request_fingerprint(payload):
    values = [payload.dog_id, payload.date_slot_id, utc_string(payload.requested_start_at),
              utc_string(payload.requested_end_at)]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("ascii")).hexdigest()


def owns_date_request(db, request_id, actor):
    """A routing hint for locks, never a substitute for the locked permission checks."""
    return (db.query(WalkRequest.id).filter(WalkRequest.id == request_id, WalkRequest.user_id == actor.id,
                                           WalkRequest.date_slot_id.is_not(None)).first() is not None)


def date_request_replay(db, user_id, key, fingerprint):
    entry = db.query(WalkRequest).filter_by(user_id=user_id, idempotency_key=key).first()
    if entry is None:
        return None
    if entry.request_fingerprint != fingerprint:
        raise ContractError(409, "IDEMPOTENCY_KEY_REUSED", "같은 요청 키로 다른 산책을 신청할 수 없습니다.")
    return entry.creation_receipt


def create_date_request(db, actor, payload, key, fingerprint):
    """The authenticated applicant row is already exclusively locked to serialize its keys."""
    replay = date_request_replay(db, actor.id, key, fingerprint)
    if replay is not None:
        return replay
    require_walk_onboarding(db, actor)
    dog = lock_row(db, Dog, payload.dog_id)
    if dog is None or dog.shelter_id is None:
        raise dog_not_available()
    try:
        walkable_setting(db, dog, actor)
    except HTTPException:
        raise dog_not_available() from None
    start, end = as_utc(payload.requested_start_at), as_utc(payload.requested_end_at)
    slot = check_booking_slot(db, dog.id, payload.date_slot_id, start, end, utc_now())
    try:
        check_approved_conflicts(db, actor.id, dog.id, start, end)
    except HTTPException as exc:
        raise ContractError(409, "WALK_REQUEST_CONFLICT", exc.detail) from None
    locked_profile(db, actor.id, create=True)
    request = WalkRequest(user_id=actor.id, dog=dog, shelter_id=dog.shelter_id, date_slot_id=slot.id,
                          requested_start_at=start, requested_end_at=end, status="PENDING")
    db.add(request)
    db.flush()
    receipt = {"id": request.id, "dog_id": dog.id, "date_slot_id": slot.id, "shelter_id": dog.shelter_id,
               "requested_start_at": utc_string(start), "requested_end_at": utc_string(end),
               "status": "PENDING", "created_at": utc_string(request.created_at)}
    request.idempotency_key, request.request_fingerprint, request.creation_receipt = key, fingerprint, receipt
    db.flush()
    return receipt


def validate_linked_eligibility(db, request, dog, volunteer, *, require_future=True):
    start, end = request.requested_start_at, request.requested_end_at
    if request.date_slot_id is None:
        validate_eligibility(db, dog, volunteer, start, end, request.shelter_id, require_future=require_future)
        if occupied(db, dog.id, start, end, date_only=True):
            raise HTTPException(409, "날짜별 산책 예약과 시간이 겹칩니다.")
        return
    walkable_setting(db, dog, volunteer, request.shelter_id)
    try:
        return check_booking_slot(db, dog.id, request.date_slot_id, start, end, utc_now(),
                                  exclude_id=request.id, require_future=require_future)
    except ContractError as exc:
        raise HTTPException(409, exc.message) from None


def authorize_action(actor, owner_id, shelter_id, action):
    if action == "START" or (action == "CANCEL" and actor.role == UserRole.USER):
        if actor.id != owner_id:
            raise HTTPException(403, "본인 신청만 처리할 수 있습니다.")
    else:
        require_shelter_access(actor, shelter_id)


def locked_request(db, request_id, actor, action):
    # Read immutable routing keys first, then acquire locks in the global order.
    hint = db.query(WalkRequest.dog_id, WalkRequest.user_id, WalkRequest.shelter_id).filter_by(id=request_id).first()
    if hint is None:
        raise HTTPException(404, "Walk request not found")
    authorize_action(actor, hint.user_id, hint.shelter_id, action)
    dog, volunteer = lock_resources(db, hint.dog_id, hint.user_id)
    request = lock_row(db, WalkRequest, request_id)
    if request is None:
        raise HTTPException(404, "Walk request not found")
    if (request.dog_id, request.user_id, request.shelter_id) != tuple(hint):
        raise HTTPException(409, "신청 소유 정보가 변경되었습니다. 다시 조회하세요.")
    authorize_action(actor, request.user_id, request.shelter_id, action)
    return request, dog, volunteer


def has_walk_record(db, request):
    return db.query(Walk.id).filter(Walk.walk_request_id == request.id).first() is not None


def ensure_not_started(db, request):
    if has_walk_record(db, request):
        raise HTTPException(409, "이미 산책이 시작되어 취소할 수 없습니다.")


def cancellation_conflict(request, now, *, has_walk):
    if request.status == "CANCELLED":
        return {"code": "REQUEST_ALREADY_CANCELLED", "message": "이미 취소된 예약입니다."}
    if request.status not in ACTIVE:
        return {"code": "REQUEST_STATE_CONFLICT", "message": "현재 상태에서는 예약을 취소할 수 없습니다."}
    if as_utc(request.requested_start_at) <= now:
        return {"code": "CANCEL_TIME_EXPIRED", "message": "예약 시작 시각에 도달해 취소할 수 없습니다."}
    if has_walk:
        return {"code": "WALK_RECORD_EXISTS", "message": "이 예약에 산책 기록이 있어 취소할 수 없습니다."}
    return None


def can_cancel_request(request, actor, now):
    try:
        authorize_action(actor, request.user_id, request.shelter_id, "CANCEL")
    except HTTPException:
        return False
    return cancellation_conflict(request, now, has_walk=request.walk is not None) is None


def transition_request(db, request_id, actor, payload):
    request, dog, volunteer = locked_request(db, request_id, actor, payload.action)
    if payload.action == "CANCEL":
        now = utc_now()
        conflict = cancellation_conflict(request, now, has_walk=has_walk_record(db, request))
        if conflict is not None:
            raise HTTPException(409, conflict)
        request.status = "CANCELLED"  # Preserve any previous review metadata.
        request.cancel_reason_code = payload.cancel_reason_code
        request.cancelled_at = now
        request.cancelled_by = actor.id
        request.cancelled_actor_role = actor.role.name
    else:
        if request.status != "PENDING":
            raise HTTPException(409, "승인 대기 중인 신청만 승인하거나 거절할 수 있습니다.")
        if payload.action == "APPROVE":
            locked_profile(db, volunteer.id)
            validate_linked_eligibility(db, request, dog, volunteer)
            ensure_not_started(db, request)
            check_approved_conflicts(db, volunteer.id, dog.id, request.requested_start_at,
                                     request.requested_end_at, exclude_id=request.id)
            request.status = "APPROVED"
            request.rejection_reason = None
            request.rejection_reason_code = None
        else:
            request.status = "REJECTED"
            request.rejection_reason = payload.rejection_reason
            request.rejection_reason_code = (
                payload.rejection_reason_code.value
                if payload.rejection_reason_code
                else None
            )
        request.reviewed_by, request.reviewed_at = actor.id, utc_now()
    db.flush()
    return request


def get_request_detail(db, request_id, actor):
    request = (
        db.query(WalkRequest)
        .options(
            joinedload(WalkRequest.dog),
            joinedload(WalkRequest.shelter),
            joinedload(WalkRequest.walk),
        )
        .filter(WalkRequest.id == request_id)
        .first()
    )
    if request is None:
        raise HTTPException(404, "Walk request not found")

    if actor.role == UserRole.USER:
        if request.user_id != actor.id:
            raise HTTPException(403, "본인의 신청만 조회할 수 있습니다.")
    else:
        require_shelter_access(actor, request.shelter_id)

    applicant_user = db.get(User, request.user_id)
    experience = db.get(VolunteerOnboardingExperience, request.user_id)
    has_experience = experience.has_experience if experience is not None else None
    setting = db.query(DogWalkSetting).filter_by(dog_id=request.dog_id).first()
    dog_difficulty = setting.difficulty if setting and setting.difficulty else "BEGINNER"
    now = utc_now()
    can_cancel = can_cancel_request(request, actor, now)
    owner = actor.role == UserRole.USER and actor.id == request.user_id
    show_visit = owner and request.status == "APPROVED" and now < as_utc(request.requested_end_at)

    return {
        **ManagedWalkRequestResponse.model_validate(request, context={"can_cancel": can_cancel}).model_dump(),
        "shelter": request.shelter,
        "max_walk_minutes": setting.max_walk_minutes if setting is not None else None,
        "confirmed_visit": request.shelter if show_visit else None,
        "actions": {
            "can_cancel": owner and can_cancel,
            "can_enter_shelter_code": show_visit and request.walk is None,
        },
        "applicant": {
            "id": applicant_user.id if applicant_user else request.user_id,
            "nickname": applicant_user.nickname if applicant_user else "",
            "name": None,
            "age": None,
            "dog_experience": has_experience,
            "has_experience": has_experience,
            "experienced_breed": experience.experienced_breeds_text if has_experience else None,
            "walk_frequency": WALK_FREQUENCY_LABELS.get(experience.walking_frequency) if has_experience else None,
        },
        "dog_difficulty": dog_difficulty,
    }


def validate_walk_start(db, request_id, actor):
    """Issue #4: call and create Walk in THIS transaction; this never creates a Walk.

    Time windows, no-show decisions, and post-approval setting changes are intentionally
    left to Issue #4. The existing UNIQUE(walk_request_id) remains the final guard.
    """
    request, _, _ = locked_request(db, request_id, actor, "START")
    if request.status != "APPROVED":
        raise start_conflict("WALK_START_BOOKING_NOT_ELIGIBLE", "승인된 신청만 산책을 시작할 수 있습니다.")
    if has_walk_record(db, request):
        raise start_conflict("WALK_START_ALREADY_STARTED", "이미 산책이 시작된 예약입니다.")
    return request


def start_conflict(code, message):
    """POST /walks 409 with a machine-readable detail.code (Issue #87)."""
    return HTTPException(409, {"code": code, "message": message})


FINISHED_WALK_STATUSES = ("COMPLETED", "INTERRUPTED")


def request_list_query(db, *, status=None, tab=None, status_group=None, startable_at=None):
    query = db.query(WalkRequest).options(
        joinedload(WalkRequest.dog),
        joinedload(WalkRequest.shelter),
        joinedload(WalkRequest.walk),
    )
    if tab == "COMPLETED":
        query = query.filter(WalkRequest.status == "APPROVED",
                             WalkRequest.walk.has(Walk.status.in_(FINISHED_WALK_STATUSES)))
    elif tab == "APPROVED":
        query = query.filter(WalkRequest.status == "APPROVED",
                             ~WalkRequest.walk.has(Walk.status.in_(FINISHED_WALK_STATUSES)))
    elif tab not in (None, "ALL"):
        query = query.filter(WalkRequest.status == tab)
    elif status is not None:
        query = query.filter(WalkRequest.status == status)
    elif status_group == "PAST":
        query = query.filter(WalkRequest.status.in_(("REJECTED", "CANCELLED")))
    if startable_at is not None:
        query = query.filter(WalkRequest.status == "APPROVED", ~WalkRequest.walk.has(),
                             WalkRequest.requested_start_at <= startable_at,
                             WalkRequest.requested_end_at > startable_at)
    return query.order_by(WalkRequest.created_at.desc(), WalkRequest.id.desc())
