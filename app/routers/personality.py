from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Dog
from app.personality import personality_data

router = APIRouter(prefix="/dogs", tags=["personality"])


@router.get("/{dog_id}/personality")
def get_personality(dog_id: int, db: Session = Depends(get_db)):
    dog = db.get(Dog, dog_id)
    if dog is None:
        raise HTTPException(404, "Dog not found")
    return personality_data(dog)
