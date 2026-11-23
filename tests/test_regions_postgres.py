"""Migration 022, constraints and region master sync on real PostgreSQL (Issue #43).

REGION_TEST_DATABASE_URL must name an EMPTY local toi_walk_test_* database. Each test
works in its own new rg_<uuid> schema and drops only that schema afterwards.
"""
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from uuid import uuid4

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.database import get_db
    from app.region_master import SnapshotError, read_snapshot, sync_regions
    from app.region_models import SEJONG_ALL_CODE, Region
    from app.region_service import INACTIVE, check_region_codes, require_selectable_regions
    from app.routers import regions
    from scripts.prepare_demo_db import PreparationError, validate_local_engine

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from tests.test_regions import FIXTURE, GONE, REAL_SNAPSHOT, SMALL, replace, snapshot

TEST_URL = os.getenv("REGION_TEST_DATABASE_URL")
SCHEMA_NAME = re.compile(r"rg_[0-9a-f]{32}")
ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "migrations" / "022_regions.sql").read_text(encoding="utf-8")


@unittest.skipUnless(TEST_URL, "REGION_TEST_DATABASE_URL의 빈 격리 PostgreSQL이 필요합니다")
class RegionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = create_engine(TEST_URL)
        validate_local_engine(cls.admin)
        if not cls.admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        with cls.admin.connect() as connection:
            inspector = inspect(connection)
            custom = [s for s in inspector.get_schema_names()
                      if s not in ("public", "information_schema") and not s.startswith("pg_")]
            if inspector.get_table_names() or custom:
                raise PreparationError("새로운 빈 테스트 DB만 허용합니다.")
        cls.created_schemas = set()

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.admin.connect() as connection:
                left = sorted(set(inspect(connection).get_schema_names()) & cls.created_schemas)
            if left:
                raise PreparationError(f"정리되지 않은 테스트 스키마: {', '.join(left)}")
        finally:
            cls.admin.dispose()

    def setUp(self):
        self.schema = "rg_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.created_schemas.add(self.schema)
        self.addCleanup(self.drop_own_schema, self.schema)
        self.engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={self.schema}"})
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)

    def drop_own_schema(self, schema):
        if (schema not in self.created_schemas or SCHEMA_NAME.fullmatch(schema) is None
                or not self.admin.url.database.startswith("toi_walk_test_")):
            raise PreparationError(f"이 실행이 만든 테스트 스키마가 아니므로 삭제하지 않습니다: {schema}")
        with self.admin.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            owned = connection.execute(text(
                "SELECT n.nspowner = r.oid FROM pg_namespace n, pg_roles r"
                " WHERE n.nspname = :schema AND r.rolname = current_user"), {"schema": schema}).scalar_one_or_none()
            if owned is not True:
                raise PreparationError(f"현재 테스트 계정 소유 스키마가 아니므로 삭제하지 않습니다: {schema}")
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        self.created_schemas.discard(schema)

    def run_sql_file(self, sql):
        raw = self.engine.raw_connection()
        driver = raw.driver_connection
        try:
            driver.autocommit = True  # the file manages its own BEGIN/COMMIT, like psql
            with driver.cursor() as cursor:
                try:
                    cursor.execute(sql)
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
        finally:
            driver.autocommit = False
            raw.close()

    def schema_snapshot(self):
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'regions'::regclass ORDER BY conname")).all()
            indexes = connection.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"
                " ORDER BY indexname")).all()
            columns = connection.execute(text(
                "SELECT column_name, data_type, character_maximum_length, is_nullable FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'regions' ORDER BY ordinal_position")).all()
        return rows, indexes, columns

    def sync(self, snap, apply=True, **options):
        with self.Session() as db:
            try:
                report = sync_regions(db, snap, apply=apply, **options)
                db.commit() if apply else db.rollback()
                return report
            except Exception:
                db.rollback()
                raise

    def table(self):
        with self.Session() as db:
            return {r.region_code: (r.region_name, r.region_level, r.parent_region_code, r.is_active)
                    for r in db.query(Region)}

    def test_migration_022_shape_and_rerun(self):
        self.run_sql_file(MIGRATION)
        constraints, indexes, columns = self.schema_snapshot()
        self.assertEqual([c[0] for c in constraints], ["ck_regions_code_digits", "ck_regions_hierarchy",
                                                      "ck_regions_level", "ck_regions_name_not_blank",
                                                      "fk_regions_parent_region_code", "regions_pkey"])
        self.assertEqual([i[0] for i in indexes], ["ix_regions_parent_active", "regions_pkey"])
        self.assertEqual(columns, [("region_code", "character varying", 20, "NO"),
                                   ("region_name", "character varying", 100, "NO"),
                                   ("region_level", "character varying", 20, "NO"),
                                   ("parent_region_code", "character varying", 20, "YES"),
                                   ("is_active", "boolean", None, "NO")])
        self.run_sql_file(MIGRATION)  # re-running is a no-op
        self.assertEqual(self.schema_snapshot(), (constraints, indexes, columns))
        with self.engine.connect() as connection:
            tables = inspect(connection).get_table_names()
        self.assertEqual(tables, ["regions"])  # nothing else is created

    def test_constraints_on_the_migrated_table(self):
        self.run_sql_file(MIGRATION)
        self.sync(snapshot(FIXTURE), **SMALL)
        bad = [("DONG level", "('99', '잘못', 'DONG', NULL, true)"),
               ("SIDO with parent", "('98', '잘못', 'SIDO', '11', true)"),
               ("SIDO of three digits", "('981', '잘못', 'SIDO', NULL, true)"),
               ("SIDO not digits", "('9a', '잘못', 'SIDO', NULL, true)"),
               ("SIGUNGU under another SIDO", "('11999', '잘못', 'SIGUNGU', '26', true)"),
               ("SIGUNGU not digits", "('11a99', '잘못', 'SIGUNGU', '11', true)"),
               ("parent missing (FK)", "('99110', '없는 시도', 'SIGUNGU', '99', true)"),
               ("duplicate code", "('11', '중복', 'SIDO', NULL, true)"),
               ("special under another SIDO", "('36_SPECIAL_ALL', '잘못', 'SPECIAL_SELECTABLE', '11', true)"),
               ("other special code", "('36_ALL', '잘못', 'SPECIAL_SELECTABLE', '36', true)"),
               ("blank name", "('97', '  ', 'SIDO', NULL, true)"),
               ("null active", "('96', '상태 없음', 'SIDO', NULL, NULL)")]
        for label, values in bad:
            with self.subTest(label), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO regions (region_code, region_name, region_level,"
                                           f" parent_region_code, is_active) VALUES {values}")
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM regions WHERE region_code = '11'")  # children keep it

    def test_real_file_load_rerun_and_api_on_migrated_table(self):
        self.run_sql_file(MIGRATION)
        real = read_snapshot(REAL_SNAPSHOT)
        report = self.sync(real)
        self.assertEqual(report["counts"], {"inserted": 272, "renamed": 0, "reactivated": 0, "deactivated": 0})
        first = self.table()
        again = self.sync(real)
        self.assertEqual(again["counts"], {"inserted": 0, "renamed": 0, "reactivated": 0, "deactivated": 0})
        self.assertEqual(self.table(), first)

        app = FastAPI()
        app.include_router(regions.router)

        def test_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        with TestClient(app) as client:
            sido = client.get("/regions/sido").json()["data"]["regions"]
            self.assertEqual(len(sido), 16)
            self.assertEqual(sido[0], {"region_code": "11", "region_name": "서울특별시"})
            gyeonggi = client.get("/regions/sigungu", params={"sido_code": "41"}).json()["data"]["regions"]
            names = [r["region_name"] for r in gyeonggi]
            self.assertIn("수원시 영통구", names)
            self.assertNotIn("수원시", names)
            self.assertTrue(all(r["sido_code"] == "41" for r in gyeonggi))
            sejong = client.get("/regions/sigungu", params={"sido_code": "36"}).json()["data"]["regions"]
            self.assertEqual([r["region_code"] for r in sejong], [SEJONG_ALL_CODE])
            self.assertEqual(client.get("/regions/sigungu", params={"sido_code": "29"}).status_code, 404)
        self.assertEqual(self.table(), first)

    def test_failed_sync_leaves_the_previous_master(self):
        self.run_sql_file(MIGRATION)
        self.sync(snapshot(FIXTURE), **SMALL)
        before = self.table()
        changed = replace(FIXTURE, "1114000000", status=GONE) + [("1117000000", "서울특별시 용산구", "존재")]
        with patch("app.region_master.master_problems", return_value=["주입한 실패"]), \
                self.assertRaises(SnapshotError):
            self.sync(snapshot(changed), **SMALL)
        self.assertEqual(self.table(), before)
        partial = [r for r in FIXTURE if not r[0].startswith("51")]
        with self.assertRaises(SnapshotError):
            self.sync(snapshot(partial), **SMALL)
        self.assertEqual(self.table(), before)

    def test_abolished_region_stays_for_existing_references(self):
        self.run_sql_file(MIGRATION)
        self.sync(snapshot(FIXTURE), **SMALL)
        with self.engine.begin() as connection:  # stands in for #44's walk routine regions
            connection.exec_driver_sql("CREATE TABLE stored_choices (region_code VARCHAR(20) NOT NULL"
                                       " REFERENCES regions(region_code))")
            connection.exec_driver_sql("INSERT INTO stored_choices VALUES ('11140')")
        report = self.sync(snapshot(replace(FIXTURE, "1114000000", status=GONE)), **SMALL)
        self.assertEqual([x["code"] for x in report["deactivated"]], ["11140"])
        self.assertEqual(self.table()["11140"], ("중구", "SIGUNGU", "11", False))
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM stored_choices").scalar(), 1)
        with self.Session() as db:
            self.assertEqual(check_region_codes(db, ["11140"]).problems, {"11140": INACTIVE})

    def test_selection_check_with_share_lock(self):
        """lock=True keeps the chosen rows from being deactivated until the caller ends its transaction."""
        self.run_sql_file(MIGRATION)
        self.sync(snapshot(FIXTURE), **SMALL)
        with self.Session() as db:
            chosen = require_selectable_regions(db, ["41117", SEJONG_ALL_CODE], lock=True)
            self.assertEqual([r.region_code for r in chosen], ["41117", SEJONG_ALL_CODE])
            with self.assertRaises(OperationalError), self.engine.begin() as other:
                other.exec_driver_sql("SET LOCAL lock_timeout = '300ms'")
                other.exec_driver_sql("UPDATE regions SET is_active = false WHERE region_code = '41117'")
            db.rollback()
        with self.engine.begin() as other:  # free again once the checking transaction ended
            other.exec_driver_sql("SET LOCAL lock_timeout = '300ms'")
            other.exec_driver_sql("UPDATE regions SET is_active = false WHERE region_code = '41117'")


if __name__ == "__main__":
    unittest.main()
