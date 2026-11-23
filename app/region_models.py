"""Region master (Issue #43, migration 022_regions.sql).

Kept out of app.models so the region feature stays one self-contained change.
app.region_service imports it, and app.main imports that through the router, so
Base.metadata knows the table wherever the app or the service is loaded.
"""
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, String

from app.database import Base

LEVEL_SIDO = "SIDO"
LEVEL_SIGUNGU = "SIGUNGU"
LEVEL_SPECIAL_SELECTABLE = "SPECIAL_SELECTABLE"
REGION_LEVELS = (LEVEL_SIDO, LEVEL_SIGUNGU, LEVEL_SPECIAL_SELECTABLE)
SELECTABLE_LEVELS = (LEVEL_SIGUNGU, LEVEL_SPECIAL_SELECTABLE)

# 세종특별자치시 has no 시군구. Its SIDO code is official; the selectable "전체" code is
# an application code that can never look like a numeric 법정동코드 prefix.
SEJONG_SIDO_CODE = "36"
SEJONG_ALL_CODE = "36_SPECIAL_ALL"
SEJONG_ALL_NAME = "세종특별자치시 전체"


class Region(Base):
    __tablename__ = "regions"
    __table_args__ = (
        CheckConstraint("region_level IN ('SIDO', 'SIGUNGU', 'SPECIAL_SELECTABLE')", name="ck_regions_level"),
        CheckConstraint(
            "(region_level = 'SIDO' AND parent_region_code IS NULL AND length(region_code) = 2)"
            " OR (region_level = 'SIGUNGU' AND length(region_code) = 5"
            " AND parent_region_code = substr(region_code, 1, 2))"
            " OR (region_level = 'SPECIAL_SELECTABLE' AND region_code = '36_SPECIAL_ALL'"
            " AND parent_region_code = '36')",
            name="ck_regions_hierarchy"),
        # Regular expressions are PostgreSQL only; SQLite tests rely on the loader's checks.
        CheckConstraint("region_level = 'SPECIAL_SELECTABLE' OR region_code ~ '^[0-9]+$'",
                        name="ck_regions_code_digits").ddl_if(dialect="postgresql"),
        CheckConstraint("trim(region_name) <> ''", name="ck_regions_name_not_blank"),
        Index("ix_regions_parent_active", "parent_region_code", "is_active"),
    )

    region_code = Column(String(20), primary_key=True)
    region_name = Column(String(100), nullable=False)
    region_level = Column(String(20), nullable=False)
    parent_region_code = Column(String(20), ForeignKey("regions.region_code", name="fk_regions_parent_region_code"))
    is_active = Column(Boolean, nullable=False)
