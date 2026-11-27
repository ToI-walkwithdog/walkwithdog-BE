from fastapi import APIRouter, Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.dependencies import require_admin, require_shelter, require_shelter_access
from app.models import Shelter, User
from app.schemas import DogCardResponse, ShelterCreate, ShelterRegionPatch, ShelterResponse
from app.dog_walk_settings import dog_list_query
from app.region_service import InvalidRegionCodes, require_selectable_region

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


def selectable_region(db, code):
    try:
        return require_selectable_region(db, code, lock=True)
    except InvalidRegionCodes:
        raise HTTPException(422, "선택할 수 있는 지역 코드를 입력하세요.") from None


@router.post("/", response_model=ShelterResponse)
def create_shelter(
    shelter: ShelterCreate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    if shelter.region_code is not None:
        selectable_region(db, shelter.region_code)
    new_shelter = Shelter(
        name=shelter.name,
        phone=shelter.phone,
        address=shelter.address,
        website_url=shelter.website_url,
        region_code=shelter.region_code,
    )

    db.add(new_shelter)
    db.commit()
    db.refresh(new_shelter)

    return new_shelter


@router.patch("/{shelter_id}/region")
def update_shelter_region(shelter_id: int, payload: ShelterRegionPatch, actor: User = Depends(require_shelter),
                          db: Session = Depends(get_db)):
    require_shelter_access(actor, shelter_id)
    shelter = db.get(Shelter, shelter_id)
    if shelter is None:
        raise HTTPException(404, "Shelter not found")
    region = selectable_region(db, payload.region_code)
    shelter.region_code = region.region_code
    response = {"shelter_id": shelter_id, "region_code": region.region_code, "region_name": region.region_name}
    db.commit()
    return response


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
