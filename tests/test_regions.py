"""Region master conversion, sync, selection check and list APIs (Issue #43) on SQLite.

Two kinds of data:
- FIXTURE rows below are synthetic test rows shaped like the official file (real code
  formats and names, a handful of regions). They exercise the rules one by one.
- REAL_SNAPSHOT is the official 법정동코드 전체자료 zip kept in data/regions; the real
  conversion tests read it as downloaded.
"""
import dataclasses
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import Base, get_db
    from app.region_master import (REASON_ABOLISHED, REASON_INTERMEDIATE, REASON_SINGLE_TIER, SOURCE_HEADER,
                                   Snapshot, SnapshotError, SyncRefused, build_master, parse_rows, read_snapshot,
                                   sync_regions)
    from app.region_models import SEJONG_ALL_CODE, Region
    from app import region_service
    from app.region_service import (DUPLICATE, INACTIVE, INVALID_FORMAT, NOT_FOUND, NOT_SELECTABLE_LEVEL,
                                    PARENT_INACTIVE, InvalidRegionCodes, check_region_codes,
                                    require_selectable_region, require_selectable_regions)
    from app.routers import regions
    from scripts import sync_regions as sync_script

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
REAL_SNAPSHOT = ROOT / "data" / "regions" / "legal_dong_code_full_20261123.zip"
REAL_METADATA = ROOT / "data" / "regions" / "legal_dong_code_full_20261123.json"
LIVE, GONE = "존재", "폐지"

# Synthetic fixture (not the national master).
FIXTURE = [
    ("1100000000", "서울특별시", LIVE),
    ("1111000000", "서울특별시 종로구", LIVE),
    ("1111010100", "서울특별시 종로구 청운동", LIVE),   # 동 level: ignored
    ("1114000000", "서울특별시 중구", LIVE),
    ("2600000000", "부산광역시", LIVE),
    ("2611000000", "부산광역시 중구", LIVE),           # same name as 11140, other code
    ("3611000000", "세종특별자치시", LIVE),             # no 3600000000 row, as in the real file
    ("3611010100", "세종특별자치시 반곡동", LIVE),
    ("4100000000", "경기도", LIVE),
    ("4111000000", "경기도 수원시", LIVE),               # city with 구: intermediate level
    ("4111100000", "경기도 수원시 장안구", LIVE),
    ("4111700000", "경기도 수원시 영통구 ", LIVE),       # trailing space as in the real file
    ("4115000000", "경기도 의정부시", LIVE),             # city without 구
    ("4200000000", "강원도", GONE),
    ("4211000000", "강원도 춘천시", GONE),
    ("4300000000", "충청북도", LIVE),
    ("4374000000", "충청북도 영동군", LIVE),             # same first four digits, not a parent
    ("4374500000", "충청북도 증평군", LIVE),
    ("5100000000", "강원특별자치도", LIVE),
    ("5111000000", "강원특별자치도 춘천시", LIVE),
]
SMALL = {"min_active_sido": 1, "min_selectable_sigungu": 1}


def snapshot(rows, header=SOURCE_HEADER):
    return Snapshot(parse_rows(["\t".join(header)] + ["\t".join(r) for r in rows]), {"file": "fixture"})


def replace(rows, code, name=None, status=None):
    return [(c, name if c == code and name else n, status if c == code and status else s) for c, n, s in rows]


def sqlite_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    return engine


