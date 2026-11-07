"""Raw observations only: score direction is a product decision, not a conversion."""
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.walk_record_schemas import UTCResponse

ReviewScore = Annotated[int, Field(strict=True, ge=1, le=5)]


class WalkReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leash_pulling: ReviewScore | None = None
    handling_difficulty: ReviewScore | None = None
    sociability_with_people: ReviewScore | None = None
    sociability_with_dogs: ReviewScore | None = None
    barking_level: ReviewScore | None = None
    memo: str | None = None

    @model_validator(mode="after")
    def require_observation(self):
        if not any(value is not None for name, value in self.model_dump().items() if name != "memo"):
            raise ValueError("관찰한 평가 점수가 최소 하나 필요합니다. 메모만 등록할 수 없습니다.")
        return self


class WalkReviewResponse(UTCResponse):
    id: int
    walk_id: int
    leash_pulling: ReviewScore | None
    handling_difficulty: ReviewScore | None
    sociability_with_people: ReviewScore | None
    sociability_with_dogs: ReviewScore | None
    barking_level: ReviewScore | None
    memo: str | None
    created_at: datetime
    updated_at: datetime
