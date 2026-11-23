"""Build and update the region master from the official 법정동코드 file (Issue #43).

Source: 행정표준코드관리시스템 "법정동코드 전체자료" (https://www.code.go.kr/stdcode/regCodeL.do),
downloaded by POST https://www.code.go.kr/etc/codeFullDown.do with codeseId=00002. The
zip holds one CP949 tab-separated text file with the header 법정동코드, 법정동명, 폐지여부
and every code that ever existed (폐지여부 is 존재 or 폐지). No API key is needed.

Conversion (checked against the 2026-11-23 snapshot, see docs/issues/43/HANDOFF.md):
- 10-digit code SS GGG DDD RR. A SIDO row is SS00000000, a 시군구-level row SSGGG00000
  (GGG != 000). 읍면동 and 리 rows are not used.
- SIDO: region_code = SS, region_name = the official name.
- SIGUNGU: region_code = SSGGG, parent = SS, region_name = the official name without the
  SIDO name ("종로구", "수원시 영통구").
- A city that has 구 (수원시 41110 with 41111 장안구 ... 41117 영통구) is an intermediate
  level, not a choice: its 구 are the SIGUNGU rows and the city row is left out. A row
  is a 구 of a city only when both the code (same first four digits, fifth digit not
  0 under a fifth digit 0) and the name (the city name plus a space) say so; the code
  alone is wrong for 43740 영동군 and 43745 증평군.
- 세종특별자치시 has no SS00000000 row. Its only 시군구-level row 3611000000 carries the
  SIDO name, so SIDO 36 is built from it and the application code 36_SPECIAL_ALL
  ("세종특별자치시 전체") is its only selectable child.

Update rules (sync): new codes are inserted, changed names are updated, codes the file
marks 폐지 (or that became an intermediate city) are deactivated and kept. A code that
is in the master but absent from the file stops the run: the file is incomplete, not
proof that the region was abolished. Large deactivations also stop unless allowed.
Nothing is written before every check passes; the caller owns the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import math
from pathlib import Path
import zipfile

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.region_models import (LEVEL_SIDO, LEVEL_SIGUNGU, LEVEL_SPECIAL_SELECTABLE, SEJONG_ALL_CODE,
                               SEJONG_ALL_NAME, SEJONG_SIDO_CODE, SELECTABLE_LEVELS, Region)

SOURCE_PAGE_URL = "https://www.code.go.kr/stdcode/regCodeL.do"
DOWNLOAD_URL = "https://www.code.go.kr/etc/codeFullDown.do"
DOWNLOAD_FORM = {"codeseId": "00002"}  # 법정동코드
SOURCE_ENCODING = "cp949"
SOURCE_HEADER = ("법정동코드", "법정동명", "폐지여부")
STATUS_LIVE = "존재"
STATUS_ABOLISHED = "폐지"

# SIDO without 시군구: SIDO code -> the 시군구-level code that carries the SIDO itself.
SINGLE_TIER_SIDO = {SEJONG_SIDO_CODE: "36110"}

# Guards against a truncated or wrong file. They are sanity floors, not policy: the
# 2026-11-23 file has 16 SIDO and 255 selectable 시군구.
MIN_ACTIVE_SIDO = 10
MIN_SELECTABLE_SIGUNGU = 150
# A run that would deactivate more than this share of active rows stops unless allowed.
MAX_DEACTIVATE_RATIO = 0.10

REASON_ABOLISHED = "ABOLISHED"
REASON_INTERMEDIATE = "INTERMEDIATE_CITY"
REASON_SINGLE_TIER = "SINGLE_TIER_SIDO"


class SnapshotError(ValueError):
    """The input is unusable or incomplete; nothing was written."""


class SyncRefused(RuntimeError):
    """The change set looks unsafe (large deactivation, changed code meaning); nothing was written."""


@dataclass(frozen=True)
class SourceRow:
    code: str
    name: str
    live: bool


@dataclass
class Snapshot:
    rows: list[SourceRow]
    metadata: dict


@dataclass(frozen=True)
class RegionRecord:
    code: str
    name: str
    level: str
    parent: str | None
    active: bool = True


@dataclass
class Master:
    records: dict[str, RegionRecord]          # desired active rows
    inactive_reasons: dict[str, str]          # codes in the file that must not be active, with the reason
    intermediate_cities: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        levels = [r.level for r in self.records.values()]
        return {"active_sido": levels.count(LEVEL_SIDO), "selectable_sigungu": levels.count(LEVEL_SIGUNGU),
                "special_selectable": levels.count(LEVEL_SPECIAL_SELECTABLE),
                "intermediate_cities": self.intermediate_cities}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_snapshot(path: str | Path) -> Snapshot:
    """Read the official zip (or the text file inside it) and validate every row."""
    path = Path(path)
    raw = path.read_bytes()
    metadata = {"file": path.name, "file_sha256": sha256(raw)}
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) != 1 or not members[0].filename.lower().endswith(".txt"):
                raise SnapshotError("zip에는 법정동코드 텍스트 파일 하나만 있어야 합니다.")
            text_bytes = archive.read(members[0])
            metadata["zip_member_time"] = datetime(*members[0].date_time).isoformat()
    else:
        text_bytes = raw
    metadata["text_sha256"] = sha256(text_bytes)
    try:
        text = text_bytes.decode(SOURCE_ENCODING)
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"{SOURCE_ENCODING}로 읽을 수 없습니다: {exc.start}바이트") from None
    rows = parse_rows(text.splitlines())
    metadata.update(rows=len(rows), live_rows=sum(r.live for r in rows))
    return Snapshot(rows, metadata)


def parse_rows(lines) -> list[SourceRow]:
    lines = [line for line in lines if line.strip()]
    if not lines or tuple(part.strip() for part in lines[0].split("\t")) != SOURCE_HEADER:
        raise SnapshotError("머리글이 법정동코드, 법정동명, 폐지여부가 아닙니다.")
    rows, seen = [], set()
    for number, line in enumerate(lines[1:], start=2):
        parts = line.split("\t")
        if len(parts) != 3:
            raise SnapshotError(f"{number}행: 열이 3개가 아닙니다.")
        code, name, status = parts[0].strip(), " ".join(parts[1].split()), parts[2].strip()
        if len(code) != 10 or not code.isdigit():
            raise SnapshotError(f"{number}행: 법정동코드가 10자리 숫자가 아닙니다.")
        if status not in (STATUS_LIVE, STATUS_ABOLISHED):
            raise SnapshotError(f"{number}행: 폐지여부 값을 알 수 없습니다.")
        if not name:
            raise SnapshotError(f"{number}행: 법정동명이 비어 있습니다.")
        if code in seen:
            raise SnapshotError(f"{number}행: 법정동코드 {code}가 중복됩니다.")
        seen.add(code)
        rows.append(SourceRow(code, name, status == STATUS_LIVE))
    return rows


def _is_sido_row(code: str) -> bool:
    return code[2:] == "00000000"


def _is_sigungu_row(code: str) -> bool:
    return code[2:5] != "000" and code[5:] == "00000"


def build_master(rows: list[SourceRow], *, min_active_sido: int = MIN_ACTIVE_SIDO,
                 min_selectable_sigungu: int = MIN_SELECTABLE_SIGUNGU) -> Master:
    """Convert official rows to the desired master. Raises SnapshotError on anything unexpected."""
    sido_rows = {r.code[:2]: r for r in rows if _is_sido_row(r.code)}
    sigungu_rows = {r.code[:5]: r for r in rows if _is_sigungu_row(r.code)}
    records: dict[str, RegionRecord] = {}
    inactive: dict[str, str] = {}

    for code, row in sido_rows.items():
        if row.live:
            records[code] = RegionRecord(code, row.name, LEVEL_SIDO, None)
        else:
            inactive[code] = REASON_ABOLISHED
    for sido_code, carrier in SINGLE_TIER_SIDO.items():
        row = sigungu_rows.get(carrier)
        if row is None:
            continue
        inactive[carrier] = REASON_SINGLE_TIER  # never a choice; the special code replaces it
        others = [c for c, r in sigungu_rows.items() if c[:2] == sido_code and c != carrier and r.live]
        if others:
            raise SnapshotError(f"{sido_code}에 예상하지 않은 시군구가 있습니다: {', '.join(sorted(others))}")
        if sido_code in records:
            continue
        if row.live:
            if " " in row.name:
                raise SnapshotError(f"{carrier}의 이름이 시도 이름 형식이 아닙니다.")
            records[sido_code] = RegionRecord(sido_code, row.name, LEVEL_SIDO, None)
            inactive.pop(sido_code, None)
        else:
            inactive.setdefault(sido_code, REASON_ABOLISHED)

    live_sigungu = {c: r for c, r in sigungu_rows.items() if r.live and c not in SINGLE_TIER_SIDO.values()}
    intermediates = []
    for code, row in sorted(live_sigungu.items()):
        if code[4] != "0":
            continue
        children = sorted(c for c, r in live_sigungu.items()
                          if c[:4] == code[:4] and c[4] != "0" and r.name.startswith(row.name + " "))
        if children:
            inactive[code] = REASON_INTERMEDIATE
            intermediates.append({"code": code, "name": row.name, "children": children})
    parents_of = {child: item["code"] for item in intermediates for child in item["children"]}

    for code, row in sigungu_rows.items():
        if code in SINGLE_TIER_SIDO.values() or code in inactive:
            continue
        if not row.live:
            inactive[code] = REASON_ABOLISHED
            continue
        sido = records.get(code[:2])
        if sido is None or sido.level != LEVEL_SIDO:
            raise SnapshotError(f"{code}({row.name})의 상위 시도 {code[:2]}가 현존하지 않습니다.")
        if not row.name.startswith(sido.name + " "):
            raise SnapshotError(f"{code}의 이름이 상위 시도 이름으로 시작하지 않습니다.")
        local = row.name[len(sido.name) + 1:]
        if " " in local and code not in parents_of:
            raise SnapshotError(f"{code}({row.name})의 중간 계층 상위 시를 찾을 수 없습니다.")
        records[code] = RegionRecord(code, local, LEVEL_SIGUNGU, code[:2])

    if SEJONG_SIDO_CODE in records:
        records[SEJONG_ALL_CODE] = RegionRecord(SEJONG_ALL_CODE, SEJONG_ALL_NAME, LEVEL_SPECIAL_SELECTABLE,
                                                SEJONG_SIDO_CODE)
    else:
        inactive[SEJONG_ALL_CODE] = REASON_ABOLISHED

    _check_master(records, min_active_sido, min_selectable_sigungu)
    return Master(records, inactive, intermediates)


def _check_master(records, min_active_sido, min_selectable_sigungu):
    sidos = [r for r in records.values() if r.level == LEVEL_SIDO]
    children = [r for r in records.values() if r.level in SELECTABLE_LEVELS]
    if len(sidos) < min_active_sido:
        raise SnapshotError(f"현존 시도가 {len(sidos)}개뿐입니다. 전체자료인지 확인해 주세요.")
    if sum(r.level == LEVEL_SIGUNGU for r in children) < min_selectable_sigungu:
        raise SnapshotError("선택 가능한 시군구 수가 너무 적습니다. 전체자료인지 확인해 주세요.")
    empty = sorted(s.code for s in sidos if not any(c.parent == s.code for c in children))
    if empty:
        raise SnapshotError(f"선택 가능한 하위 지역이 없는 시도가 있습니다: {', '.join(empty)}")
    names = {}
    for child in children:
        key = (child.parent, child.name)
        if key in names:
            raise SnapshotError(f"같은 시도 안에 이름이 같은 지역이 있습니다: {names[key]}, {child.code}")
        names[key] = child.code


@dataclass
class Changes:
    inserted: list[RegionRecord] = field(default_factory=list)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)      # code, before, after
    reactivated: list[str] = field(default_factory=list)
    deactivated: list[tuple[str, str, str]] = field(default_factory=list)  # code, name, reason

    def count(self) -> int:
        return len(self.inserted) + len(self.renamed) + len(self.reactivated) + len(self.deactivated)

    def report(self) -> dict:
        return {
            "counts": {"inserted": len(self.inserted), "renamed": len(self.renamed),
                       "reactivated": len(self.reactivated), "deactivated": len(self.deactivated)},
            "inserted": [{"code": r.code, "name": r.name, "level": r.level} for r in self.inserted],
            "renamed": [{"code": c, "before": b, "after": a} for c, b, a in self.renamed],
            "reactivated": self.reactivated,
            "deactivated": [{"code": c, "name": n, "reason": why} for c, n, why in self.deactivated],
        }


def plan_changes(existing: list[Region], master: Master, *, allow_large_change: bool = False) -> Changes:
    current = {row.region_code: row for row in existing}
    changes = Changes()
    for code, record in master.records.items():
        row = current.get(code)
        if row is None:
            changes.inserted.append(record)
            continue
        if row.region_level != record.level or row.parent_region_code != record.parent:
            raise SyncRefused(f"{code}의 수준이나 상위 지역이 바뀌었습니다. 수동 검토가 필요합니다.")
        if row.region_name != record.name:
            changes.renamed.append((code, row.region_name, record.name))
        if not row.is_active:
            changes.reactivated.append(code)
    missing = []
    for code, row in current.items():
        if code in master.records:
            continue
        reason = master.inactive_reasons.get(code)
        if reason is None:
            missing.append(code)
        elif row.is_active:
            changes.deactivated.append((code, row.region_name, reason))
    if missing:
        shown = ", ".join(sorted(missing)[:10])
        raise SnapshotError(f"마스터에 있는 코드 {len(missing)}개가 입력 자료에 없습니다({shown}). "
                            "폐지가 아니라 자료 누락일 수 있어 중단합니다.")
    active = sum(row.is_active for row in current.values())
    limit = max(3, math.ceil(active * MAX_DEACTIVATE_RATIO))
    if len(changes.deactivated) > limit and not allow_large_change:
        raise SyncRefused(f"비활성화 대상이 {len(changes.deactivated)}개로 기준 {limit}개를 넘습니다. "
                          "자료를 확인한 뒤 명시적으로 허용해야 합니다.")
    return changes


def apply_changes(db: Session, changes: Changes) -> None:
    """Write the change set in the caller's transaction, then re-check the whole table."""
    order = {LEVEL_SIDO: 0, LEVEL_SIGUNGU: 1, LEVEL_SPECIAL_SELECTABLE: 1}
    for record in sorted(changes.inserted, key=lambda r: (order[r.level], r.code)):
        db.add(Region(region_code=record.code, region_name=record.name, region_level=record.level,
                      parent_region_code=record.parent, is_active=record.active))
        db.flush()  # parents before children for the self-referencing FK
    for code, _, after in changes.renamed:
        db.get(Region, code).region_name = after
    for code in changes.reactivated:
        db.get(Region, code).is_active = True
    for code, _, _ in changes.deactivated:
        db.get(Region, code).is_active = False
    db.flush()
    problems = master_problems(db)
    if problems:
        raise SnapshotError("적용 후 마스터 검증 실패: " + "; ".join(problems))


