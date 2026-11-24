"""Date slot validation, lookup, and availability shared by reads and booking writes."""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import DateTime, and_, func, or_, select
from sqlalchemy.orm import aliased

from app.api_contract import ContractError
from app.models import Dog, DogWalkDateSlot, DogWalkSetting, Shelter, WalkRequest
from app.onboarding_common import KST, as_utc
from app.region_models import LEVEL_SIDO, SELECTABLE_LEVELS, Region
from app.region_service import (InvalidRegionCodes, require_selectable_regions, region_data_unavailable)

ACTIVE = ("PENDING", "APPROVED")
REGISTRATION_DAYS = 180


def slot_times(slot):
    return tuple(datetime.combine(slot.slot_date, time, KST).astimezone(timezone.utc)
                 for time in (slot.start_time, slot.end_time))


def slot_unavailable():
    return ContractError(409, "WALK_SLOT_UNAVAILABLE", "선택한 산책 일정을 신청할 수 없습니다.")


def slot_changed():
    return ContractError(409, "WALK_SLOT_CHANGED", "선택한 일정이 변경됐어요. 일정을 다시 선택해 주세요.")


def dog_not_available():
    return ContractError(404, "WALK_DOG_NOT_AVAILABLE", "산책 가능한 강아지를 찾을 수 없습니다.")


def occupied(db, dog_id, start, end, *, slot_id=None, exclude_id=None, date_only=False):
    query = db.query(WalkRequest.id).filter(WalkRequest.dog_id == dog_id, WalkRequest.status.in_(ACTIVE))
    overlap = (WalkRequest.requested_start_at < end) & (WalkRequest.requested_end_at > start)
    if slot_id is not None:
        overlap = or_(WalkRequest.date_slot_id == slot_id, overlap)
    if date_only:
        query = query.filter(WalkRequest.date_slot_id.is_not(None))
    if exclude_id is not None:
        query = query.filter(WalkRequest.id != exclude_id)
    return query.filter(overlap).first() is not None


def check_booking_slot(db, dog_id, slot_id, start, end, now, *, exclude_id=None, require_future=True):
    """The caller holds the parent Dog lock for the entire booking transaction."""
    slot = db.get(DogWalkDateSlot, slot_id, populate_existing=True)
    if slot is None or not slot.is_active:
        raise slot_unavailable()
    if slot.dog_id != dog_id:
        raise ContractError(422, "INVALID_WALK_REQUEST", "선택한 강아지와 일정이 일치하지 않습니다.")
    slot_start, slot_end = slot_times(slot)
    if slot_end - slot_start < timedelta(minutes=slot.duration_minutes):
        raise slot_unavailable()
    if (slot_start, slot_end) != (as_utc(start), as_utc(end)):
        raise slot_changed()
    cutoff = now.astimezone(KST).date() + timedelta(days=REGISTRATION_DAYS)
    if (require_future and (slot_start <= now or slot.slot_date > cutoff)) or occupied(
        db, dog_id, start, end, slot_id=slot_id, exclude_id=exclude_id,
    ):
        raise slot_unavailable()
    return slot


def validate_date_slot(db, dog_id, slot, now, *, exclude_id=None):
    start, end = slot_times(slot)
    if start <= now or slot.slot_date > now.astimezone(KST).date() + timedelta(days=REGISTRATION_DAYS):
        raise HTTPException(422, "산책 일정은 현재 시각 이후부터 180일 이내여야 합니다.")
    if end - start < timedelta(minutes=slot.duration_minutes):
        raise HTTPException(422, "산책 소요시간이 일정의 시간 범위를 넘습니다.")
    query = db.query(DogWalkDateSlot.id).filter(
        DogWalkDateSlot.dog_id == dog_id, DogWalkDateSlot.slot_date == slot.slot_date,
        DogWalkDateSlot.is_active.is_(True), DogWalkDateSlot.start_time < slot.end_time,
        DogWalkDateSlot.end_time > slot.start_time,
    )
    if exclude_id is not None:
        query = query.filter(DogWalkDateSlot.id != exclude_id)
    if query.first() is not None:
        raise HTTPException(409, "같은 강아지의 산책 일정과 시간이 겹칩니다.")


def date_slot_has_reservations(db, slot, *, active_only=False):
    query = db.query(WalkRequest.id).filter(WalkRequest.date_slot_id == slot.id)
    if active_only:
        query = query.filter(WalkRequest.status.in_(ACTIVE))
    return query.first() is not None


def validate_search_regions(db, codes):
    if not codes:
        raise ContractError(422, "REGIONS_REQUIRED", "검색할 지역을 선택해 주세요.", {"field": "region_codes"})
    if len(codes) > 3:
        raise ContractError(422, "REGION_LIMIT_EXCEEDED", "검색 지역은 최대 3개까지 선택할 수 있습니다.",
                            {"field": "region_codes", "max": 3})
    if not db.query(Region.region_code).filter(Region.is_active.is_(True), Region.region_level == LEVEL_SIDO).first():
        raise region_data_unavailable()
    try:
        require_selectable_regions(db, codes)
    except InvalidRegionCodes:
        raise ContractError(422, "INVALID_REGION_CODE", "검색 지역을 확인해 주세요.",
                            {"field": "region_codes"}) from None


