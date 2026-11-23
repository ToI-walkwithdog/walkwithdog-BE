"""Region list reads and the shared region code check (Issue #43).

The two list functions back GET /regions/sido and GET /regions/sigungu. They only read.

check_region_codes() / require_selectable_regions() are for the walk routine (#44) and
the current-location lookup: only an active SIGUNGU or the active 세종특별자치시 전체
code (SPECIAL_SELECTABLE) under an active SIDO can be chosen. They take a Session and
never commit, so they run inside the caller's transaction; pass lock=True there to keep
the rows from being deactivated by a concurrent sync until the caller commits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from sqlalchemy.orm import Session

from app.api_contract import ContractError
from app.region_models import LEVEL_SIDO, SELECTABLE_LEVELS, Region

SIDO_CODE_PATTERN = re.compile(r"[0-9]{2}")
REGION_CODE_MAX_LENGTH = 20

REGION_DATA_UNAVAILABLE_MESSAGE = "지역 목록을 불러올 수 없습니다."
REGION_NOT_FOUND_MESSAGE = "선택한 지역을 찾을 수 없습니다."

# Reasons a code cannot be chosen (check_region_codes).
INVALID_FORMAT = "INVALID_FORMAT"
DUPLICATE = "DUPLICATE"
NOT_FOUND = "NOT_FOUND"
INACTIVE = "INACTIVE"
NOT_SELECTABLE_LEVEL = "NOT_SELECTABLE_LEVEL"   # a SIDO code
PARENT_INACTIVE = "PARENT_INACTIVE"


def region_data_unavailable() -> ContractError:
    return ContractError(503, "REGION_DATA_UNAVAILABLE", REGION_DATA_UNAVAILABLE_MESSAGE)


def _order(query):
    # Provisional policy (Issue #43 open item): official code order. It is the same on
    # SQLite and PostgreSQL, unlike Korean name collation.
    return query.order_by(Region.region_code)


def list_sido(db: Session) -> list[dict]:
    rows = _order(db.query(Region).filter(Region.region_level == LEVEL_SIDO, Region.is_active.is_(True))).all()
    if not rows:  # empty or broken master: never an empty list
        raise region_data_unavailable()
    return [{"region_code": r.region_code, "region_name": r.region_name} for r in rows]


def list_sigungu(db: Session, sido_code: str) -> list[dict]:
    """Selectable children of an active SIDO. The caller has checked the code format."""
    sido = db.get(Region, sido_code)
    if sido is None or sido.region_level != LEVEL_SIDO or not sido.is_active:
        has_master = db.query(Region.region_code).filter(
            Region.region_level == LEVEL_SIDO, Region.is_active.is_(True)).first() is not None
        if not has_master:  # nothing loaded: same answer as the SIDO list, not "not found"
            raise region_data_unavailable()
        raise ContractError(404, "REGION_NOT_FOUND", REGION_NOT_FOUND_MESSAGE)
    rows = _order(db.query(Region).filter(Region.parent_region_code == sido.region_code,
                                          Region.region_level.in_(SELECTABLE_LEVELS),
                                          Region.is_active.is_(True))).all()
    if not rows:
        # The loader never leaves an active SIDO without a choice, so this is a broken
        # master, not a normal empty answer.
        raise region_data_unavailable()
    return [{"region_code": r.region_code, "region_name": r.region_name, "sido_code": r.parent_region_code}
            for r in rows]


@dataclass
class RegionCheck:
    regions: list[Region] = field(default_factory=list)       # selectable, in input order
    problems: dict[str, str] = field(default_factory=dict)    # code -> reason (first reason only)

    @property
    def ok(self) -> bool:
        return not self.problems


class InvalidRegionCodes(ValueError):
    """At least one code cannot be chosen. `problems` maps each code to a reason constant."""

    def __init__(self, problems: dict[str, str]):
        super().__init__(", ".join(f"{code}:{reason}" for code, reason in problems.items()))
        self.problems = problems


def check_region_codes(db: Session, codes, *, lock: bool = False) -> RegionCheck:
    """Check codes for selection without raising. Counts (1 to 3) belong to the caller."""
    result = RegionCheck()
    wanted = []
    for code in codes:
        if not isinstance(code, str) or not code or len(code) > REGION_CODE_MAX_LENGTH:
            # A non-string value gets a typed key so it never collides with a real code.
            key = code if isinstance(code, str) else f"{type(code).__name__}:{code!r}"
            result.problems.setdefault(key, INVALID_FORMAT)
        elif code in wanted:
            result.problems.setdefault(code, DUPLICATE)
        else:
            wanted.append(code)
    if not wanted:
        return result
    query = db.query(Region).filter(Region.region_code.in_(wanted)).populate_existing()
    if lock and db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(read=True)
    found = {r.region_code: r for r in query.all()}
    parents = {r.parent_region_code for r in found.values() if r.parent_region_code}
    parent_rows = {}
    if parents:
        parent_query = db.query(Region).filter(Region.region_code.in_(parents)).populate_existing()
        if lock and db.get_bind().dialect.name == "postgresql":
            parent_query = parent_query.with_for_update(read=True)
        parent_rows = {r.region_code: r for r in parent_query.all()}
    for code in wanted:
        row = found.get(code)
        if row is None:
            result.problems.setdefault(code, NOT_FOUND)
        elif row.region_level not in SELECTABLE_LEVELS:
            result.problems.setdefault(code, NOT_SELECTABLE_LEVEL)
        elif not row.is_active:
            result.problems.setdefault(code, INACTIVE)
        elif not getattr(parent_rows.get(row.parent_region_code), "is_active", False):
            result.problems.setdefault(code, PARENT_INACTIVE)
        else:
            result.regions.append(row)
    return result


def require_selectable_regions(db: Session, codes, *, lock: bool = False) -> list[Region]:
    """Selectable regions in input order, or InvalidRegionCodes. #44 maps it to 422 INVALID_REGION_CODE."""
    result = check_region_codes(db, codes, lock=lock)
    if not result.ok:
        raise InvalidRegionCodes(result.problems)
    return result.regions


def require_selectable_region(db: Session, code: str, *, lock: bool = False) -> Region:
    return require_selectable_regions(db, [code], lock=lock)[0]
