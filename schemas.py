from pydantic import BaseModel
from typing import Optional


class UserCreate(BaseModel):
    email: str
    nickname: str
    password: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    email: str
    nickname: str

    class Config:
        from_attributes = True

class ShelterCreate(BaseModel):
    name: str
    phone: str | None = None
    address: str | None = None
    website_url: str | None = None


class ShelterResponse(ShelterCreate):
    id: int

    class Config:
        from_attributes = True
    
class DogCreate(BaseModel):
    desertion_no: str | None = None
    shelter_id: int | None = None
    name: str
    breed: str | None = None
    age: int | None = None
    gender: str | None = None
    image_url: str | None = None
    description: str | None = None
    personality_keywords: str | None = None
    energy_level: int | None = None
    barking_level: int | None = None
    independence_level: int | None = None
    sociability_level: int | None = None
    walk_need: int | None = None

    good_with_children: str | None = None
    good_with_other_dogs: str | None = None
    separation_anxiety: str | None = None


class DogResponse(DogCreate):
    desertion_no: str | None = None
    id: int

    class Config:
        from_attributes = True

class SurveyCreate(BaseModel):
    user_id: int
    housing_type: str | None = None
    family_type: str | None = None
    outing_frequency: str | None = None
    daily_walk_time: str | None = None
    life_pattern: str | None = None
    walk_frequency: str | None = None
    walk_duration: str | None = None
    dog_experience: str | None = None
    mbti: str | None = None


class SurveyResponse(SurveyCreate):
    id: int

    class Config:
        from_attributes = True

class MatchingCreate(BaseModel):
    user_id: int
    dog_id: int


class MatchingResponse(BaseModel):
    id: int
    user_id: int
    dog_id: int
    score: int
    reason: str | None = None
    detail_analysis: str | None = None

    class Config:
        from_attributes = True

class DogSimpleResponse(BaseModel):
    desertion_no: str | None = None
    id: int
    name: str
    breed: str | None = None
    age: int | None = None
    gender: str | None = None
    image_url: str | None = None
    description: str | None = None
    personality_keywords: str | None = None

    class Config:
        from_attributes = True


class MatchingDetailResponse(BaseModel):
    id: int
    user_id: int
    dog_id: int
    score: int
    reason: str | None = None
    detail_analysis: str | None = None
    dog: DogSimpleResponse

    class Config:
        from_attributes = True

class ShelterSimpleResponse(BaseModel):
    id: int
    name: str
    phone: str | None = None
    address: str | None = None
    website_url: str | None = None

    class Config:
        from_attributes = True


class DogDetailResponse(BaseModel):
    desertion_no: str | None = None
    id: int
    shelter_id: int | None = None
    name: str
    breed: str | None = None
    age: int | None = None
    gender: str | None = None
    image_url: str | None = None
    description: str | None = None
    personality_keywords: str | None = None
    shelter: ShelterSimpleResponse | None = None

    class Config:
        from_attributes = True