def available_slot_filters(db, now):
    """Express the same half-open interval rule in SQL for paging by dog, not slot."""
    slot = DogWalkDateSlot
    if db.get_bind().dialect.name == "sqlite":
        start = func.julianday(slot.slot_date, slot.start_time, "-9 hours")
        end = func.julianday(slot.slot_date, slot.end_time, "-9 hours")
        request_start = func.julianday(WalkRequest.requested_start_at)
        request_end = func.julianday(WalkRequest.requested_end_at)
        clock = func.julianday(as_utc(now).isoformat())
        # Round the elapsed seconds to avoid Julian day floating-point boundary error.
        duration = func.round((end - start) * 86400, 3)
    else:
        start = func.timezone("Asia/Seoul", slot.slot_date.op("+")(slot.start_time), type_=DateTime(timezone=True))
        end = func.timezone("Asia/Seoul", slot.slot_date.op("+")(slot.end_time), type_=DateTime(timezone=True))
        request_start, request_end, clock = WalkRequest.requested_start_at, WalkRequest.requested_end_at, now
        duration = func.extract("epoch", end - start)
    conflict = (select(WalkRequest.id)
                .where(WalkRequest.dog_id == slot.dog_id, WalkRequest.status.in_(ACTIVE),
                       or_(WalkRequest.date_slot_id == slot.id, (request_start < end) & (request_end > start)))
                .correlate(slot).exists())
    cutoff = now.astimezone(KST).date() + timedelta(days=REGISTRATION_DAYS)
    return slot.is_active.is_(True), slot.slot_date <= cutoff, start > clock, duration >= slot.duration_minutes * 60, ~conflict


def candidate_dogs(db):
    parent = Region.__table__.alias("region_parent")
    active_parent = select(parent.c.region_code).where(parent.c.region_code == Region.parent_region_code,
                                                       parent.c.is_active.is_(True)).correlate(Region).exists()
    return (db.query(Dog, DogWalkSetting, Shelter, Region)
            .join(DogWalkSetting, DogWalkSetting.dog_id == Dog.id)
            .join(Shelter, Shelter.id == Dog.shelter_id)
            .outerjoin(Region, and_(Region.region_code == Shelter.region_code, Region.is_active.is_(True),
                                    Region.region_level.in_(SELECTABLE_LEVELS), active_parent))
            .filter(DogWalkSetting.walkable.is_(True)))


def cards_for_regions(db, codes, size_code, limit, offset, now):
    slot = DogWalkDateSlot
    filters = available_slot_filters(db, now)
    has_slot = select(slot.id).where(slot.dog_id == Dog.id, *filters).correlate(Dog).exists()
    query = candidate_dogs(db).filter(Region.region_code.in_(codes), has_slot)
    if size_code is not None:
        query = query.filter(DogWalkSetting.size_code == size_code)
    page = query.with_entities(Dog.id.label("dog_id")).order_by(Dog.id).offset(offset).limit(limit + 1).cte()
    # Keep the page and its previews on the same snapshot. Read a third slot only to flag more.
    position = func.row_number().over(partition_by=slot.dog_id,
                                     order_by=(slot.slot_date, slot.start_time, slot.end_time, slot.id)).label("position")
    ranked = db.query(slot, position).join(page, page.c.dog_id == slot.dog_id).filter(*filters).subquery()
    preview = aliased(slot, ranked)
    rows = (candidate_dogs(db).join(ranked, and_(Dog.id == ranked.c.dog_id, ranked.c.position <= 3))
            .add_entity(preview).order_by(Dog.id, ranked.c.position).all())
    items = {}
    for dog, setting, shelter, region, available in rows:
        if dog.id not in items:
            items[dog.id] = {**card(dog, setting, shelter, region), "date_slots": [], "has_more_date_slots": False}
        item = items[dog.id]
        if len(item["date_slots"]) < 2:
            item["date_slots"].append(public_slot(available))
        else:
            item["has_more_date_slots"] = True
    more = len(items) > limit
    return {"items": list(items.values())[:limit], "limit": limit, "offset": offset, "has_more": more,
            "next_offset": offset + limit if more else None}


def slots_by_dog(db, dog_ids, filters):
    result = {}
    if dog_ids:
        slot = DogWalkDateSlot
        rows = (db.query(slot).filter(slot.dog_id.in_(dog_ids), *filters)
                .order_by(slot.slot_date, slot.start_time, slot.end_time, slot.id).all())
        for row in rows:
            result.setdefault(row.dog_id, []).append(public_slot(row))
    return result


def public_slot(slot):
    start, end = slot_times(slot)
    return {"date_slot_id": slot.id, "starts_at": utc_string(start), "ends_at": utc_string(end)}


def utc_string(value):
    return as_utc(value).isoformat().replace("+00:00", "Z")


def card(dog, setting, shelter, region):
    return {"dog_id": dog.id, "name": dog.name, "image_url": dog.image_url, "size_code": setting.size_code,
            "difficulty_score": float(setting.difficulty_score) if setting.difficulty_score is not None else None,
            "max_walk_minutes": setting.max_walk_minutes,
            "shelter": {"id": shelter.id, "name": shelter.name, "region_code": region.region_code if region else None,
                        "region_name": region.region_name if region else None}}


def availability_detail(db, dog_id, now):
    row = candidate_dogs(db).filter(Dog.id == dog_id).first()
    if row is None:
        raise dog_not_available()
    dog, setting, shelter, region = row
    return {**card(dog, setting, shelter, region), "description": dog.description, "precautions": setting.precautions,
            "date_slots": slots_by_dog(db, [dog.id], available_slot_filters(db, now)).get(dog.id, [])}
