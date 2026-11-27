from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from app.account_status import ensure_active
from app.api_contract import ContractError, ContractErrorResponse, ContractRoute, ok
from app.auth import get_contract_user
from app.dependencies import require_shelter, require_shelter_access
from app.models import DogWalkDateSlot, User, UserRole
from app.onboarding_common import require_walk_onboarding
from app.services.dog_service import cleanup_dog_image, save_dog_image, validate_and_create_date_slots
from app.walk_date_slots import (availability_detail, cards_for_regions, date_slot_has_reservations,
                                 validate_date_slot, validate_search_regions)

from app.database import SessionLocal
from app.models import Dog, DogWalkSetting, Shelter
from app.dog_walk_settings import apply_walk_settings, dog_list_query
from app.walk_schemas import (DateSlotInput, DateSlotPatch, DateSlotResponse, DogAvailabilityDetailResponse,
                              DogAvailabilityListResponse, DogSizeCode, WalkSettingsPatch, WalkSettingsResponse)
from app.schemas import (
    DogCreate,
    DogResponse,
    DogDetailResponse,
    DogTraitUpdate,
    DogCardResponse
)

router = APIRouter(
    prefix="/dogs",
    tags=["dogs"]
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------
# 보호소 입력 텍스트 기반 성향 자동 계산
# --------------------------------------------------
def calculate_traits_from_text(
    description: str | None,
    special_note: str | None
):
    text = f"{description or ''} {special_note or ''}"

    energy = 3
    separation = 3
    sociability = 3
    training = 3
    independence = 3

    # 활동량
    if any(word in text for word in ["활발", "산책", "뛰", "장난", "에너지"]):
        energy += 1

    # 분리불안
    if any(word in text for word in ["불안", "혼자", "낑낑", "분리"]):
        separation += 1
        independence -= 1

    # 친화력
    if any(word in text for word in ["사람 좋아", "친화", "애교", "순함"]):
        sociability += 1

    # 훈련성
    if any(word in text for word in ["훈련", "앉아", "기다려", "배변"]):
        training += 1

    # 독립성
    if any(word in text for word in ["혼자 잘", "독립", "차분"]):
        independence += 1

    return {
        "energy_level": max(1, min(5, energy)),
        "separation_anxiety_level": max(1, min(5, separation)),
        "sociability_level": max(1, min(5, sociability)),
        "training_level": max(1, min(5, training)),
        "independence_level": max(1, min(5, independence)),
    }


# --------------------------------------------------
# 강아지 생성
# --------------------------------------------------
@router.post("/", response_model=DogResponse)
def create_dog(
    dog: DogCreate,
    current_user: User = Depends(require_shelter),
    db: Session = Depends(get_db)
):
    new_dog = build_dog(db, dog, current_user)
    db.add(new_dog)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "이미 등록된 강아지 식별번호이거나 데이터가 충돌합니다.") from None
    db.refresh(new_dog)
    return new_dog


def build_dog(db, dog, actor):
    require_shelter_access(actor, dog.shelter_id)
    if dog.shelter_id is not None:
        shelter = (
            db.query(Shelter)
            .filter(Shelter.id == dog.shelter_id)
            .first()
        )

        if shelter is None:
            raise HTTPException(
                status_code=404,
                detail="Shelter not found"
            )

    # 보호소 입력 텍스트 기반 성향 계산
    calculated_traits = calculate_traits_from_text(
        description=dog.description,
        special_note=dog.special_note
    )

    return Dog(
        shelter_id=dog.shelter_id,
        desertion_no=dog.desertion_no,

        name=dog.name,
        breed=dog.breed,
        age=dog.age,
        gender=dog.gender,
        weight=dog.weight,

        image_url=dog.image_url,

        description=dog.description,
        special_note=dog.special_note,

        personality_keywords=dog.personality_keywords,

        energy_level=calculated_traits["energy_level"],
        separation_anxiety_level=calculated_traits["separation_anxiety_level"],
        sociability_level=calculated_traits["sociability_level"],
        training_level=calculated_traits["training_level"],
        independence_level=calculated_traits["independence_level"],

        barking_level=dog.barking_level,
        walk_need=dog.walk_need,

        good_with_children=dog.good_with_children,
        good_with_other_dogs=dog.good_with_other_dogs,

        separation_anxiety=dog.separation_anxiety,

        trait_source="shelter_text_parsed"
    )



class AvailabilityRoute(ContractRoute):
    def validation_error(self, exc):
        field = exc.errors()[0]["loc"][-1]
        code, message = {
            "dog_id": ("INVALID_DOG_ID", "강아지 ID를 확인해 주세요."),
            "size_code": ("INVALID_DOG_SIZE", "강아지 크기를 확인해 주세요."),
        }.get(field, ("INVALID_PAGINATION", "목록 조회 범위를 확인해 주세요."))
        return ContractError(422, code, message, {"field": field})


