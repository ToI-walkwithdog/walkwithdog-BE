"""Schemas for F-14 First Walk Guide (SC-03 Home & SC-04 Guide ViewPager)."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

GuideCategory = Literal[
    "HANDLING",
    "PROHIBITED_ACTIONS",
    "WASTE_DISPOSAL",
    "EMERGENCY_REPORT",
]


class GuideSlideItem(BaseModel):
    step: int
    category: GuideCategory
    title: str
    summary: str
    tips: list[str]


class FirstWalkGuideStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    is_completed: bool
    completed_at: datetime | None = None
    should_recommend_guide: bool
    recommendation_message: str | None = None

    @field_validator("completed_at")
    @classmethod
    def utc_datetime(cls, value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class FirstWalkGuideResponse(FirstWalkGuideStatusResponse):
    guide_version: str = "v1"
    title: str
    subtitle: str
    total_slides: int
    slides: list[GuideSlideItem]
