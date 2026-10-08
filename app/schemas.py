from pydantic import BaseModel, Field, field_validator, EmailStr
from typing import Optional
from app.models import UserRole
from app.survey_codes import (
    HousingType, FamilyType, OutingFrequency, LifePattern,
    WalkFrequency, WalkDuration, DogExperience, MBTI, parse_walk_hours, SURVEY_FIELDS,
)


class UserCreate(BaseModel):
    email: EmailStr = Field(max_length=100)
    nickname: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=15, max_length=128)

    @field_validator("nickname")
    @classmethod
    def nonempty_nickname(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("닉네임이 필요합니다.")
        return value


class LoginRequest(BaseModel):
    email: EmailStr = Field(max_length=100)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: int
    email: str
    nickname: str
    role: UserRole = UserRole.USER
    shelter_id: int | None = None

    class Config:
        from_attributes = True


class UserShelterUpdate(BaseModel):
    # Explicit null clears the assignment; an omitted field must not clear it.
    shelter_id: int | None = Field(..., gt=0)

    model_config = {"extra": "forbid"}


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
    weight: str | None = None

    image_url: str | None = None

    description: str | None = None
    special_note: str | None = None
    personality_keywords: str | None = None

    energy_level: int | None = None
    barking_level: int | None = None
    independence_level: int | None = None
    sociability_level: int | None = None
    walk_need: int | None = None
    separation_anxiety_level: int | None = None
    training_level: int | None = None

    good_with_children: str | None = None
    good_with_other_dogs: str | None = None
    separation_anxiety: str | None = None

    trait_source: str | None = "breed_default"


class DogResponse(DogCreate):
    id: int

    class Config:
        from_attributes = True


class DogSimpleResponse(BaseModel):
    desertion_no: str | None = None
    id: int
    name: str
    breed: str | None = None
    age: int | None = None
    gender: str | None = None
    weight: str | None = None
    image_url: str | None = None

    description: str | None = None
    special_note: str | None = None
    personality_keywords: str | None = None

    energy_level: int | None = None
    independence_level: int | None = None
    sociability_level: int | None = None
    separation_anxiety_level: int | None = None
    training_level: int | None = None

    trait_source: str | None = None

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
    weight: str | None = None

    image_url: str | None = None

    description: str | None = None
    special_note: str | None = None
    personality_keywords: str | None = None

    energy_level: int | None = None
    barking_level: int | None = None
    independence_level: int | None = None
    sociability_level: int | None = None
    walk_need: int | None = None
    separation_anxiety_level: int | None = None
    training_level: int | None = None

    good_with_children: str | None = None
    good_with_other_dogs: str | None = None
    separation_anxiety: str | None = None

    trait_source: str | None = None
    process_state: str | None = None
    neuter_status: str | None = None
    shelter: ShelterSimpleResponse | None = None

    class Config:
        from_attributes = True


class SurveyCreate(BaseModel):
    user_id: int = Field(gt=0)
    housing_type: HousingType | None = None
    family_type: FamilyType | None = None
    outing_frequency: OutingFrequency | None = None
    daily_walk_time: float | None = Field(default=None, ge=0, le=24, description="하루 산책 가능 시간(시간 단위)")
    life_pattern: LifePattern | None = None
    walk_frequency: WalkFrequency | None = None
    walk_duration: WalkDuration | None = None
    dog_experience: DogExperience | None = None
    mbti: MBTI | None = None

    @field_validator("daily_walk_time", mode="before")
    @classmethod
    def validate_walk_time(cls, value):
        return parse_walk_hours(value)

    @field_validator("mbti", mode="before")
    @classmethod
    def normalize_mbti(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class SurveyResponse(SurveyCreate):
    id: int
    # Old versions accepted arbitrary strings. Preserve unknown historical
    # answers on reads, while new SurveyCreate requests remain strict.
    housing_type: HousingType | str | None = None
    family_type: FamilyType | str | None = None
    outing_frequency: OutingFrequency | str | None = None
    life_pattern: LifePattern | str | None = None
    walk_frequency: WalkFrequency | str | None = None
    walk_duration: WalkDuration | str | None = None
    dog_experience: DogExperience | str | None = None
    daily_walk_time: float | str | None = None
    mbti: MBTI | str | None = None

    @field_validator(*SURVEY_FIELDS, mode="before")
    @classmethod
    def normalize_legacy_option(cls, value, info):
        if value is None:
            return None
        try:
            return SURVEY_FIELDS[info.field_name](value)
        except ValueError:
            return value

    @field_validator("daily_walk_time", mode="before")
    @classmethod
    def validate_walk_time(cls, value):
        try:
            return parse_walk_hours(value)
        except ValueError:
            return value

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


class MatchingDetailResponse(BaseModel):
    id: int
    user_id: int
    dog_id: int
    score: int
    reason: str | None = None
    detail_analysis: str | None = None
    survey_id: int | None = None
    label: str
    warnings: list[str]
    care_points: list[str]
    score_breakdown: dict[str, int]
    dog: DogDetailResponse

    class Config:
        from_attributes = True


class BreedTraitCreate(BaseModel):
    breed_name: str

    energy_level: int | None = None
    barking_level: int | None = None
    independence_level: int | None = None
    sociability_level: int | None = None
    walk_need: int | None = None

    good_with_children: str | None = None
    good_with_other_dogs: str | None = None
    separation_anxiety: str | None = None


class BreedTraitResponse(BreedTraitCreate):
    id: int

    class Config:
        from_attributes = True


class DogTraitUpdate(BaseModel):
    name: str | None = None
    breed: str | None = None
    age: int | None = None
    gender: str | None = None
    weight: str | None = None

    special_note: str | None = None
    description: str | None = None

class DogCardResponse(BaseModel):
    id: int
    name: str
    breed: str | None = None
    age: int | None = None
    gender: str | None = None
    image_url: str | None = None
    personality_keywords: str | None = None

    class Config:
        from_attributes = True

class MatchingScoreRequest(BaseModel):
    user_id: int
    dog_id: int


class MatchingScoreResponse(BaseModel):
    score: int
    label: str
    reason: str
    warnings: list[str]
    care_points: list[str]
    score_breakdown: dict[str, int]