def availability_actor(actor: User = Depends(get_contract_user), db: Session = Depends(get_db)):
    if actor.role != UserRole.USER:
        raise ContractError(403, "WALK_LIST_NOT_ALLOWED", "산책 가능 일정을 조회할 수 없습니다.")
    ensure_active(actor)
    require_walk_onboarding(db, actor)
    return actor


availability_router = APIRouter(route_class=AvailabilityRoute, dependencies=[Depends(availability_actor)])
availability_errors = {
    401: {"model": ContractErrorResponse, "description": "AUTHENTICATION_REQUIRED, INVALID_ACCESS_TOKEN, SESSION_EXPIRED"},
    403: {"model": ContractErrorResponse, "description": "WALK_LIST_NOT_ALLOWED, AGE_REVIEW_REQUIRED, ONBOARDING_NOT_COMPLETED"},
    500: {"model": ContractErrorResponse, "description": "INTERNAL_SERVER_ERROR"},
}


@availability_router.get("/walk-availability", response_model=DogAvailabilityListResponse, responses={
    **availability_errors,
    422: {"model": ContractErrorResponse, "description": (
        "REGIONS_REQUIRED, REGION_LIMIT_EXCEEDED, INVALID_REGION_CODE, INVALID_DOG_SIZE, INVALID_PAGINATION")},
    503: {"model": ContractErrorResponse, "description": "REGION_DATA_UNAVAILABLE"},
})
def list_walk_availability(region_codes: Annotated[list[str] | None, Query()] = None,
                           size_code: DogSizeCode | None = None, limit: int = Query(20, ge=1, le=100),
                           offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    validate_search_regions(db, region_codes)
    return ok(cards_for_regions(db, region_codes, size_code, limit, offset, datetime.now(timezone.utc)),
              "산책 가능 목록을 조회했습니다.")


@availability_router.get("/{dog_id}/walk-availability", response_model=DogAvailabilityDetailResponse, responses={
    **availability_errors,
    404: {"model": ContractErrorResponse, "description": "WALK_DOG_NOT_AVAILABLE"},
    422: {"model": ContractErrorResponse, "description": "INVALID_DOG_ID"},
})
def get_walk_availability(dog_id: Annotated[int, Field(gt=0, le=2147483647)], db: Session = Depends(get_db)):
    return ok(availability_detail(db, dog_id, datetime.now(timezone.utc)), "산책 상세와 신청 가능 일정을 조회했습니다.")


router.include_router(availability_router)


# --------------------------------------------------
# 전체 강아지 조회
# --------------------------------------------------
@router.get("/", response_model=list[DogCardResponse])
def get_dogs(walkable: bool | None = None, shelter_id: int | None = None,
             db: Session = Depends(get_db)):
    return dog_list_query(db, walkable=walkable, shelter_id=shelter_id).all()


# --------------------------------------------------
# 강아지 상세 조회
# --------------------------------------------------
@router.get("/{dog_id}", response_model=DogDetailResponse)
def get_dog(
    dog_id: int,
    db: Session = Depends(get_db)
):
    dog = (
        db.query(Dog)
        .options(joinedload(Dog.shelter), joinedload(Dog.walk_setting).selectinload(DogWalkSetting.slots))
        .filter(Dog.id == dog_id)
        .first()
    )

    if dog is None:
        raise HTTPException(
            status_code=404,
            detail="Dog not found"
        )

    return dog


@router.patch("/{dog_id}/walk-settings", response_model=WalkSettingsResponse)
def update_walk_settings(
    dog_id: int,
    payload: WalkSettingsPatch,
    current_user: User = Depends(require_shelter),
    db: Session = Depends(get_db),
):
    try:
        # Lock the existing parent even if its one-to-one setting does not exist.
        # Concurrent writers fail promptly with 409 and may retry the PATCH.
        dog = db.query(Dog).filter(Dog.id == dog_id).with_for_update(nowait=True).first()
        if dog is None:
            raise HTTPException(404, "Dog not found")
        require_shelter_access(current_user, dog.shelter_id)
        setting = apply_walk_settings(db, dog, payload)
        # Snapshot under the lock, before commit expires ORM state.
        response = WalkSettingsResponse.model_validate(setting)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "산책 설정 데이터가 충돌합니다. 다시 시도하세요.") from None
    except DBAPIError as exc:
        db.rollback()
        if getattr(exc.orig, "pgcode", None) in ("40001", "40P01", "55P03"):
            raise HTTPException(409, "다른 작업이 산책 정보를 수정 중입니다. 다시 시도하세요.") from None
        raise HTTPException(500, "산책 설정을 저장하지 못했습니다.") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(500, "산책 설정을 저장하지 못했습니다.") from None
    return response


