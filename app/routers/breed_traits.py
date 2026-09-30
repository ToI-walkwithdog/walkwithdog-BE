from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BreedTrait
from app.schemas import BreedTraitCreate, BreedTraitResponse

from app.dependencies import require_admin
from app.models import User

router = APIRouter(
    prefix="/breed-traits",
    tags=["breed-traits"]
)


@router.post("/", response_model=BreedTraitResponse)
def create_breed_trait(
    breed_trait: BreedTraitCreate,
    current_user: User = Depends(require_admin),
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