class ConversionTests(unittest.TestCase):
    def test_fixture_rules(self):
        master = build_master(snapshot(FIXTURE).rows, **SMALL)
        records = {code: (r.name, r.level, r.parent) for code, r in master.records.items()}
        self.assertEqual(records, {
            "11": ("서울특별시", "SIDO", None), "11110": ("종로구", "SIGUNGU", "11"), "11140": ("중구", "SIGUNGU", "11"),
            "26": ("부산광역시", "SIDO", None), "26110": ("중구", "SIGUNGU", "26"),
            "36": ("세종특별자치시", "SIDO", None),
            SEJONG_ALL_CODE: ("세종특별자치시 전체", "SPECIAL_SELECTABLE", "36"),
            "41": ("경기도", "SIDO", None), "41111": ("수원시 장안구", "SIGUNGU", "41"),
            "41117": ("수원시 영통구", "SIGUNGU", "41"), "41150": ("의정부시", "SIGUNGU", "41"),
            "43": ("충청북도", "SIDO", None), "43740": ("영동군", "SIGUNGU", "43"),
            "43745": ("증평군", "SIGUNGU", "43"),
            "51": ("강원특별자치도", "SIDO", None), "51110": ("춘천시", "SIGUNGU", "51")})
        self.assertEqual(master.inactive_reasons, {"41110": REASON_INTERMEDIATE, "36110": REASON_SINGLE_TIER,
                                                   "42": REASON_ABOLISHED, "42110": REASON_ABOLISHED})
        self.assertEqual(master.intermediate_cities,
                         [{"code": "41110", "name": "경기도 수원시", "children": ["41111", "41117"]}])

    def test_real_official_snapshot(self):
        """The committed zip is the official file, unchanged; the whole country converts."""
        meta = json.loads(REAL_METADATA.read_text(encoding="utf-8"))
        snap = read_snapshot(REAL_SNAPSHOT)
        self.assertEqual((snap.metadata["file_sha256"], snap.metadata["text_sha256"]),
                         (meta["zip_sha256"], meta["text_sha256"]))
        self.assertEqual((snap.metadata["rows"], snap.metadata["live_rows"]), (meta["rows"], meta["live_rows"]))
        master = build_master(snap.rows)
        summary = master.summary()
        self.assertEqual((summary["active_sido"], summary["selectable_sigungu"], summary["special_selectable"]),
                         (16, 255, 1))
        self.assertEqual(len(summary["intermediate_cities"]), 13)
        r = master.records
        self.assertEqual((r["11110"].name, r["11110"].parent), ("종로구", "11"))
        self.assertEqual((r["41117"].name, r["41117"].parent), ("수원시 영통구", "41"))
        self.assertEqual(r["51"].name, "강원특별자치도")
        self.assertEqual(r["36"].name, "세종특별자치시")
        self.assertEqual(r[SEJONG_ALL_CODE].parent, "36")
        self.assertEqual(r["12"].name, "전남광주통합특별시")
        self.assertEqual((r["43740"].name, r["43745"].name), ("영동군", "증평군"))
        for code in ("41110", "41590", "36110", "29", "29110", "42", "46"):
            with self.subTest(code=code):
                self.assertNotIn(code, r)
                self.assertIn(code, master.inactive_reasons)
        self.assertEqual(master.inactive_reasons["41590"], REASON_INTERMEDIATE)  # 화성시 now has 구
        children = [x for x in r.values() if x.level == "SIGUNGU"]
        self.assertTrue(all(len(x.code) == 5 and x.code[:2] == x.parent and x.code.isdigit() for x in children))
        self.assertTrue(all(x.parent in r and r[x.parent].level == "SIDO" for x in children))
        same_name = [x.code for x in children if x.name == "중구"]
        self.assertGreater(len(same_name), 1)  # 중구 exists in several SIDO, told apart by code

    def test_unusable_files_are_rejected(self):
        cases = {
            "header": dict(header=("코드", "이름", "상태")),
            "short code": dict(rows=[("11000000", "서울특별시", LIVE)]),
            "status": dict(rows=[("1100000000", "서울특별시", "존재함")]),
            "duplicate": dict(rows=[("1100000000", "서울특별시", LIVE), ("1100000000", "서울", LIVE)]),
            "blank name": dict(rows=[("1100000000", " ", LIVE)]),
        }
        for label, case in cases.items():
            with self.subTest(label), self.assertRaises(SnapshotError):
                snapshot(case.get("rows", FIXTURE), header=case.get("header", SOURCE_HEADER))
        with self.subTest("columns"), self.assertRaises(SnapshotError):
            parse_rows(["\t".join(SOURCE_HEADER), "1100000000\t서울특별시"])

    def test_files_on_disk(self):
        text = "\n".join(["\t".join(SOURCE_HEADER)] + ["\t".join(r) for r in FIXTURE])
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory, "codes.txt")
            plain.write_bytes(text.encode("cp949"))
            self.assertEqual(len(read_snapshot(plain).rows), len(FIXTURE))
            utf8 = Path(directory, "utf8.txt")
            utf8.write_bytes(text.encode("utf-8"))
            with self.assertRaises(SnapshotError):
                read_snapshot(utf8)
            two = Path(directory, "two.zip")
            with zipfile.ZipFile(two, "w") as archive:
                archive.writestr("a.txt", text.encode("cp949"))
                archive.writestr("b.txt", text.encode("cp949"))
            with self.assertRaises(SnapshotError):
                read_snapshot(two)

    def test_inconsistent_hierarchies_stop_the_conversion(self):
        cases = {
            "구 without its city": [r for r in FIXTURE if r[0] != "4111000000"],
            "live 시군구 under an abolished SIDO": FIXTURE + [("4213000000", "강원도 원주시", LIVE)],
            "name not under its SIDO": FIXTURE + [("1117000000", "부산광역시 용산구", LIVE)],
            "unexpected 시군구 in 세종": FIXTURE + [("3612000000", "세종특별자치시 조치원읍", LIVE)],
            "same name twice in one SIDO": FIXTURE + [("1117000000", "서울특별시 중구", LIVE)],
            "SIDO with nothing to choose": FIXTURE + [("4400000000", "충청남도", LIVE)],
        }
        for label, rows in cases.items():
            with self.subTest(label), self.assertRaises(SnapshotError):
                build_master(snapshot(rows).rows, **SMALL)
        with self.subTest("too few SIDO"), self.assertRaises(SnapshotError):
            build_master(snapshot(FIXTURE).rows)  # default floors are for the national file


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.Session = sessionmaker(bind=self.engine)

    def sync(self, rows, apply=True, **options):
        with self.Session() as db:
            try:
                report = sync_regions(db, snapshot(rows), apply=apply, **{**SMALL, **options})
                db.commit() if apply else db.rollback()
                return report
            except Exception:
                db.rollback()
                raise

    def table(self):
        with self.Session() as db:
            return {r.region_code: (r.region_name, r.region_level, r.parent_region_code, r.is_active)
                    for r in db.query(Region)}

    def test_initial_load_and_identical_rerun(self):
        report = self.sync(FIXTURE)
        self.assertEqual(report["counts"], {"inserted": 16, "renamed": 0, "reactivated": 0, "deactivated": 0})
        first = self.table()
        self.assertEqual(len(first), 16)
        self.assertTrue(all(row[3] for row in first.values()))
        again = self.sync(FIXTURE)
        self.assertEqual(again["counts"], {"inserted": 0, "renamed": 0, "reactivated": 0, "deactivated": 0})
        self.assertEqual(self.table(), first)

    def test_preview_writes_nothing(self):
        report = self.sync(FIXTURE, apply=False)
        self.assertEqual(report["counts"]["inserted"], 16)
        self.assertFalse(report["applied"])
        self.assertEqual(self.table(), {})

    def test_rename_new_code_abolition_and_return(self):
        self.sync(FIXTURE)
        changed = replace(FIXTURE, "4374500000", name="충청북도 증평읍군")
        changed = replace(changed, "1114000000", status=GONE)
        changed += [("1117000000", "서울특별시 용산구", LIVE)]
        report = self.sync(changed)
        self.assertEqual(report["renamed"], [{"code": "43745", "before": "증평군", "after": "증평읍군"}])
        self.assertEqual([x["code"] for x in report["inserted"]], ["11170"])
        self.assertEqual(report["deactivated"], [{"code": "11140", "name": "중구", "reason": REASON_ABOLISHED}])
        table = self.table()
        self.assertEqual(table["11140"], ("중구", "SIGUNGU", "11", False))  # kept, not deleted
        self.assertEqual(table["43745"][0], "증평읍군")
        back = self.sync(replace(changed, "1114000000", status=LIVE))
        self.assertEqual(back["reactivated"], ["11140"])

    def test_city_that_gets_districts(self):
        """Like 화성시 (41590): the city code is deactivated and its new 구 are added."""
        before = FIXTURE + [("4159000000", "경기도 화성시", LIVE)]
        self.sync(before)
        self.assertTrue(self.table()["41590"][3])
        after = before + [("4159100000", "경기도 화성시 만세구", LIVE), ("4159700000", "경기도 화성시 동탄구", LIVE)]
        report = self.sync(after)
        self.assertEqual(report["deactivated"], [{"code": "41590", "name": "화성시", "reason": REASON_INTERMEDIATE}])
        self.assertEqual(sorted(x["code"] for x in report["inserted"]), ["41591", "41597"])
        self.assertEqual(self.table()["41597"], ("화성시 동탄구", "SIGUNGU", "41", True))

    def test_merged_sido(self):
        """Like 광주광역시 (29) joining 전남광주통합특별시 (12) in the 2026 file."""
        before = FIXTURE + [("2900000000", "광주광역시", LIVE), ("2911000000", "광주광역시 동구", LIVE)]
        self.sync(before)
        after = replace(replace(before, "2900000000", status=GONE), "2911000000", status=GONE)
        after += [("1200000000", "전남광주통합특별시", LIVE), ("1221000000", "전남광주통합특별시 동구", LIVE)]
        report = self.sync(after)
        self.assertEqual(sorted(x["code"] for x in report["deactivated"]), ["29", "29110"])
        self.assertEqual(sorted(x["code"] for x in report["inserted"]), ["12", "12210"])
        table = self.table()
        self.assertFalse(table["29"][3] or table["29110"][3])

    def test_partial_file_stops_without_writing(self):
        self.sync(FIXTURE)
        before = self.table()
        partial = [r for r in FIXTURE if not r[0].startswith("43")]  # 충청북도 rows missing, not abolished
        with self.assertRaises(SnapshotError) as caught:
            self.sync(partial)
        self.assertIn("자료 누락", str(caught.exception))
        self.assertEqual(self.table(), before)

    def test_large_deactivation_needs_explicit_permission(self):
        rows = FIXTURE + [(f"11{n:03d}00000", f"서울특별시 테스트{n}구", LIVE) for n in range(200, 240, 2)]
        self.sync(rows)
        before = self.table()
        gone = [(c, n, GONE if c.startswith("112") or c.startswith("113") else s) for c, n, s in rows]
        with self.assertRaises(SyncRefused):
            self.sync(gone)
        self.assertEqual(self.table(), before)
        report = self.sync(gone, allow_large_change=True)
        self.assertEqual(report["counts"]["deactivated"], 20)

    def test_failure_after_writes_rolls_everything_back(self):
        self.sync(FIXTURE)
        before = self.table()
        changed = replace(FIXTURE, "1111000000", name="서울특별시 종로구청") + [("1117000000", "서울특별시 용산구", LIVE)]
        with patch("app.region_master.master_problems", return_value=["주입한 실패"]), \
                self.assertRaises(SnapshotError):
            self.sync(changed)
        self.assertEqual(self.table(), before)

    def test_changed_level_or_parent_is_refused(self):
        """A code whose level or parent differs from the stored row needs manual review."""
        self.sync(FIXTURE)
        before = self.table()
        master = build_master(snapshot(FIXTURE).rows, **SMALL)
        for code, change in (("41150", {"parent": "43"}), ("11110", {"level": "SPECIAL_SELECTABLE"})):
            changed = build_master(snapshot(FIXTURE).rows, **SMALL)
            changed.records[code] = dataclasses.replace(master.records[code], **change)
            with self.subTest(code=code), patch("app.region_master.build_master", return_value=changed),                     self.assertRaises(SyncRefused):
                self.sync(FIXTURE)
        self.assertEqual(self.table(), before)

    def test_database_constraints(self):
        self.sync(FIXTURE)
        bad = [dict(region_code="99", region_name="잘못", region_level="DONG", parent_region_code=None),
               dict(region_code="98", region_name="잘못", region_level="SIDO", parent_region_code="11"),
               dict(region_code="11999", region_name="잘못", region_level="SIGUNGU", parent_region_code="26"),
               dict(region_code="99110", region_name="없는 시도", region_level="SIGUNGU", parent_region_code="99"),
               dict(region_code="11", region_name="중복", region_level="SIDO", parent_region_code=None),
               dict(region_code="36_SPECIAL_ALL2", region_name="잘못", region_level="SPECIAL_SELECTABLE",
                    parent_region_code="36"),
               dict(region_code="97", region_name=" ", region_level="SIDO", parent_region_code=None)]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(IntegrityError), self.Session.begin() as db:
                db.add(Region(is_active=True, **values))
        with self.assertRaises(IntegrityError), self.Session.begin() as db:
            db.add(Region(region_code="96", region_name="상태 없음", region_level="SIDO", is_active=None))


class SelectionCheckTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            sync_regions(db, snapshot(FIXTURE), apply=True, **SMALL)
            db.add(Region(region_code="42", region_name="강원도", region_level="SIDO", is_active=False))
            db.flush()
            db.add(Region(region_code="42110", region_name="춘천시", region_level="SIGUNGU",
                          parent_region_code="42", is_active=True))  # broken: active under inactive
            db.add(Region(region_code="11170", region_name="용산구", region_level="SIGUNGU",
                          parent_region_code="11", is_active=False))
            db.commit()

    def test_reasons(self):
        codes = ["11110", SEJONG_ALL_CODE, "11", "36", "11170", "99999", "42110", "11110", "", 11110, "x" * 21]
        with self.Session() as db:
            result = check_region_codes(db, codes)
        self.assertEqual([r.region_code for r in result.regions], ["11110", SEJONG_ALL_CODE])
        self.assertFalse(result.ok)
        self.assertEqual(result.problems, {
            "11110": DUPLICATE, "": INVALID_FORMAT, "int:11110": INVALID_FORMAT, "x" * 21: INVALID_FORMAT,
            "11": NOT_SELECTABLE_LEVEL, "36": NOT_SELECTABLE_LEVEL, "11170": INACTIVE, "99999": NOT_FOUND,
            "42110": PARENT_INACTIVE})

    def test_require_helpers(self):
        with self.Session() as db:
            regions_ = require_selectable_regions(db, ["51110", "41117", SEJONG_ALL_CODE], lock=True)
            self.assertEqual([r.region_code for r in regions_], ["51110", "41117", SEJONG_ALL_CODE])
            self.assertEqual(require_selectable_region(db, "11110").region_name, "종로구")
            with self.assertRaises(InvalidRegionCodes) as caught:
                require_selectable_regions(db, ["11110", "11"])
            self.assertEqual(caught.exception.problems, {"11": NOT_SELECTABLE_LEVEL})
            self.assertFalse(db.new or db.dirty or db.deleted)


class RegionApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(regions.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def load(self, rows=FIXTURE):
        with self.Session() as db:
            sync_regions(db, snapshot(rows), apply=True, **SMALL)
            db.commit()

    def rows(self):
        with self.Session() as db:
            return sorted((r.region_code, r.region_name, r.region_level, r.parent_region_code, r.is_active)
                          for r in db.query(Region))

    def assert_error(self, response, status, code, data=None):
        self.assertEqual((response.status_code, response.json()["code"]), (status, code), response.text)
        if data is not None:
            self.assertEqual(response.json()["data"], data)

    def test_sido_list(self):
        self.load(FIXTURE + [("2900000000", "광주광역시", GONE)])
        before = self.rows()
        response = self.client.get("/regions/sido")  # no Authorization header
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual((body["code"], body["message"]), ("OK", "시도 목록을 조회했습니다."))
        self.assertEqual(body["data"]["regions"], [
            {"region_code": "11", "region_name": "서울특별시"}, {"region_code": "26", "region_name": "부산광역시"},
            {"region_code": "36", "region_name": "세종특별자치시"}, {"region_code": "41", "region_name": "경기도"},
            {"region_code": "43", "region_name": "충청북도"}, {"region_code": "51", "region_name": "강원특별자치도"}])
        self.assertEqual(self.client.get("/regions/sido").json(), body)  # stable order
        self.assertEqual(self.rows(), before)  # reads only

    def test_sigungu_list(self):
        self.load()
        with self.Session.begin() as db:
            db.get(Region, "41111").is_active = False
        before = self.rows()
        response = self.client.get("/regions/sigungu", params={"sido_code": "41"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual((body["code"], body["message"]), ("OK", "시군구 목록을 조회했습니다."))
        self.assertEqual(body["data"], {"sido_code": "41", "regions": [
            {"region_code": "41117", "region_name": "수원시 영통구", "sido_code": "41"},
            {"region_code": "41150", "region_name": "의정부시", "sido_code": "41"}]})
        seoul = self.client.get("/regions/sigungu", params={"sido_code": "11"}).json()["data"]["regions"]
        busan = self.client.get("/regions/sigungu", params={"sido_code": "26"}).json()["data"]["regions"]
        self.assertEqual([r["region_code"] for r in seoul if r["region_name"] == "중구"], ["11140"])
        self.assertEqual([r["region_code"] for r in busan if r["region_name"] == "중구"], ["26110"])
        self.assertTrue(all(r["sido_code"] == "11" for r in seoul))
        self.assertEqual(self.rows(), before)

    def test_sejong_returns_only_the_special_code(self):
        self.load()
        body = self.client.get("/regions/sigungu", params={"sido_code": "36"}).json()
        self.assertEqual(body["data"], {"sido_code": "36", "regions": [
            {"region_code": "36_SPECIAL_ALL", "region_name": "세종특별자치시 전체", "sido_code": "36"}]})

    def test_parameter_errors(self):
        self.load()
        self.assert_error(self.client.get("/regions/sigungu"), 422, "SIDO_CODE_REQUIRED", {"field": "sido_code"})
        self.assert_error(self.client.get("/regions/sigungu", params={"sido_code": " "}), 422, "SIDO_CODE_REQUIRED")
        for value in ("1", "111", "11110", "ab", "1a", "１１", "36_SPECIAL_ALL"):
            with self.subTest(value=value):
                self.assert_error(self.client.get("/regions/sigungu", params={"sido_code": value}),
                                  422, "INVALID_SIDO_CODE", {"field": "sido_code"})

    def test_missing_or_inactive_sido_is_404(self):
        self.load()
        with self.Session.begin() as db:
            db.add(Region(region_code="42", region_name="강원도", region_level="SIDO", is_active=False))
        for value in ("99", "42"):
            with self.subTest(value=value):
                response = self.client.get("/regions/sigungu", params={"sido_code": value})
                self.assert_error(response, 404, "REGION_NOT_FOUND", {})
                self.assertEqual(response.json()["message"], "선택한 지역을 찾을 수 없습니다.")

    def test_empty_or_broken_master_is_503(self):
        expected = {"code": "REGION_DATA_UNAVAILABLE", "data": {}, "message": "지역 목록을 불러올 수 없습니다."}
        for path in ("/regions/sido", "/regions/sigungu?sido_code=11"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual((response.status_code, response.json()), (503, expected))
        self.load()
        with self.Session.begin() as db:  # an active SIDO left without any choice
            db.get(Region, "51110").is_active = False
        self.assertEqual(self.client.get("/regions/sigungu", params={"sido_code": "51"}).status_code, 503)

    def test_database_failure_is_500(self):
        self.load()
        for path, target in (("/regions/sido", "list_sido"), ("/regions/sigungu?sido_code=11", "list_sigungu")):
            with self.subTest(path=path), \
                    patch(f"app.routers.regions.{target}", side_effect=OperationalError("SELECT", {}, Exception())), \
                    self.assertLogs("app.api_contract", "ERROR"):
                response = self.client.get(path)
                self.assertEqual(response.json(), {"code": "INTERNAL_SERVER_ERROR", "data": {},
                                                   "message": "일시적인 오류가 발생했습니다."})
                self.assertEqual(response.status_code, 500)


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.Session = sessionmaker(bind=self.engine)

    def run_script(self, *args):
        out = io.StringIO()
        with patch("app.database.SessionLocal", self.Session), patch("sys.stdout", out):
            code = sync_script.main(list(args))
        return code, out.getvalue()

    def count(self):
        with self.Session() as db:
            return db.query(Region).count()

    def test_preview_then_apply_with_the_real_file(self):
        code, out = self.run_script("--input", str(REAL_SNAPSHOT))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["counts"]["inserted"], 16 + 255 + 1)
        self.assertEqual(self.count(), 0)
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory, "report.json")
            code, out = self.run_script("--input", str(REAL_SNAPSHOT), "--apply", "--report", str(report_path))
            self.assertEqual(code, 0)
            saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["applied"])
        self.assertEqual(saved["source"]["file_sha256"], json.loads(REAL_METADATA.read_text("utf-8"))["zip_sha256"])
        self.assertEqual(self.count(), 272)
        code, out = self.run_script("--input", str(REAL_SNAPSHOT), "--apply")
        self.assertEqual(json.loads(out)["counts"], {"inserted": 0, "renamed": 0, "reactivated": 0, "deactivated": 0})

    def test_offline_and_refused_runs(self):
        code, out = self.run_script("--input", str(REAL_SNAPSHOT), "--offline")
        self.assertEqual((code, json.loads(out)["master"]["active_sido"]), (0, 16))
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory, "bad.txt")
            bad.write_bytes("머리글\n".encode("cp949"))
            err = io.StringIO()
            with patch("sys.stderr", err):
                code, _ = self.run_script("--input", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("중단", err.getvalue())
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()