# --------------------------------------------------
# 보호소 입력 수정 + 성향 재계산
# --------------------------------------------------
@router.put("/{dog_id}/traits")
def update_dog_traits(
    dog_id: int,
    trait_data: DogTraitUpdate,
    current_user: User = Depends(require_shelter),
    db: Session = Depends(get_db)
):
    dog = (
        db.query(Dog)
        .filter(Dog.id == dog_id)
        .first()
    )

    if dog is None:
        raise HTTPException(
            status_code=404,
            detail="Dog not found"
        )

    require_shelter_access(current_user, dog.shelter_id)
    update_data = trait_data.model_dump(exclude_unset=True)

    # 입력값 업데이트
    for key, value in update_data.items():
        setattr(dog, key, value)

    # 수정된 텍스트 기반 성향 재계산
    calculated_traits = calculate_traits_from_text(
        description=dog.description,
        special_note=dog.special_note
    )

    dog.energy_level = calculated_traits["energy_level"]
    dog.separation_anxiety_level = calculated_traits["separation_anxiety_level"]
    dog.sociability_level = calculated_traits["sociability_level"]
    dog.training_level = calculated_traits["training_level"]
    dog.independence_level = calculated_traits["independence_level"]

    dog.trait_source = "shelter_text_parsed"

    db.commit()
    db.refresh(dog)

    return {
        "message": "보호소 입력 정보를 기반으로 성향이 자동 계산되었습니다.",
        "dog_id": dog.id,
        "trait_source": dog.trait_source,
        "calculated_traits": calculated_traits
    }

slot_batch = TypeAdapter(Annotated[list[DateSlotInput], Field(min_length=1)])


class RegisteredDog(DogResponse):
    age_months: int


class DogRegistrationResponse(BaseModel):
    dog: RegisteredDog
    walk_settings: WalkSettingsResponse
    date_slots: list[DateSlotResponse]


def date_slot_error(exc):
    original = getattr(exc, "orig", None)
    if isinstance(exc, IntegrityError) or getattr(original, "pgcode", None) in ("40001", "40P01", "55P03"):
        return HTTPException(409, "다른 일정과 충돌했습니다. 다시 조회한 뒤 시도하세요.")
    return HTTPException(500, "산책 일정을 저장하지 못했습니다.")


@router.post("/walk-registration", response_model=DogRegistrationResponse, status_code=201)
def register_dog_with_date_slots(
    name: str = Form(..., min_length=1, max_length=50), breed: str | None = Form(None, max_length=50),
    age: int | None = Form(None, ge=0), age_months: int = Form(0, ge=0, le=11),
    difficulty: str = Form("BEGINNER"), precautions: str | None = Form(None),
    size_code: DogSizeCode | None = Form(None), difficulty_score: float | None = Form(None),
    max_walk_minutes: int | None = Form(None), shelter_id: int | None = Form(None, gt=0),
    slots: str = Form(...), image: UploadFile = File(...),
    db: Session = Depends(get_db), actor: User = Depends(require_shelter),
):
    shelter_id = shelter_id if shelter_id is not None else actor.shelter_id
    if shelter_id is None:
        raise HTTPException(422 if actor.role == UserRole.ADMIN else 403, "등록할 보호소를 확인해 주세요.")
    try:
        slot_inputs = slot_batch.validate_json(slots)
        settings = WalkSettingsPatch(walkable=True, difficulty=difficulty, precautions=precautions,
                                     size_code=size_code, difficulty_score=difficulty_score,
                                     max_walk_minutes=max_walk_minutes)
    except ValidationError:
        raise HTTPException(422, "산책 설정 및 날짜 일정을 확인해 주세요.") from None
    dog = build_dog(db, DogCreate(name=name, breed=breed, age=age, shelter_id=shelter_id), actor)
    image_url = save_dog_image(image)
    try:
        dog.image_url, dog.age_months = image_url, age_months
        db.add(dog)
        db.flush()
        setting = apply_walk_settings(db, dog, settings)
        date_slots = validate_and_create_date_slots(db, dog.id, slot_inputs)
        output = DogRegistrationResponse(dog=RegisteredDog.model_validate(dog),
                                         walk_settings=WalkSettingsResponse.model_validate(setting),
                                         date_slots=[DateSlotResponse.model_validate(slot) for slot in date_slots])
        db.commit()
        return output
    except Exception as exc:
        db.rollback()
        cleanup_dog_image(image_url)
        if isinstance(exc, SQLAlchemyError):
            raise date_slot_error(exc) from None
        raise


