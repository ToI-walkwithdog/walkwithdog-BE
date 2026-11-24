"""Walk-setting payloads: omitted PATCH fields differ from explicit nulls."""
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

DogSizeCode = Literal["SMALL", "MEDIUM", "LARGE"]


class DateSlotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_date: date
    start_time: time
    end_time: time
    duration_minutes: Literal[30, 60, 90]

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def time_string(cls, value):
        if not isinstance(value, (str, time)):
            raise ValueError("시간은 HH:MM[:SS] 형식이어야 합니다.")
        return value

    @model_validator(mode="after")
    def validate_times(self):
        if self.start_time.tzinfo is not None or self.end_time.tzinfo is not None:
            raise ValueError("시간대 offset 없이 Asia/Seoul 현지 시간을 입력하세요.")
        if self.start_time >= self.end_time:
            raise ValueError("시작 시간은 종료 시간보다 빨라야 합니다.")
        return self


class DateSlotResponse(DateSlotInput):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dog_id: int
    is_active: bool


class DateSlotPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_date: date | None = None
    start_time: str | None = None
    end_time: str | None = None
    duration_minutes: Literal[30, 60, 90] | None = None

    @model_validator(mode="after")
    def nonempty_patch(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("수정할 날짜 일정 값을 입력하세요.")
        return self


class AvailableDateSlot(BaseModel):
    date_slot_id: int
    starts_at: datetime
    ends_at: datetime


class AvailabilityShelter(BaseModel):
    id: int
    name: str
    region_code: str | None
    region_name: str | None


class AvailableDog(BaseModel):
    dog_id: int
    name: str
    image_url: str | None
    size_code: DogSizeCode | None
    difficulty_score: float | None
    max_walk_minutes: int | None
    shelter: AvailabilityShelter
    date_slots: list[AvailableDateSlot]


class AvailableDogPreview(AvailableDog):
    has_more_date_slots: bool


class AvailableDogDetail(AvailableDog):
    description: str | None
    precautions: str | None


class DogAvailabilityPage(BaseModel):
    items: list[AvailableDogPreview]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


class DogAvailabilityListResponse(BaseModel):
    code: Literal["OK"]
    data: DogAvailabilityPage
    message: str


class DogAvailabilityDetailResponse(BaseModel):
    code: Literal["OK"]
    data: AvailableDogDetail
    message: str


class WalkSlotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_of_week: Annotated[int, Field(strict=True, ge=0, le=6)]
    start_time: time
    end_time: time

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def time_string(cls, value):
        if not isinstance(value, (str, time)):
            raise ValueError("시간은 HH:MM[:SS] 형식이어야 합니다.")
        return value

    @model_validator(mode="after")
    def local_time_order(self):
        if self.start_time.tzinfo is not None or self.end_time.tzinfo is not None:
            raise ValueError("시간대 offset 없이 Asia/Seoul 현지 시간을 입력하세요.")
        if self.start_time >= self.end_time:
            raise ValueError("시작 시간은 종료 시간보다 빨라야 합니다.")
        return self


class WalkSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Defaults match the existing ORM; model_fields_set controls partial updates.
    walkable: StrictBool = False
    difficulty: Literal["BEGINNER", "INTERMEDIATE", "EXPERIENCED"] = "BEGINNER"
    precautions: str | None = None
    recommended_duration_minutes: Annotated[int, Field(strict=True, ge=1, le=2147483647)] | None = None
    size_code: DogSizeCode | None = None
    difficulty_score: Annotated[Decimal, Field(ge=1, le=5, multiple_of=Decimal("0.5"))] | None = None
    max_walk_minutes: Annotated[int, Field(strict=True, ge=1, le=2147483647)] | None = None
    slots: list[WalkSlotInput] = Field(default_factory=list)

    @field_validator("difficulty_score", mode="before")
    @classmethod
    def numeric_difficulty(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float, Decimal))):
            raise ValueError("난이도는 숫자로 입력하세요.")
        return value

    @model_validator(mode="after")
    def nonempty_patch(self):
        if not self.model_fields_set:
            raise ValueError("수정할 필드를 하나 이상 입력하세요.")
        return self


class WalkSlotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    day_of_week: int
    start_time: time
    end_time: time


class WalkSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dog_id: int
    walkable: bool
    difficulty: Literal["BEGINNER", "INTERMEDIATE", "EXPERIENCED"]
    precautions: str | None
    recommended_duration_minutes: int | None
    size_code: DogSizeCode | None = None
    difficulty_score: float | None = None
    max_walk_minutes: int | None = None
    slots: list[WalkSlotResponse]

    @field_validator("slots", mode="after")
    @classmethod
    def chronological_slots(cls, value):
        return sorted(value, key=lambda slot: (slot.day_of_week, slot.start_time, slot.end_time))
