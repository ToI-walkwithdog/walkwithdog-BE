"""Walk-setting payloads: omitted PATCH fields differ from explicit nulls."""
from datetime import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


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
    slots: list[WalkSlotInput] = Field(default_factory=list)

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
    slots: list[WalkSlotResponse]

    @field_validator("slots", mode="after")
    @classmethod
    def chronological_slots(cls, value):
        return sorted(value, key=lambda slot: (slot.day_of_week, slot.start_time, slot.end_time))
