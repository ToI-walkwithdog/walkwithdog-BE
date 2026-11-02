from fastapi import APIRouter, Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.dependencies import require_admin
from app.models import Shelter, User
from app.schemas import DogCardResponse, ShelterCreate, ShelterResponse
from app.dog_walk_settings import dog_list_query

router = APIRouter(
    prefix="/shelters",
    tags=["shelters"]
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/", response_model=ShelterResponse)
def create_shelter(
    shelter: ShelterCreate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    new_shelter = Shelter(
        name=shelter.name,
        phone=shelter.phone,
        address=shelter.address,
        website_url=shelter.website_url
    )

    db.add(new_shelter)
    db.commit()
    db.refresh(new_shelter)

    return new_shelter


@router.get("/{shelter_id}", response_model=ShelterResponse)
def get_shelter(shelter_id: int, db: Session = Depends(get_db)):
    shelter = db.query(Shelter).filter(Shelter.id == shelter_id).first()

    if shelter is None:
        raise HTTPException(status_code=404, detail="Shelter not found")

    return shelter


@router.get("/{shelter_id}/dogs", response_model=list[DogCardResponse])
def get_shelter_dogs(shelter_id: int, walkable: bool | None = None,
                     db: Session = Depends(get_db)):
    if db.get(Shelter, shelter_id) is None:
        raise HTTPException(404, "Shelter not found")
    return dog_list_query(db, walkable=walkable, shelter_id=shelter_id).all()
