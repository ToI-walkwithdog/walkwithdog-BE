from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.walk_request_schemas import WalkRequestResponse


class LiveWalkUTCModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator(
        "started_at", "scheduled_end_at", "last_location_at",
        "occurred_at", "recorded_at",
        check_fields=False,
    )
    @classmethod
    def utc_time(cls, value):
        return WalkRequestResponse.utc_datetime(value)


class LiveWalkCardResponse(LiveWalkUTCModel):
    walk_id: int
    dog_id: int
    dog_name: str
    dog_image_url: str | None = None
    walker_nickname: str
    started_at: datetime
    elapsed_seconds: int
    scheduled_duration_minutes: int
    scheduled_end_at: datetime
    last_latitude: Decimal | None = None
    last_longitude: Decimal | None = None
    last_location_at: datetime | None = None
    is_stale: bool


class LiveWalkMarkerResponse(LiveWalkUTCModel):
    marker_type: Literal["START", "INCIDENT", "BEHAVIOR"]
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    occurred_at: datetime
    incident_type: str | None = None
    behavior_type: str | None = None
    description: str | None = None


class LiveWalkPointResponse(LiveWalkUTCModel):
    sequence_no: int
    latitude: Decimal
    longitude: Decimal
    recorded_at: datetime


class LiveWalkDetailResponse(LiveWalkCardResponse):
    route: list[LiveWalkPointResponse]
    markers: list[LiveWalkMarkerResponse]
    walker_phone: str | None = None


class LiveWalkUpdatesResponse(LiveWalkUTCModel):
    walk_id: int
    is_stale: bool
    last_location_at: datetime | None = None
    last_latitude: Decimal | None = None
    last_longitude: Decimal | None = None
    route: list[LiveWalkPointResponse]
    markers: list[LiveWalkMarkerResponse]
