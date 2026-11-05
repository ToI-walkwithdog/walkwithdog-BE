"""GPS and event wire formats; ownership, server times and summaries are not inputs."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

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


class WalkMarkerCreate(PositionEvent):
    marker_type: Literal["SNIFF", "POOP", "DOG_ENCOUNTER", "PERSON_ENCOUNTER", "BARKING"]
    memo: str | None = None


class WalkIncidentCreate(Coordinates):
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    incident_type: Literal["INJURY", "LOST_DOG", "SAFETY_THREAT", "OTHER"]
    description: str
    occurred_at: AwareDatetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def iso_time(cls, value):
        return WalkRequestCreate.iso_datetime(value)

    @model_validator(mode="after")
    def location_pair_and_description(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("위도와 경도는 함께 입력해야 합니다.")
        self.description = self.description.strip()
        if not self.description:
            raise ValueError("신고 내용이 필요합니다.")
        return self


class WalkEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["COMPLETED", "INTERRUPTED"] = "COMPLETED"


class UTCResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator("started_at", "ended_at", "created_at", "updated_at", "recorded_at", "occurred_at", check_fields=False)
    @classmethod
    def utc_time(cls, value):
        return WalkRequestResponse.utc_datetime(value)


class WalkResponse(UTCResponse):
    id: int
    walk_request_id: int
    status: Literal["IN_PROGRESS", "COMPLETED", "INTERRUPTED", "CANCELLED"]
    started_at: datetime
    ended_at: datetime | None
    total_distance_m: Decimal | None
    duration_seconds: int | None
    average_pace: Decimal | None
    created_at: datetime
    updated_at: datetime


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


class PointsReceipt(BaseModel):
    walk_id: int
    accepted_count: int
    duplicate_count: int
    last_sequence_no: int


class WalkDetailResponse(BaseModel):
    walk: WalkResponse
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
