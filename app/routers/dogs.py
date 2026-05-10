from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/dogs", tags=["dogs"])

class DogCreate(BaseModel):
    name: str
    breed: str
    age: int
    sex: str
    weight: float
    activity_level: int
    friendliness: int
    anxiety: int
    
dogs = [
    {
        "id": 1,
        "name": "초코",
        "breed": "진돗개",
        "age": 3,
        "sex": "male",
        "weight": 25.5,
        "activity_level": 4,
        "friendliness": 5,
        "anxiety": 2
    }
]

@router.get("/")
def get_dogs():
    return dogs

@router.get("/{dog_id}")
def get_dog(dog_id: int): 
    for dog in dogs:
        if dog["id"] == dog_id:
            return dog
    raise HTTPException(status_code=404, detail="Dog not found")    

@router.post("/")
def create_dog(dog: DogCreate):
    new_dog = dog.dict()
    new_dog["id"] = len(dogs) + 1
    dogs.append(new_dog)
    return new_dog