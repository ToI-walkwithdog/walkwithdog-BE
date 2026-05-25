from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BreedTrait
from app.schemas import BreedTraitCreate, BreedTraitResponse

router = APIRouter(
    prefix="/breed-traits",
    tags=["breed-traits"]
)


@router.post("/", response_model=BreedTraitResponse)
def create_breed_trait(
    breed_trait: BreedTraitCreate,
    db: Session = Depends(get_db)
):
    new_trait = BreedTrait(**breed_trait.model_dump())

    db.add(new_trait)
    db.commit()
    db.refresh(new_trait)

    return new_trait


@router.get("/", response_model=list[BreedTraitResponse])
def get_breed_traits(db: Session = Depends(get_db)):
    return db.query(BreedTrait).all()