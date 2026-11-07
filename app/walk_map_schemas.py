"""Map and history wire formats.

Owner responses carry the simplified route only: no accuracy, timestamps or point IDs
(that raw data stays behind GET /walks/{walk_id}). Public dog-map responses carry no
volunteer identity, walk IDs or timestamps at all.
"""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.schemas import DogCardResponse
from app.walk_record_schemas import UTCResponse
from app.walk_request_schemas import VolunteerProfileResponse

WalkStatus = Literal["IN_PROGRESS", "COMPLETED", "INTERRUPTED", "CANCELLED"]


class MapPoint(BaseModel):
    latitude: Decimal
    longitude: Decimal


class UserMapWalk(UTCResponse):
    walk_id: int
    dog: DogCardResponse
    started_at: datetime
    ended_at: datetime
    total_distance_m: Decimal | None
    duration_seconds: int | None
    point_count: int  # raw stored points; route may be a simplified subset
    route: list[MapPoint]


class UserMapResponse(BaseModel):
    user_id: int
    total: int
    limit: int
    offset: int
    walks: list[UserMapWalk]


class DogMapStats(BaseModel):
    walk_count: int
    total_distance_m: Decimal
    total_duration_seconds: int
    volunteer_count: int


class DogMapWalk(BaseModel):
    total_distance_m: Decimal | None
    duration_seconds: int | None
    route: list[MapPoint]  # trimmed at both ends, rounded, simplified; may be empty


class DogMapResponse(BaseModel):
    dog_id: int
    stats: DogMapStats
    total: int
    limit: int
    offset: int
    walks: list[DogMapWalk]


class UserProfileResponse(VolunteerProfileResponse):
    """Same statistic fields as GET /volunteers/me, plus the display name."""
    nickname: str


class UserWalkItem(UTCResponse):
    walk_id: int
    walk_request_id: int
    dog: DogCardResponse
    status: WalkStatus
    started_at: datetime
    ended_at: datetime | None
    total_distance_m: Decimal | None
    duration_seconds: int | None
    average_pace: Decimal | None
    counted_as_volunteer_record: bool  # only COMPLETED walks count toward statistics


class UserWalksResponse(BaseModel):
    user_id: int
    total: int
    limit: int
    offset: int
    walks: list[UserWalkItem]
