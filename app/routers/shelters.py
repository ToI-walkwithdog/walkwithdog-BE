from fastapi import APIRouter, Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Shelter
from schemas import ShelterCreate, ShelterResponse

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