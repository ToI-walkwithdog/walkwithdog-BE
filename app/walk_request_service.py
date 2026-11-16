"""Walk request policy; callers commit/rollback once and keep locks until commit.

Writer lock order: Dog -> applicant User -> existing WalkRequest -> VolunteerProfile.
NOWAIT avoids unbounded waits; all locks are database row locks, never process locks.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.dependencies import require_shelter_access
from app.models import Dog, DogWalkSetting, Survey, User, UserRole, VolunteerProfile, Walk, WalkRequest

SEOUL = ZoneInfo("Asia/Seoul")
ACTIVE = ("PENDING", "APPROVED")


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


def validate_eligibility(db, dog, volunteer, start, end, shelter_id=None, *, require_future=True):
    if volunteer.role != UserRole.USER:
        raise HTTPException(409, "현재 신청자가 봉사자 계정이 아닙니다.")
    if dog.shelter_id is None:
        raise HTTPException(409, "보호소가 지정되지 않은 강아지입니다.")
    if shelter_id is not None and dog.shelter_id != shelter_id:
        raise HTTPException(409, "신청 이후 강아지 소속 보호소가 변경되었습니다. 운영 확인이 필요합니다.")
    setting = db.query(DogWalkSetting).filter_by(dog_id=dog.id).populate_existing().first()
    if setting is None or not setting.walkable:
        raise HTTPException(409, "현재 산책 가능한 강아지가 아닙니다.")
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
    check_active_overlap(db, volunteer.id, dog.id, start, end)
    check_approved_conflicts(db, volunteer.id, dog.id, start, end)
    request = WalkRequest(user_id=volunteer.id, dog=dog, shelter_id=dog.shelter_id,
                          requested_start_at=start, requested_end_at=end, status="PENDING")
    db.add(request)
    db.flush()
    return request


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


def ensure_not_started(db, request):
    if db.query(Walk.id).filter_by(walk_request_id=request.id).first():
        raise HTTPException(409, "이미 산책 기록이 생성된 신청입니다.")


def transition_request(db, request_id, actor, payload):
    request, dog, volunteer = locked_request(db, request_id, actor, payload.action)
    if payload.action == "CANCEL":
        if request.status not in ACTIVE:
            raise HTTPException(409, "거절되거나 취소된 신청은 취소할 수 없습니다.")
        if as_utc(request.requested_start_at) <= utc_now():
            raise HTTPException(409, "예정 시작 시각이 지난 신청은 취소할 수 없습니다.")
        ensure_not_started(db, request)
        request.status = "CANCELLED"  # Preserve any previous review metadata.
    else:
        if request.status != "PENDING":
            raise HTTPException(409, "승인 대기 중인 신청만 승인하거나 거절할 수 있습니다.")
        if payload.action == "APPROVE":
            locked_profile(db, volunteer.id)
            validate_eligibility(db, dog, volunteer, request.requested_start_at,
                                 request.requested_end_at, shelter_id=request.shelter_id)
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
        .options(joinedload(WalkRequest.dog))
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
    latest_survey = (
        db.query(Survey)
        .filter_by(user_id=request.user_id)
        .order_by(Survey.created_at.desc(), Survey.id.desc())
        .first()
    )
    setting = db.query(DogWalkSetting).filter_by(dog_id=request.dog_id).first()
    dog_difficulty = setting.difficulty if setting and setting.difficulty else "BEGINNER"

    return {
        "id": request.id,
        "user_id": request.user_id,
        "dog_id": request.dog_id,
        "shelter_id": request.shelter_id,
        "dog": request.dog,
        "requested_start_at": request.requested_start_at,
        "requested_end_at": request.requested_end_at,
        "status": request.status,
        "reviewed_by": request.reviewed_by,
        "reviewed_at": request.reviewed_at,
        "rejection_reason": request.rejection_reason,
        "rejection_reason_code": request.rejection_reason_code,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
        "applicant": {
            "id": applicant_user.id if applicant_user else request.user_id,
            "nickname": applicant_user.nickname if applicant_user else "",
            "name": None,
            "age": None,
            "dog_experience": latest_survey.dog_experience if latest_survey else None,
            "experienced_breed": None,
            "walk_frequency": latest_survey.walk_frequency if latest_survey else None,
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
        raise HTTPException(409, "승인된 신청만 산책을 시작할 수 있습니다.")
    ensure_not_started(db, request)
    return request


def request_list_query(db, status=None):
    query = db.query(WalkRequest).options(joinedload(WalkRequest.dog))
    if status is not None:
        query = query.filter(WalkRequest.status == status)
    return query.order_by(WalkRequest.created_at.desc(), WalkRequest.id.desc())
