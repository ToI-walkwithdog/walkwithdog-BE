"""GPS and event wire formats; ownership, server times and summaries are not inputs."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.walk_request_schemas import WalkRequestCreate, WalkRequestResponse

PositiveID = Annotated[int, Field(strict=True, gt=0, le=2147483647)]
Latitude = Annotated[Decimal, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[Decimal, Field(ge=-180, le=180, allow_inf_nan=False)]


class WalkStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    walk_request_id: PositiveID


class Coordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: Latitude
    longitude: Longitude

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def numeric_coordinate(cls, value):
        if isinstance(value, bool):
            raise ValueError("좌표에는 유한한 숫자가 필요합니다.")
        return value

    @field_validator("latitude", "longitude")
    @classmethod
    def coordinate_precision(cls, value):
        return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP) if value is not None else None


class PositionEvent(Coordinates):
    recorded_at: AwareDatetime

    @field_validator("recorded_at", mode="before")
    @classmethod
    def iso_time(cls, value):
        return WalkRequestCreate.iso_datetime(value)


class WalkPointInput(PositionEvent):
    sequence_no: Annotated[int, Field(strict=True, ge=0, le=2147483647)]
    accuracy_m: Annotated[Decimal, Field(ge=0, le=Decimal("99999999.99"), allow_inf_nan=False)] | None = None

    @field_validator("accuracy_m", mode="before")
    @classmethod
    def numeric_accuracy(cls, value):
        if isinstance(value, bool):
            raise ValueError("정확도에는 유한한 숫자가 필요합니다.")
        return value

    @field_validator("accuracy_m")
    @classmethod
    def accuracy_precision(cls, value):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if value is not None else None


class WalkPointsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: list[WalkPointInput] = Field(min_length=1, max_length=500)


MARKER_MEMO_MAX_LENGTH = 100
INCIDENT_DESCRIPTION_MAX_LENGTH = 1000


class WalkMarkerCreate(Coordinates):
    """행동마커. 좌표는 선택(없거나 GPS가 약하면 서버가 마지막 위치를 쓴다)."""
    marker_type: Literal[
        "POOP", "CROSSWALK_WAIT", "SNIFF", "OTHER",
        "DOG_ENCOUNTER", "PERSON_ENCOUNTER", "BARKING",
    ]
    memo: str | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    accuracy_m: Annotated[Decimal, Field(ge=0, le=Decimal("99999999.99"), allow_inf_nan=False)] | None = None
    recorded_at: AwareDatetime
    # Issue #89: optional retry key (one UUID per real tap) and the time the position was observed.
    client_marker_id: UUID | None = None
    position_recorded_at: AwareDatetime | None = None

    @field_validator("recorded_at", "position_recorded_at", mode="before")
    @classmethod
    def iso_time(cls, value):
        return None if value is None else WalkRequestCreate.iso_datetime(value)

    @field_validator("accuracy_m", mode="before")
    @classmethod
    def numeric_accuracy(cls, value):
        if isinstance(value, bool):
            raise ValueError("정확도에는 유한한 숫자가 필요합니다.")
        return value

    @field_validator("accuracy_m")
    @classmethod
    def accuracy_precision(cls, value):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if value is not None else None

    @model_validator(mode="after")
    def location_pair_and_memo(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("위도와 경도는 함께 입력해야 합니다.")
        memo = self.memo.strip() if self.memo is not None else None
        if len(memo or "") > MARKER_MEMO_MAX_LENGTH:
            raise ValueError(f"내용은 {MARKER_MEMO_MAX_LENGTH}자 이하로 입력해야 합니다.")
        if self.marker_type == "OTHER" and not memo:
            raise ValueError("기타를 선택한 경우 내용을 입력해야 합니다.")
        self.memo = memo or None
        return self


class WalkIncidentCreate(Coordinates):
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    incident_type: Literal[
        "INJURY", "LOST_DOG", "SAFETY_THREAT", "OTHER",
        "POOP_NO_BAG", "ATE_FOREIGN_OBJECT", "ABNORMAL_BEHAVIOR",
    ]
    description: str | None = None
    occurred_at: AwareDatetime
    client_report_id: str | None = Field(None, max_length=36)
    confirm_duplicate: bool = False

    @field_validator("occurred_at", mode="before")
    @classmethod
    def iso_time(cls, value):
        return WalkRequestCreate.iso_datetime(value)

    @model_validator(mode="after")
    def location_pair_and_description(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("위도와 경도는 함께 입력해야 합니다.")
        if self.description is not None:
            self.description = self.description.strip()
            if not self.description:
                self.description = None
        # Issue #89: OTHER needs the user's own text; nothing is cut or replaced with a label.
        if self.description is not None and len(self.description) > INCIDENT_DESCRIPTION_MAX_LENGTH:
            raise ValueError(f"신고 내용은 {INCIDENT_DESCRIPTION_MAX_LENGTH}자 이하로 입력해야 합니다.")
        if self.incident_type == "OTHER" and self.description is None:
            raise ValueError("기타 신고는 보호소에 전달할 내용을 입력해야 합니다.")
        return self


class WalkEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["COMPLETED", "INTERRUPTED"] = "COMPLETED"


HandoverResult = Literal["COMPLETE", "ISSUE"]
HandoverReceiptStatus = Literal["SELF_REPORTED", "ISSUE_REPORTED"]
HandoverStatus = Literal["NOT_REQUIRED", "NOT_CONFIRMED", HandoverReceiptStatus]


class WalkHandoverCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handover_result: HandoverResult
    dog_returned: StrictBool
    equipment_returned: StrictBool
    condition_shared: StrictBool
    acknowledge_auto_timeout: StrictBool = False
    issue_description: str | None = None

    @model_validator(mode="after")
    def validate_checklist(self):
        all_checked = self.dog_returned and self.equipment_returned and self.condition_shared
        description = (self.issue_description or "").strip()
        if self.handover_result == "COMPLETE":
            if not all_checked or description:
                raise ValueError("정상 인계는 세 항목을 모두 확인하고 문제 설명을 비워야 합니다.")
        elif all_checked or not 1 <= len(description) <= 500 or "\x00" in description:
            raise ValueError("인계 문제는 미완료 항목과 1~500자의 설명이 필요합니다.")
        self.issue_description = description or None
        return self


class UTCResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator(
        "started_at", "ended_at", "expected_end_at", "created_at", "updated_at",
        "recorded_at", "occurred_at", "last_point_recorded_at", "last_point_received_at", "auto_end_at",
        "self_confirmed_at", "issue_reported_at",
        check_fields=False,
    )
    @classmethod
    def utc_time(cls, value):
        return WalkRequestResponse.utc_datetime(value)


class WalkHandoverDetails(UTCResponse):
    handover_result: HandoverResult
    dog_returned: bool
    equipment_returned: bool
    condition_shared: bool
    issue_description: str | None
    self_confirmed_at: datetime | None
    issue_reported_at: datetime | None


class WalkHandoverReceipt(WalkHandoverDetails):
    walk_id: int
    handover_status: HandoverReceiptStatus


class WalkResponse(UTCResponse):
    id: int
    walk_request_id: int
    status: Literal["IN_PROGRESS", "COMPLETED", "INTERRUPTED", "CANCELLED"]
    started_at: datetime
    ended_at: datetime | None
    total_distance_m: Decimal | None
    duration_seconds: int | None
    average_pace: Decimal | None = Field(description="초/km. 신규 분석은 moving_duration_seconds × 1000 / moving_distance_m. 새 시간 필드가 NULL인 기존 기록은 전체 경과 시간 기준의 레거시 값입니다.")
    moving_duration_seconds: int | None = Field(default=None, description="GPS로 이동이 확인된 정수 초; 기존 미분석 기록은 NULL")
    stopped_duration_seconds: int | None = Field(default=None, description="60초 이상 안정 정지로 확정된 정수 초; 기존 미분석 기록은 NULL")
    unknown_duration_seconds: int | None = Field(default=None, description="GPS 근거 부족·공백·미확정 및 초 미만 절삭 잔여 시간; 기존 미분석 기록은 NULL")
    moving_distance_m: Decimal | None = Field(default=None, description="실제 이동으로 분류된 구간의 거리(m). 정지 흔들림은 제외하며 기존 미분석 기록은 NULL")
    last_latitude: Decimal | None = None
    last_longitude: Decimal | None = None
    last_accuracy_m: Decimal | None = None
    last_point_recorded_at: datetime | None = None
    last_point_received_at: datetime | None = None
    end_reason: Literal["USER", "STAFF", "AUTO_TIMEOUT"] | None = None
    planned_duration_minutes: int | None = None
    expected_end_at: datetime | None = None
    is_overdue: bool = False
    overdue_minutes: int = 0
    created_at: datetime
    updated_at: datetime


class ActiveWalkResponse(UTCResponse):
    walk_id: int
    walk_request_id: int
    started_at: datetime
    planned_duration_minutes: int | None = None
    expected_end_at: datetime | None = None
    is_overdue: bool = False
    overdue_minutes: int = 0
    last_latitude: Decimal | None = None
    last_longitude: Decimal | None = None
    last_accuracy_m: Decimal | None = None
    last_point_recorded_at: datetime | None = None
    last_point_received_at: datetime | None = None  # Issue #89: server receipt time of the last new point
    auto_end_at: datetime  # Issue #89: no-signal auto end happens strictly after this time
    last_sequence_no: int | None = None
    elapsed_seconds: int


class WalkPointResponse(UTCResponse):
    id: int
    walk_id: int
    latitude: Decimal
    longitude: Decimal
    recorded_at: datetime
    accuracy_m: Decimal | None
    sequence_no: int


class WalkMarkerResponse(UTCResponse):
    id: int
    walk_id: int
    marker_type: str
    latitude: Decimal
    longitude: Decimal
    recorded_at: datetime
    memo: str | None
    location_source: str


class WalkIncidentResponse(UTCResponse):
    id: int
    walk_id: int
    reported_by: int
    incident_type: str
    description: str
    latitude: Decimal | None
    longitude: Decimal | None
    occurred_at: datetime
    status: str
    created_at: datetime
    updated_at: datetime
    client_report_id: str | None = None
    acknowledged_at: datetime | None = None


class PointsReceipt(BaseModel):
    walk_id: int
    accepted_count: int
    duplicate_count: int
    last_sequence_no: int


class ShelterIncidentDog(BaseModel):
    id: int
    name: str


class ShelterIncidentItem(UTCResponse):
    id: int
    walk_id: int
    incident_type: str
    description: str
    dog: ShelterIncidentDog
    latitude: Decimal | None
    longitude: Decimal | None
    occurred_at: datetime
    created_at: datetime
    reporter_nickname: str
    shelter_phone: str | None
    acknowledged_at: datetime | None


class WalkDetailResponse(BaseModel):
    walk: WalkResponse
    handover_status: HandoverStatus
    handover: WalkHandoverDetails | None
    user_id: int
    dog_id: int
    shelter_id: int
    points: list[WalkPointResponse]
    markers: list[WalkMarkerResponse]
    incidents: list[WalkIncidentResponse]
    point_count: int
    marker_count: int
    incident_count: int
    limit: int
    offset: int
