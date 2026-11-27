from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.walk_record_schemas import HandoverStatus, UTCResponse, WalkIncidentResponse, WalkMarkerResponse
from app.walk_request_schemas import WalkRequestResponse

VALID_CONDITION_TAGS = {"ENERGETIC", "CALM", "EXCITED", "TENSE", "TIRED", "SCARED", "UNWELL"}


class WalkReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Annotated[int, Field(ge=1, le=5, description="별점 (1~5)")]
    condition_tags: list[str] = Field(default_factory=list, description="컨디션 태그 목록 (중복 불가)")
    comment: Annotated[str | None, Field(default=None, max_length=100, description="한 줄 코멘트 (최대 100자)")]

    @field_validator("condition_tags")
    @classmethod
    def validate_tags(cls, tags: list[str]) -> list[str]:
        if len(tags) != len(set(tags)):
            raise ValueError("컨디션 태그는 중복될 수 없습니다.")
        for tag in tags:
            if tag not in VALID_CONDITION_TAGS:
                raise ValueError(f"유효하지 않은 컨디션 태그입니다: {tag}")
        return tags


class WalkReviewResponse(UTCResponse):
    id: int
    walk_id: int
    rating: int | None = None
    condition_tags: list[str] = Field(default_factory=list)
    comment: str | None = None
    created_at: datetime
    updated_at: datetime


class WalkPrivateMemoWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memo: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)]
    expected_version: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("memo")
    @classmethod
    def validate_storable_text(cls, memo: str) -> str:
        if "\x00" in memo:
            raise ValueError("메모에 저장할 수 없는 문자가 있습니다.")
        return memo


class WalkPrivateMemoResponse(UTCResponse):
    memo: str
    version: int
    created_at: datetime
    updated_at: datetime


class WalkPrivateMemoReceipt(WalkPrivateMemoResponse):
    walk_id: int


class SummaryWalkInfo(UTCResponse):
    id: int
    status: str
    started_at: datetime
    ended_at: datetime | None = None


class SummaryMetrics(BaseModel):
    duration_seconds: int | None = None
    total_distance_m: Decimal | None = None
    marker_count: int


class SummaryPoint(BaseModel):
    latitude: Decimal
    longitude: Decimal


class WalkLevelUpEvent(UTCResponse):
    event_id: int = Field(validation_alias="id")
    before_level: int
    after_level: int
    policy_version: str
    earned_at: datetime

    @field_validator("earned_at")
    @classmethod
    def utc_earned_at(cls, value):
        return WalkRequestResponse.utc_datetime(value)


class WalkSummaryResponse(BaseModel):
    walk: SummaryWalkInfo
    handover_status: HandoverStatus
    private_memo: WalkPrivateMemoResponse | None = None
    level_evaluation_status: Literal["NOT_ELIGIBLE", "PENDING", "COMPLETED", "FAILED"]
    level_up_event: WalkLevelUpEvent | None = None
    summary: SummaryMetrics
    points: list[SummaryPoint]
    points_total: int
    points_returned: int
    is_downsampled: bool
    markers: list[WalkMarkerResponse]
    incidents: list[WalkIncidentResponse]
    shelter_link: str | None = None
    review: WalkReviewResponse | None = None
