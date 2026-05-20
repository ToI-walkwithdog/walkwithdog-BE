from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Dog, Shelter
from schemas import DogCreate, DogResponse, DogDetailResponse

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


@router.post("/", response_model=DogResponse)
def create_dog(dog: DogCreate, db: Session = Depends(get_db)):
    if dog.shelter_id is not None:
        shelter = db.query(Shelter).filter(Shelter.id == dog.shelter_id).first()
        if shelter is None:
            raise HTTPException(status_code=404, detail="Shelter not found")

    new_dog = Dog(
    shelter_id=dog.shelter_id,
    name=dog.name,
    breed=dog.breed,
    age=dog.age,
    gender=dog.gender,
    image_url=dog.image_url,
    description=dog.description,
    personality_keywords=dog.personality_keywords,

    energy_level=dog.energy_level,
    barking_level=dog.barking_level,
    independence_level=dog.independence_level,
    sociability_level=dog.sociability_level,
    walk_need=dog.walk_need,

    good_with_children=dog.good_with_children,
    good_with_other_dogs=dog.good_with_other_dogs,
    separation_anxiety=dog.separation_anxiety
)

    db.add(new_dog)
    db.commit()
    db.refresh(new_dog)

    return new_dog


@router.get("/", response_model=list[DogResponse])
def get_dogs(db: Session = Depends(get_db)):
    return db.query(Dog).all()


@router.get("/{dog_id}", response_model=DogDetailResponse)
def get_dog(dog_id: int, db: Session = Depends(get_db)):
    dog = db.query(Dog).filter(Dog.id == dog_id).first()

    if dog is None:
        raise HTTPException(status_code=404, detail="Dog not found")

    return dog