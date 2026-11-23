"""Schemas for F-15 Shelter-to-Volunteer Review (SC-16, SC-09, SC-10)."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VolunteerReviewTagCode = Literal[
    "PUNCTUALITY",
    "SAFETY_CARE",
    "CONSIDERATE",
    "DISAPPOINTING",
]

VOLUNTEER_REVIEW_TAG_CODES: tuple[VolunteerReviewTagCode, ...] = (
    "PUNCTUALITY",
    "SAFETY_CARE",
    "CONSIDERATE",
    "DISAPPOINTING",
)


def empty_tag_counts() -> dict[VolunteerReviewTagCode, int]:
    return {code: 0 for code in VOLUNTEER_REVIEW_TAG_CODES}


class ShelterVolunteerReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Decimal
    tags: list[VolunteerReviewTagCode] = Field(default_factory=list)
    comment: str | None = Field(default=None, max_length=500)

    @field_validator("rating")
    @classmethod
    def validate_half_step_rating(cls, value: Decimal) -> Decimal:
        if value.is_nan() or value.is_infinite():
            raise ValueError("rating must be a finite number")
        if value < Decimal("0.5") or value > Decimal("5.0"):
            raise ValueError("rating must be between 0.5 and 5.0")
        scaled = value * Decimal("10")
        if scaled != int(scaled) or int(scaled) % 5 != 0:
            raise ValueError("rating must be in 0.5 increments")
        return value.quantize(Decimal("0.1"))

    @field_validator("tags")
    @classmethod
    def validate_unique_tags(cls, value: list[VolunteerReviewTagCode]) -> list[VolunteerReviewTagCode]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate tags are not allowed")
        return value

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > 500:
            raise ValueError("comment must be at most 500 characters")
        return stripped


class ShelterVolunteerReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    walk_id: int
    shelter_id: int
    shelter_name: str | None = None
    volunteer_user_id: int
    reviewed_by: int
    rating: Decimal
    tags: list[VolunteerReviewTagCode] = Field(default_factory=list)
    comment: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class VolunteerReviewSummaryResponse(BaseModel):
    total_review_count: int = 0
    average_rating: Decimal | None = None
    tag_counts: dict[VolunteerReviewTagCode, int] = Field(default_factory=empty_tag_counts)


class VolunteerReviewsResponse(BaseModel):
    user_id: int
    summary: VolunteerReviewSummaryResponse
    total: int
    limit: int
    offset: int
    reviews: list[ShelterVolunteerReviewResponse]
