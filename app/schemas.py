from datetime import datetime

from pydantic import AliasChoices, BaseModel, Field, field_validator
from typing import ClassVar, Literal, Optional
from app.account_rules import (REQUIRED_MESSAGES, normalize_login_id, normalize_login_identifier,
                               normalize_nickname, require_login_password, validate_password)
from app.privacy_policy import INVALID_PRIVACY_PAYLOAD_MESSAGE, PrivacyInput, parse_privacy
from app.pledge_service import PLEDGE_VERSION_REQUIRED_MESSAGE, normalize_pledge_version
from app.api_contract import as_field_error
from app.models import UserRole
from app.walk_schemas import WalkSettingsResponse
from app.survey_codes import (
    HousingType, FamilyType, OutingFrequency, LifePattern,
    WalkFrequency, WalkDuration, DogExperience, MBTI, parse_walk_hours, SURVEY_FIELDS,
)


class SignupRequest(BaseModel):
    """Account fields and the privacy confirmation (Issue #31).

    Email is not collected. Unknown fields, including a leftover `email`, are ignored.
    """
    login_id: str = Field(description="영문 대소문자, 숫자 4~20자. 앞뒤 공백 제거 후 소문자로 저장",
                          examples=["walker04"])
    nickname: str = Field(description="한글, 영문, 숫자, 밑줄(_) 3~20자. 앞뒤 공백 제거, NFC 정규화",
                          examples=["산책왕"])
    password: str = Field(description="15~128자, 영문 대문자, 소문자, 숫자, 특수문자 각 1개 이상. 변형 없이 해시",
                          examples=["WalkTogether2026!"])
    privacy: PrivacyInput = Field(
        description="개인정보 확인값. document_version은 서버 현재 버전과 같아야 하고 age_14_plus_confirmed는 true. "
                    "동의 필드는 JSON boolean이며 서버에서 활성화한 항목 중 true인 것만 기록",
        examples=[{"document_version": "privacy-v1", "collection_use_agreed": True, "age_14_plus_confirmed": True}])

    # A missing field never reaches the validators; ContractRoute uses this for privacy.
    contract_required_errors: ClassVar[dict] = {"privacy": ("INVALID_PRIVACY_PAYLOAD", INVALID_PRIVACY_PAYLOAD_MESSAGE)}

    @field_validator("login_id", mode="before")
    @classmethod
    def check_login_id(cls, value):
        return as_field_error(normalize_login_id, value)

    @field_validator("nickname", mode="before")
    @classmethod
    def check_nickname(cls, value):
        return as_field_error(normalize_nickname, value)

    @field_validator("password", mode="before")
    @classmethod
    def check_password(cls, value):
        return as_field_error(validate_password, value)

    @field_validator("privacy", mode="before")
    @classmethod
    def check_privacy(cls, value):
        return as_field_error(parse_privacy, value)


class SignupUserData(BaseModel):
    id: int
    login_id: str
    nickname: str
    role: UserRole
    shelter_id: int | None = None


class SignupResponse(BaseModel):
    code: Literal["OK"] = "OK"
    data: SignupUserData
    message: str


class AvailabilityData(BaseModel):
    available: bool


class AvailabilityResponse(BaseModel):
    code: Literal["OK"] = "OK"
    data: AvailabilityData
    message: str


class LoginRequest(BaseModel):
    """Login ID only; email login was removed with Issue #32."""
    identifier: str = Field(description="로그인 아이디. 앞뒤 공백 제거 후 영문 소문자로 비교, 최대 100자",
                            examples=["walker04"])
    password: str = Field(description="비밀번호. 로그인에서는 가입용 복잡도 규칙을 적용하지 않음",
                          examples=["WalkTogether2026!"])

    # A missing field never reaches the validators below; ContractRoute uses these.
    contract_required_errors: ClassVar[dict] = {
        field: (code, REQUIRED_MESSAGES[code])
        for field, code in (("identifier", "IDENTIFIER_REQUIRED"), ("password", "PASSWORD_REQUIRED"))
    }

    @field_validator("identifier", mode="before")
    @classmethod
    def check_identifier(cls, value):
        return as_field_error(normalize_login_identifier, value)

    @field_validator("password", mode="before")
    @classmethod
    def check_password(cls, value):
        return as_field_error(require_login_password, value)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LoginResponse(BaseModel):
    code: Literal["OK"] = "OK"
    data: TokenResponse
    message: str


class EmptyContractResponse(BaseModel):
    code: Literal["OK"] = "OK"
    data: dict = Field(default_factory=dict, examples=[{}])
    message: str


class UserResponse(BaseModel):
    id: int
    login_id: str
    nickname: str
    role: UserRole = UserRole.USER
    shelter_id: int | None = None

    class Config:
        from_attributes = True


class MeData(UserResponse):
    """GET /users/me (#45). Basic fields plus onboarding progress computed from stored records.

    General users always get booleans for the step fields and onboarding_completed. Shelter and
    admin accounts get null for step1/2/3_completed, onboarding_completed and next_onboarding_step
    (not onboarding targets; null is not a home-entry signal for them) and keep the pledge fields.
    """
    account_status: Literal["ACTIVE", "AGE_REVIEW_REQUIRED"]
    step1_completed: bool | None = Field(description="volunteer_onboarding_basics 기록 존재")
    step2_completed: bool | None = Field(description="volunteer_onboarding_experiences 기록 존재(경험 없음 포함)")
    step3_completed: bool | None = Field(description="volunteer_onboarding_routines 기록 존재")
    pledge_completed: bool = Field(description="서버 현재 서약 버전의 확인 기록 존재")
    current_pledge_version: str
    pledge_acknowledged_at: str | None = Field(description="현재 버전 서약 확인 시각(UTC ISO 8601), 없으면 null")
    onboarding_completed: bool | None = Field(description="ACTIVE이고 현재 서약과 Step 1~3이 모두 완료")
    next_onboarding_step: Literal["AGE_REVIEW", "PLEDGE", "BASIC_INFORMATION", "PET_EXPERIENCE",
                                  "WALKING_ROUTINE"] | None


class MeResponse(BaseModel):
    """GET /users/me."""
    code: Literal["OK"] = "OK"
    data: MeData
    message: str


class PledgeRequest(BaseModel):
    """PUT /users/me/pledge (Issue #33). No user ID, time or agreement flag is accepted."""
    pledge_version: str = Field(description="화면에 표시한 서약 문서 버전. 서버 현재 버전과 같아야 함, 최대 32자",
                                examples=["pledge-v1"])

    # A missing field or unreadable body never reaches the validator; ContractRoute uses this.
    contract_required_errors: ClassVar[dict] = {
        "pledge_version": ("PLEDGE_VERSION_REQUIRED", PLEDGE_VERSION_REQUIRED_MESSAGE)}

    @field_validator("pledge_version", mode="before")
    @classmethod
    def check_pledge_version(cls, value):
        return as_field_error(normalize_pledge_version, value)


class PledgeData(BaseModel):
    pledge_completed: bool
    pledge_version: str
    acknowledged_at: datetime


class PledgeResponse(BaseModel):
    code: Literal["OK"] = "OK"
    data: PledgeData
    message: str


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
    desertion_no: str | None = Field(default=None, max_length=20)
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
    walk_settings: WalkSettingsResponse | None = Field(
        default=None, validation_alias=AliasChoices("walk_settings", "walk_setting")
    )

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