def shelter_dog(db, dog_id, actor, *, lock=False):
    query = db.query(Dog).filter(Dog.id == dog_id)
    if lock:
        query = query.with_for_update(nowait=True)
    dog = query.first()
    if dog is None:
        raise HTTPException(404, "Dog not found")
    require_shelter_access(actor, dog.shelter_id)
    if dog.shelter_id is None:
        raise HTTPException(409, "강아지에 소속 보호소를 지정해 주세요.")
    return dog


@router.get("/{dog_id}/date-slots", response_model=list[DateSlotResponse])
def list_date_slots(dog_id: int, db: Session = Depends(get_db), actor: User = Depends(require_shelter)):
    shelter_dog(db, dog_id, actor)
    return (db.query(DogWalkDateSlot).filter_by(dog_id=dog_id)
            .order_by(DogWalkDateSlot.slot_date, DogWalkDateSlot.start_time, DogWalkDateSlot.id).all())


@router.post("/{dog_id}/date-slots", response_model=list[DateSlotResponse], status_code=201)
def add_date_slots(dog_id: int, payload: Annotated[list[DateSlotInput], Field(min_length=1)],
                   db: Session = Depends(get_db), actor: User = Depends(require_shelter)):
    try:
        shelter_dog(db, dog_id, actor, lock=True)
        output = [DateSlotResponse.model_validate(row) for row in validate_and_create_date_slots(db, dog_id, payload)]
        db.commit()
        return output
    except (HTTPException, SQLAlchemyError) as exc:
        db.rollback()
        if isinstance(exc, SQLAlchemyError):
            raise date_slot_error(exc) from None
        raise


def managed_date_slot(db, dog_id, slot_id, actor):
    shelter_dog(db, dog_id, actor, lock=True)
    slot = db.get(DogWalkDateSlot, slot_id)
    if slot is None or slot.dog_id != dog_id:
        raise HTTPException(404, "산책 일정을 찾을 수 없습니다.")
    return slot


@router.patch("/{dog_id}/date-slots/{slot_id}", response_model=DateSlotResponse)
def change_date_slot(dog_id: int, slot_id: int, payload: DateSlotPatch, db: Session = Depends(get_db),
                     actor: User = Depends(require_shelter)):
    try:
        slot = managed_date_slot(db, dog_id, slot_id, actor)
        if not slot.is_active:
            raise HTTPException(409, "종료한 산책 일정은 변경할 수 없습니다.")
        previous = {key: getattr(slot, key) for key in DateSlotInput.model_fields}
        values = {**previous, **payload.model_dump(exclude_unset=True)}
        try:
            update = DateSlotInput.model_validate(values)
        except ValidationError:
            raise HTTPException(422, "산책 일정의 날짜와 시각을 확인해 주세요.") from None
        values = update.model_dump()
        if values != previous:
            if date_slot_has_reservations(db, slot, active_only=True):
                raise HTTPException(409, "활성 예약이 연결된 일정은 변경할 수 없습니다.")
            validate_date_slot(db, dog_id, update, datetime.now(timezone.utc), exclude_id=slot.id)
            if date_slot_has_reservations(db, slot):
                slot.is_active = False
                slot = DogWalkDateSlot(dog_id=dog_id, **values)
                db.add(slot)
            else:
                for key, value in values.items():
                    setattr(slot, key, value)
        db.flush()
        output = DateSlotResponse.model_validate(slot)
        db.commit()
        return output
    except (HTTPException, SQLAlchemyError) as exc:
        db.rollback()
        if isinstance(exc, SQLAlchemyError):
            raise date_slot_error(exc) from None
        raise


@router.delete("/{dog_id}/date-slots/{slot_id}", status_code=204)
def retire_date_slot(dog_id: int, slot_id: int, db: Session = Depends(get_db), actor: User = Depends(require_shelter)):
    try:
        slot = managed_date_slot(db, dog_id, slot_id, actor)
        if date_slot_has_reservations(db, slot, active_only=True):
            raise HTTPException(409, "활성 예약이 연결된 일정은 삭제할 수 없습니다.")
        slot.is_active = False
        db.commit()
    except (HTTPException, SQLAlchemyError) as exc:
        db.rollback()
        if isinstance(exc, SQLAlchemyError):
            raise date_slot_error(exc) from None
        raise


dog_router = router
router = APIRouter()
router.include_router(dog_router)


@router.get("/media/dogs/{filename}", tags=["dogs"])
def get_registered_dog_image(filename: str):
    if not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\.(jpg|png)", filename):
        raise HTTPException(404, "강아지 사진을 찾을 수 없습니다.")
    directory = Path(os.getenv("UPLOAD_DIR", "./uploads/dogs")).resolve()
    target = (directory / filename).resolve()
    if target.parent != directory or not target.is_file():
        raise HTTPException(404, "강아지 사진을 찾을 수 없습니다.")
    return FileResponse(target, media_type="image/png" if filename.endswith(".png") else "image/jpeg")
