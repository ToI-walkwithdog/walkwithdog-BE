"""Small shared queries and walk-setting updates; the router owns the transaction."""
from fastapi import HTTPException
from sqlalchemy.sql import func

from app.models import Dog, DogWalkSetting, DogWalkSlot


def dog_list_query(db, walkable=None, shelter_id=None):
    query = db.query(Dog)
    if shelter_id is not None:
        query = query.filter(Dog.shelter_id == shelter_id)
    if walkable is not None:
        enabled = Dog.walk_setting.has(DogWalkSetting.walkable.is_(True))
        query = query.filter(enabled if walkable else ~enabled)
    return query.order_by(Dog.id)


def validate_walk_slots(slots):
    ordered = sorted(slots, key=lambda slot: (slot.day_of_week, slot.start_time, slot.end_time))
    for previous, current in zip(ordered, ordered[1:]):
        if previous.day_of_week == current.day_of_week and current.start_time < previous.end_time:
            raise HTTPException(409, "같은 요일의 산책 시간대가 중복되거나 겹칩니다.")


def apply_walk_settings(db, dog, payload):
    """The caller must hold the parent Dog row lock, including on first creation."""
    changes = payload.model_dump(exclude_unset=True, exclude={"slots"})
    replace_slots = "slots" in payload.model_fields_set
    if replace_slots:
        validate_walk_slots(payload.slots)

    setting = db.query(DogWalkSetting).filter(DogWalkSetting.dog_id == dog.id).first()
    if setting is None:
        setting = DogWalkSetting(dog_id=dog.id)
        db.add(setting)
    for key, value in changes.items():
        setattr(setting, key, value)
    # A slots-only PATCH also updates the parent's modification timestamp.
    setting.updated_at = func.now()
    db.flush()

    if replace_slots:
        # Delete before insert so replacing an identical slot cannot hit UNIQUE.
        db.query(DogWalkSlot).filter(DogWalkSlot.walk_setting_id == setting.id).delete(synchronize_session="fetch")
        db.expire(setting, ["slots"])
        setting.slots = [DogWalkSlot(**slot.model_dump()) for slot in payload.slots]
    db.flush()
    return setting
