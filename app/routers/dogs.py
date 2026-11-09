from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from app.dependencies import require_shelter, require_shelter_access
from app.models import User

from app.database import SessionLocal
from app.models import Dog, DogWalkSetting, Shelter
from app.dog_walk_settings import apply_walk_settings, dog_list_query
from app.walk_schemas import WalkSettingsPatch, WalkSettingsResponse
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
    require_shelter_access(current_user, dog.shelter_id)
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

    new_dog = Dog(
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

    db.add(new_dog)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "이미 등록된 강아지 식별번호이거나 데이터가 충돌합니다.") from None
    db.refresh(new_dog)

    return new_dog


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

@router.post("/", status_code=201)
def create_dog_with_slots(
    name: str = Form(...),
    breed: str = Form(...),
    age: int = Form(...),
    age_months: int = Form(...),
    difficulty: str = Form(...),
    precautions: str = Form(""),
    slots: str = Form(...), # JSON 문자열 형태로 파싱
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user = Depends(require_shelter)
):
    # 1. 보호소 정보 자동 매핑
    shelter_id = current_user.shelter_id
    
    # 2. 이미지 처리
    image_url = save_dog_image(image)
    
    # 3. 강아지 데이터 생성 (트랜잭션 생략)
    # ... 강아지 저장 로직 ...
    
    # 4. 슬롯 JSON 파싱 및 검증/저장
    import json
    slots_data = json.loads(slots) 
    # validate_and_create_date_slots(db, new_dog.id, parsed_slots_data)
    
    return {"message": "Dog and slots registered successfully"}