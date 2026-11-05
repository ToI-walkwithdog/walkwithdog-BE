"""Public walk-request inputs never accept ownership, state, or volunteer statistics."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas import DogCardResponse

RequestStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"]


class WalkRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dog_id: Annotated[int, Field(strict=True, gt=0, le=2147483647)]
    requested_start_at: AwareDatetime
    requested_end_at: AwareDatetime

    @field_validator("requested_start_at", "requested_end_at", mode="before")
    @classmethod
    def iso_datetime(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("timezone offset을 포함한 ISO 8601 일시가 필요합니다.")
        if isinstance(value, str):
            # Pydantic also accepts numeric strings as Unix timestamps by default.
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("timezone offset을 포함한 ISO 8601 일시가 필요합니다.") from None
        return value

    @model_validator(mode="after")
    def chronological(self):
        if self.requested_end_at <= self.requested_start_at:
            raise ValueError("종료 시각은 시작 시각보다 늦어야 합니다.")
        return self


class WalkRequestAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["APPROVE", "REJECT", "CANCEL"]
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def reason_for_rejection_only(self):
        if self.action == "REJECT":
            if not self.rejection_reason or not self.rejection_reason.strip():
                raise ValueError("공백이 아닌 거절 사유가 필요합니다.")
            self.rejection_reason = self.rejection_reason.strip()
        elif "rejection_reason" in self.model_fields_set:
            raise ValueError("거절 사유는 REJECT 작업에만 입력할 수 있습니다.")
        return self


class WalkRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    dog_id: int
    shelter_id: int
    dog: DogCardResponse
    requested_start_at: datetime
    requested_end_at: datetime
    status: RequestStatus
    reviewed_by: int | None
    reviewed_at: datetime | None
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("requested_start_at", "requested_end_at", "reviewed_at", "created_at", "updated_at")
    @classmethod
    def utc_datetime(cls, value):
        if value is None:
            return None
        # SQLite drops timezone metadata. All writes in this service use UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class VolunteerProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    level: int = 1
    total_walk_count: int = 0
    total_distance_m: Decimal = Decimal("0")
    total_duration_seconds: int = 0
    unique_dog_count: int = 0
