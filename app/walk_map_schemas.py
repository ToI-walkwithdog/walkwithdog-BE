"""Map and history wire formats.

Owner responses carry the simplified route only: no accuracy, timestamps or point IDs
(that raw data stays behind GET /walks/{walk_id}). Public dog-map responses carry no
volunteer identity, walk IDs or timestamps at all.
"""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas import DogCardResponse
from app.walk_record_schemas import UTCResponse, WalkResponse
from app.walk_request_schemas import VolunteerProfileResponse

WalkStatus = Literal["IN_PROGRESS", "COMPLETED", "INTERRUPTED", "CANCELLED"]


class MapPoint(BaseModel):
    latitude: Decimal
    longitude: Decimal


class MotionMetrics(BaseModel):
    moving_duration_seconds: int | None = Field(default=None, description="GPS로 확인된 이동 초; 레거시 미분석 기록은 null")
    stopped_duration_seconds: int | None = Field(default=None, description="GPS로 확정된 정지 초; 레거시 미분석 기록은 null")
    unknown_duration_seconds: int | None = Field(default=None, description="GPS 근거 부족·공백·미확정 구간 초; 레거시 미분석 기록은 null")
    moving_distance_m: Decimal | None = Field(default=None, description="확인된 이동 구간의 거리(m); 레거시 미분석 기록은 null")
    average_pace: Decimal | None = Field(default=None, description=WalkResponse.model_fields["average_pace"].description)


class UserMapWalk(UTCResponse, MotionMetrics):
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
    route: list[MapPoint] = Field(description="시작·끝 절단 및 반올림한 공개 경로. 짧은 경로, 보호 반경 재진입 또는 연결선 안전성 미확인 시 빈 배열입니다.")


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


class UserWalkItem(UTCResponse, MotionMetrics):
    walk_id: int
    walk_request_id: int
    dog: DogCardResponse
    status: WalkStatus
    started_at: datetime
    ended_at: datetime | None
    total_distance_m: Decimal | None
    duration_seconds: int | None
    counted_as_volunteer_record: bool  # only COMPLETED walks count toward statistics


class UserWalksResponse(BaseModel):
    user_id: int
    total: int
    limit: int
    offset: int
    walks: list[UserWalkItem]
