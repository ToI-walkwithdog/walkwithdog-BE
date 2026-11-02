"""Opt-in integration tests against an EMPTY, disposable local PostgreSQL database.

WALK_TEST_DATABASE_URL must name toi_walk_test_*. This file never reads DATABASE_URL
for its target, and never drops databases/tables or deletes existing data.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.models import Base
    from app.walk_models import WALK_MODELS

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from scripts.migrate_backend import migrate
from scripts.prepare_demo_db import ROOT, PreparationError, table_counts, validate_local_engine
from scripts.walk_schema import check_walk_schema

TEST_URL = os.getenv("WALK_TEST_DATABASE_URL")


class TransactionEngine:
    """Exercise real migrate()/SQL/rollback, preserving the enclosing test transaction."""
    def __init__(self, engine, connection):
        self.url, self.dialect, self.connection = engine.url, engine.dialect, connection

    @contextmanager
    def connect(self):
        yield self.connection

    @contextmanager
    def begin(self):
        with self.connection.begin_nested():
            yield self.connection


@unittest.skipUnless(TEST_URL, "WALK_TEST_DATABASE_URL의 격리 PostgreSQL이 필요합니다")
class WalkPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(TEST_URL)
        validate_local_engine(cls.engine)
        if not cls.engine.url.database.startswith("toi_walk_test_"):
            raise PreparationError("테스트 DB 이름은 toi_walk_test_로 시작해야 합니다.")
        with cls.engine.begin() as connection:
            if inspect(connection).get_table_names():
                raise PreparationError("빈 격리 테스트 DB만 허용합니다. 기존 DB는 변경하지 않습니다.")
            names = {m.__tablename__ for m in WALK_MODELS}
            Base.metadata.create_all(connection, tables=[t for t in Base.metadata.sorted_tables if t.name not in names])
            connection.execute(text("INSERT INTO shelters(id, name) VALUES (1, '기존 보호소')"))
            connection.execute(text("INSERT INTO users(id, email, nickname) VALUES (1, 'one@example.com', '봉사자'), (2, 'two@example.com', '담당자')"))
            connection.execute(text("INSERT INTO dogs(id, name, shelter_id) VALUES (1, '강아지1', 1), (2, '강아지2', 1)"))
            connection.execute(text("INSERT INTO surveys(id, user_id) VALUES (1, 1)"))
            connection.execute(text("INSERT INTO matching_results(id, user_id, dog_id, survey_id, score, analysis_snapshot) VALUES (1, 1, 1, 1, 80, '{\"preserved\": true}')"))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.outer = self.connection.begin()
        self.proxy = TransactionEngine(self.engine, self.connection)
        self.addCleanup(self.connection.close)
        self.addCleanup(self.outer.rollback)

    def apply_sql(self, source=None):
        source = source or (ROOT / "migrations/007_walk_domain.sql").read_text()
        # No inner COMMIT: the outer test transaction owns all changes.
        for statement in source.split(";"):
            statement = "\n".join(l for l in statement.splitlines() if not l.startswith("--")).strip()
            if statement and statement.upper() not in ("BEGIN", "COMMIT"):
                self.connection.exec_driver_sql(statement)

    def test_actual_backup_apply_reapply_and_legacy_content(self):
        before = table_counts(self.connection)
        preview = migrate(self.proxy, "007")
        self.assertEqual(len(preview["missing"]), 10)
        self.assertIsNone(preview["backup"])
        result = migrate(self.proxy, "007", apply=True)
        self.assertTrue(result["applied"])
        self.assertGreater(Path(result["backup"]).stat().st_size, 0)
        self.assertEqual(check_walk_schema(self.connection), [])
        self.assertFalse(migrate(self.proxy, "007", apply=True)["applied"])
        after = table_counts(self.connection)
        self.assertTrue(all(after[name] == count for name, count in before.items()))
        self.assertEqual(self.connection.execute(text("SELECT score, analysis_snapshot FROM matching_results WHERE id = 1")).one(), (80, {"preserved": True}))

    def test_orm_postgresql_create_matches_reviewed_catalog_contract(self):
        Base.metadata.create_all(self.connection)
        self.assertEqual(check_walk_schema(self.connection), [])

    def test_sql_error_rolls_back_all_new_tables(self):
        original = Path.read_text
        source = (ROOT / "migrations/007_walk_domain.sql").read_text() + "\nSELECT 1/0;"
        def read(path, *args, **kwargs):
            if path.name == "007_walk_domain.sql":
                return source
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            with self.assertRaises(DBAPIError):
                migrate(self.proxy, "007", apply=True)
        self.assertNotIn("walk_requests", inspect(self.connection).get_table_names())
        self.assertEqual(self.connection.execute(text("SELECT count(*) FROM matching_results")).scalar_one(), 1)

    def test_partial_table_conflict_stops_before_backup(self):
        self.connection.execute(text("CREATE TABLE dog_walk_settings(id INTEGER PRIMARY KEY)"))
        with patch("scripts.migrate_backend.backup_database") as backup:
            with self.assertRaises(PreparationError):
                migrate(self.proxy, "007", apply=True)
            backup.assert_not_called()

    def test_missing_standalone_index_is_repaired(self):
        source = (ROOT / "migrations/007_walk_domain.sql").read_text()
        source = source.replace("CREATE INDEX IF NOT EXISTS ix_walk_requests_reviewer ON walk_requests (reviewed_by);", "")
        self.apply_sql(source)
        self.assertEqual(check_walk_schema(self.connection), ["ix_walk_requests_reviewer"])
        self.assertTrue(migrate(self.proxy, "007", apply=True)["applied"])
        self.assertEqual(check_walk_schema(self.connection), [])

    def test_default_nullable_check_and_index_drift_stop(self):
        self.apply_sql()
        changes = (
            "ALTER TABLE dog_walk_settings ALTER COLUMN walkable SET DEFAULT true",
            "ALTER TABLE walk_requests ALTER COLUMN reviewed_at SET NOT NULL",
            "ALTER TABLE walk_requests ADD CONSTRAINT unexpected_check CHECK (user_id > 0)",
            "CREATE INDEX unexpected_index ON walk_requests(status)",
        )
        for sql in changes:
            with self.connection.begin_nested() as savepoint:
                self.connection.execute(text(sql))
                with self.subTest(sql=sql), self.assertRaises(PreparationError):
                    check_walk_schema(self.connection)
                savepoint.rollback()

    def test_wrong_named_index_owned_by_another_table_stops(self):
        self.connection.execute(text("CREATE INDEX ix_walk_requests_reviewer ON users(id)"))
        with self.assertRaises(PreparationError):
            check_walk_schema(self.connection)

    def test_incompatible_parent_pk_and_search_path_stop(self):
        for sql in ("ALTER TABLE dogs ALTER COLUMN id TYPE BIGINT", "SET LOCAL search_path = pg_catalog, public"):
            with self.connection.begin_nested() as savepoint:
                self.connection.execute(text(sql))
                with self.assertRaises(PreparationError):
                    check_walk_schema(self.connection)
                savepoint.rollback()

    def request(self, user=1, dog=1, status="APPROVED", start="2026-11-02 01:00+00", end="2026-11-02 02:00+00"):
        return self.connection.execute(text("""
            INSERT INTO walk_requests(user_id, dog_id, shelter_id, status,
                requested_start_at, requested_end_at, reviewed_by, reviewed_at)
            VALUES (:user, :dog, 1, :status, :start, :end, :reviewer, :reviewed)
            RETURNING id
        """), dict(user=user, dog=dog, status=status, start=start, end=end,
                   reviewer=2 if status == "APPROVED" else None,
                   reviewed=start if status == "APPROVED" else None)).scalar_one()

    def test_approved_dog_and_volunteer_overlap_block_but_adjacent_allowed(self):
        self.apply_sql()
        self.request()
        for user, dog in ((2, 1), (1, 2)):
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.request(user=user, dog=dog)
        self.request(user=1, dog=1, start="2026-11-02 02:00+00", end="2026-11-02 03:00+00")
        # Multiple pending proposals may overlap; promotion is guarded by exclusion.
        request_id = self.request(user=2, dog=1, status="PENDING")
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.connection.execute(text("UPDATE walk_requests SET status='APPROVED', reviewed_by=2, reviewed_at=now() WHERE id=:id"), {"id": request_id})

    def test_numeric_precision_and_timezone_roundtrip(self):
        from decimal import Decimal
        self.connection.execute(text("SET LOCAL TIME ZONE 'Asia/Seoul'"))
        self.apply_sql()
        request_id = self.request()
        walk_id = self.connection.execute(text("INSERT INTO walks(walk_request_id, started_at) VALUES (:id, '2026-11-02 10:00+09') RETURNING id"), {"id": request_id}).scalar_one()
        self.connection.execute(text("INSERT INTO walk_points(walk_id, latitude, longitude, recorded_at, sequence_no) VALUES (:id, 37.123456, 127.654321, '2026-11-02 01:00+00', 0)"), {"id": walk_id})
        row = self.connection.execute(text("SELECT latitude, longitude, recorded_at FROM walk_points")).one()
        self.assertEqual(row[:2], (Decimal("37.123456"), Decimal("127.654321")))
        self.assertEqual(row.recorded_at.utcoffset().total_seconds(), 9 * 3600)
        self.assertEqual(self.connection.execute(text("SELECT started_at = recorded_at FROM walks JOIN walk_points ON walks.id=walk_points.walk_id")).scalar_one(), True)

    def test_booking_range_supports_full_integer_id_domain(self):
        self.apply_sql()
        self.connection.execute(text("INSERT INTO users(id, email, nickname) VALUES (2147483647, 'max@example.com', '상한 ID')"))
        self.connection.execute(text("INSERT INTO dogs(id, name, shelter_id) VALUES (2147483647, '상한 ID', 1)"))
        self.request(user=2147483647, dog=2147483647)


if __name__ == "__main__":
    unittest.main()