def master_problems(db: Session) -> list[str]:
    """Invariants a usable master keeps; also used by the API to detect a broken master."""
    rows = db.query(Region).all()
    by_code = {r.region_code: r for r in rows}
    problems = []
    active_sidos = [r for r in rows if r.is_active and r.region_level == LEVEL_SIDO]
    if not active_sidos:
        problems.append("활성 시도가 없습니다")
    for row in rows:
        if row.is_active and row.region_level in SELECTABLE_LEVELS:
            parent = by_code.get(row.parent_region_code)
            if parent is None or not parent.is_active:
                problems.append(f"{row.region_code}의 상위 시도가 비활성입니다")
    for sido in active_sidos:
        if not any(r.is_active and r.parent_region_code == sido.region_code and r.region_level in SELECTABLE_LEVELS
                   for r in rows):
            problems.append(f"{sido.region_code}에 선택 가능한 하위 지역이 없습니다")
    return problems


def sync_regions(db: Session, snapshot: Snapshot, *, apply: bool, allow_large_change: bool = False,
                 min_active_sido: int = MIN_ACTIVE_SIDO,
                 min_selectable_sigungu: int = MIN_SELECTABLE_SIGUNGU) -> dict:
    """Plan (and with apply=True write) one snapshot. The caller commits or rolls back."""
    master = build_master(snapshot.rows, min_active_sido=min_active_sido,
                          min_selectable_sigungu=min_selectable_sigungu)
    if db.get_bind().dialect.name == "postgresql":
        # One sync at a time until commit; API readers are not blocked.
        db.execute(text("LOCK TABLE regions IN SHARE ROW EXCLUSIVE MODE"))
    existing = db.query(Region).populate_existing().all()
    changes = plan_changes(existing, master, allow_large_change=allow_large_change)
    if apply:
        apply_changes(db, changes)
    return {"source": snapshot.metadata, "master": master.summary(), "applied": apply,
            "checked_at": datetime.now(timezone.utc).isoformat(), **changes.report()}


def download_snapshot(directory: str | Path, *, timeout: float = 120.0) -> Path:
    """Download the current official zip into directory (no key). Not used by tests."""
    import requests  # imported here so the API never needs it

    response = requests.post(DOWNLOAD_URL, data=DOWNLOAD_FORM, timeout=timeout,
                             headers={"Referer": SOURCE_PAGE_URL, "User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    if not response.content.startswith(b"PK"):
        raise SnapshotError("공식 다운로드 응답이 zip 파일이 아닙니다.")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"legal_dong_code_full_{stamp}.zip"
    target.write_bytes(response.content)
    return target
