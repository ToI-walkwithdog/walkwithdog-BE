from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Dog, Shelter
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
    db: Session = Depends(get_db)
):
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
    db.commit()
    db.refresh(new_dog)

    return new_dog


# --------------------------------------------------
# 전체 강아지 조회
# --------------------------------------------------
@router.get("/", response_model=list[DogCardResponse])
def get_dogs(db: Session = Depends(get_db)):
    return db.query(Dog).all()


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
        .filter(Dog.id == dog_id)
        .first()
    )

    if dog is None:
        raise HTTPException(
            status_code=404,
            detail="Dog not found"
        )

    return dog


# --------------------------------------------------
# 보호소 입력 수정 + 성향 재계산
# --------------------------------------------------
@router.put("/{dog_id}/traits")
def update_dog_traits(
    dog_id: int,
    trait_data: DogTraitUpdate,
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