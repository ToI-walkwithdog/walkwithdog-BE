"""Public walk-request inputs never accept ownership, state, or volunteer statistics."""
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import (AwareDatetime, BaseModel, ConfigDict, Field, ValidationInfo, computed_field, field_validator,
                      model_serializer, model_validator)

from app.schemas import DogCardResponse
from app.shelter_volunteer_review_schemas import VolunteerReviewSummaryResponse

RequestStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"]
ReservationTab = Literal["ALL", "PENDING", "APPROVED", "REJECTED", "COMPLETED"]
ReservationStatusGroup = Literal["PAST"]
ReservationDisplayStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED", "COMPLETED"]
CancelReasonCode = Literal["SCHEDULE_CHANGED", "CANNOT_VISIT"]
CancelledActorRole = Literal["USER", "SHELTER", "ADMIN"]


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
    date_slot_id: Annotated[int, Field(strict=True, gt=0, le=2147483647)] | None = None
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
    cancel_reason_code: CancelReasonCode | None = None

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action != "CANCEL" and "cancel_reason_code" in self.model_fields_set:
            raise ValueError("취소 사유는 CANCEL 요청에만 입력할 수 있습니다.")
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
    shelter_name: str | None = None
    dog: DogCardResponse
    requested_start_at: datetime
    requested_end_at: datetime
    status: RequestStatus
    walk_id: int | None = None
    walk_status: Literal["IN_PROGRESS", "COMPLETED", "INTERRUPTED", "CANCELLED"] | None = None
    date_slot_id: int | None = None
    reviewed_by: int | None
    reviewed_at: datetime | None
    rejection_reason: str | None
    rejection_reason_code: str | None = None
    created_at: datetime
    updated_at: datetime
    can_cancel: bool = False

    @model_validator(mode="before")
    @classmethod
    def extract_related_fields(cls, data, info: ValidationInfo):
        if not isinstance(data, dict):
            walk = getattr(data, "walk", None)
            shelter = getattr(data, "shelter", None)
            return {
                "id": data.id,
                "user_id": data.user_id,
                "dog_id": data.dog_id,
                "shelter_id": data.shelter_id,
                "shelter_name": getattr(data, "shelter_name", None) or (shelter.name if shelter is not None else None),
                "dog": data.dog,
                "requested_start_at": data.requested_start_at,
                "requested_end_at": data.requested_end_at,
                "status": data.status,
                "walk_id": walk.id if walk is not None else None,
                "walk_status": walk.status if walk is not None else None,
                "date_slot_id": getattr(data, "date_slot_id", None),
                "reviewed_by": data.reviewed_by,
                "reviewed_at": data.reviewed_at,
                "rejection_reason": data.rejection_reason,
                "rejection_reason_code": getattr(data, "rejection_reason_code", None),
                "created_at": data.created_at,
                "updated_at": data.updated_at,
                "can_cancel": (info.context or {}).get("can_cancel", False),
            }
        return data

    @model_serializer(mode="wrap")
    def preserve_legacy_shape(self, serialize):
        data = serialize(self)
        if self.date_slot_id is None:
            data.pop("date_slot_id", None)
        return data

    @computed_field
    @property
    def display_status(self) -> ReservationDisplayStatus:
        if self.status == "APPROVED" and self.walk_status in ("COMPLETED", "INTERRUPTED"):
            return "COMPLETED"
        return self.status

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


class ManagedWalkRequestResponse(WalkRequestResponse):
    cancel_reason_code: CancelReasonCode | None = None
    cancelled_at: datetime | None = None
    cancelled_by: int | None = None
    cancelled_actor_role: CancelledActorRole | None = None

    @model_validator(mode="before")
    @classmethod
    def extract_cancellation(cls, data, info: ValidationInfo):
        if isinstance(data, dict):
            return data
        fields = super().extract_related_fields(data, info)
        fields.update(cancel_reason_code=data.cancel_reason_code, cancelled_at=data.cancelled_at,
                      cancelled_by=data.cancelled_by, cancelled_actor_role=data.cancelled_actor_role)
        return fields

    @field_validator("cancelled_at")
    @classmethod
    def utc_cancelled_at(cls, value):
        return WalkRequestResponse.utc_datetime(value)


class ApplicantDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nickname: str
    name: str | None = None
    age: int | None = None
    dog_experience: str | None = None
    experienced_breed: str | None = None
    walk_frequency: str | None = None


class ReservationShelter(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ConfirmedVisit(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    address: str | None
    phone: str | None


class ReservationActions(BaseModel):
    can_cancel: bool
    can_enter_shelter_code: bool


class WalkRequestDetailResponse(ManagedWalkRequestResponse):
    applicant: ApplicantDetailResponse
    dog_difficulty: Literal["BEGINNER", "INTERMEDIATE", "EXPERIENCED"] = "BEGINNER"
    shelter: ReservationShelter | None
    max_walk_minutes: int | None
    confirmed_visit: ConfirmedVisit | None
    actions: ReservationActions


class DateWalkRequestReceipt(BaseModel):
    id: int
    dog_id: int
    date_slot_id: int
    shelter_id: int
    requested_start_at: datetime
    requested_end_at: datetime
    status: Literal["PENDING"]
    created_at: datetime


class DateWalkRequestResponse(BaseModel):
    code: Literal["WALK_REQUEST_CREATED"]
    data: DateWalkRequestReceipt
    message: str


class VolunteerProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    level: int = Field(default=1, description="서버에 저장된 봉사자 레벨. 산책 신청, 승인, 시작 자격은 기존 정책을 따릅니다.")
    total_walk_count: int = 0
    total_distance_m: Decimal = Decimal("0")
    total_duration_seconds: int = Field(default=0, description="COMPLETED 산책의 전체 경과 시간(duration_seconds) 합계. 이동 시간 합계가 아닙니다.")
    unique_dog_count: int = 0
    review_summary: VolunteerReviewSummaryResponse = Field(default_factory=VolunteerReviewSummaryResponse)


class MyVolunteerProfileResponse(VolunteerProfileResponse):
    history_walk_count: int
    history_distance_m: Decimal = Field(description="종료 산책 중 확인된 거리의 합계(m).")
    history_unique_dog_count: int
    history_distance_missing_count: int
    history_distance_is_complete: bool
