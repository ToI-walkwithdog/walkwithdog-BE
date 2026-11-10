"""Public walk-request inputs never accept ownership, state, or volunteer statistics."""
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.schemas import DogCardResponse

RequestStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"]


class RejectionReasonCode(str, Enum):
    SCHEDULE_CONFLICT = "SCHEDULE_CONFLICT"
    EXPERIENCE_MISMATCH = "EXPERIENCE_MISMATCH"
    INFO_INSUFFICIENT = "INFO_INSUFFICIENT"
    OTHER = "OTHER"


REJECTION_REASON_TEXTS = {
    RejectionReasonCode.SCHEDULE_CONFLICT: "보호소 일정상 어려움",
    RejectionReasonCode.EXPERIENCE_MISMATCH: "경험 부족",
    RejectionReasonCode.INFO_INSUFFICIENT: "정보 확인 어려움",
}


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
    rejection_reason_code: RejectionReasonCode | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action == "REJECT":
            if self.rejection_reason_code is None:
                if not self.rejection_reason or not self.rejection_reason.strip():
                    raise ValueError("공백이 아닌 거절 사유가 필요합니다.")
                self.rejection_reason_code = RejectionReasonCode.OTHER
                self.rejection_reason = self.rejection_reason.strip()
            elif self.rejection_reason_code == RejectionReasonCode.OTHER:
                if not self.rejection_reason or not self.rejection_reason.strip():
                    raise ValueError("기타 사유 선택 시 상세 거절 사유를 직접 입력해야 합니다.")
                self.rejection_reason = self.rejection_reason.strip()
            else:
                if not self.rejection_reason or not self.rejection_reason.strip():
                    self.rejection_reason = REJECTION_REASON_TEXTS[self.rejection_reason_code]
                else:
                    self.rejection_reason = self.rejection_reason.strip()
        elif self.action in ("APPROVE", "CANCEL"):
            if "rejection_reason" in self.model_fields_set or "rejection_reason_code" in self.model_fields_set:
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
    rejection_reason_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def cancelled_after_approval(self) -> bool:
        return self.status == "CANCELLED" and self.reviewed_by is not None

    @field_validator("requested_start_at", "requested_end_at", "reviewed_at", "created_at", "updated_at")
    @classmethod
    def utc_datetime(cls, value):
        if value is None:
            return None
        # SQLite drops timezone metadata. All writes in this service use UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ApplicantDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nickname: str
    name: str | None = None
    age: int | None = None
    dog_experience: str | None = None
    experienced_breed: str | None = None
    walk_frequency: str | None = None


class WalkRequestDetailResponse(WalkRequestResponse):
    applicant: ApplicantDetailResponse
    dog_difficulty: Literal["BEGINNER", "INTERMEDIATE", "EXPERIENCED"] = "BEGINNER"


class VolunteerProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    level: int = Field(default=1, description="하위 호환용 기존 값. 자동 승급하거나 산책 신청·승인·시작 자격 판단에 사용하지 않습니다.")
    total_walk_count: int = 0
    total_distance_m: Decimal = Decimal("0")
    total_duration_seconds: int = Field(default=0, description="COMPLETED 산책의 전체 경과 시간(duration_seconds) 합계. 이동 시간 합계가 아닙니다.")
    unique_dog_count: int = 0
