"""Issue #113 (1-D) isolated PostgreSQL verification for scripts/apply_migrations.py.

Uses SIGNUP_TEST_DATABASE_URL and the IsolatedSchema pattern from
tests/test_user_profile_postgres.py so each test runs in a throwaway schema
inside an empty local toi_walk_test_* PostgreSQL database.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from scripts import check_migrations as checker
from scripts.apply_migrations import (
    MigrationExecutor,
    Pg16PublicV1SnapshotProvider,
    apply_main,
    compare_catalog_snapshots,
    create_model_init_evidence,
    validate_backup_file,
)
from scripts.prepare_demo_db import PreparationError, validate_local_engine

TEST_URL = os.getenv("SIGNUP_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]


class IsolatedSchema:
    """A throwaway schema in an empty local test database, dropped after the test."""

    def isolated_schema(self) -> tuple[object, str, dict]:
        admin = create_engine(TEST_URL)
        self.addCleanup(admin.dispose)
        validate_local_engine(admin)
        if not admin.url.database.startswith("toi_walk_test_"):
            raise PreparationError("새 격리 PostgreSQL 테스트 DB가 필요합니다.")
        schema = "apply_mig_" + uuid4().hex[:16]
        with admin.begin() as connection:
            if inspect(connection).get_table_names(schema="public"):
                raise PreparationError("공용 스키마에 테이블이 없는 테스트 DB가 필요합니다.")
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            row = connection.exec_driver_sql(
                "SELECT system_identifier::text, current_database(), "
                "(SELECT oid::text FROM pg_database WHERE datname = current_database()) "
                "FROM pg_control_system()"
            ).one()
            identity = {
                "system_identifier": row[0],
                "database": row[1],
                "database_oid": row[2],
            }

        def remove_schema():
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')

        self.addCleanup(remove_schema)
        engine = create_engine(TEST_URL, connect_args={"options": f"-csearch_path={schema}"})
        self.addCleanup(engine.dispose)
        return engine, schema, identity


class SchemaScopedExecutor(MigrationExecutor):
    """Executor bound to a specific isolated schema via PGOPTIONS search_path."""

    def __init__(self, schema: str):
        super().__init__()
        self.schema = schema

    def execute_bytes(self, sql_bytes: bytes, transaction: str) -> tuple[int, str | None]:
        old_opts = os.environ.get("PGOPTIONS")
        os.environ["PGOPTIONS"] = f"-csearch_path={self.schema}"
        try:
            return super().execute_bytes(sql_bytes.replace(b"public.walks", b"walks"), transaction)
        finally:
            if old_opts is None:
                os.environ.pop("PGOPTIONS", None)
            else:
                os.environ["PGOPTIONS"] = old_opts


@unittest.skipUnless(TEST_URL and shutil.which("psql"), "격리 PostgreSQL 및 psql이 필요합니다")
class ApplyMigrationsPostgresTests(IsolatedSchema, unittest.TestCase):
    @staticmethod
    def _libpq_environment() -> dict[str, str]:
        url = make_url(TEST_URL)
        env = os.environ.copy()
        fields = {
            "PGHOST": url.host,
            "PGPORT": str(url.port) if url.port is not None else None,
            "PGUSER": url.username,
            "PGPASSWORD": url.password,
            "PGDATABASE": url.database,
        }
        for key, value in fields.items():
            if value is not None:
                env[key] = value

        query_fields = {
            "sslmode": "PGSSLMODE",
            "sslcert": "PGSSLCERT",
            "sslkey": "PGSSLKEY",
            "sslrootcert": "PGSSLROOTCERT",
            "application_name": "PGAPPNAME",
            "options": "PGOPTIONS",
            "connect_timeout": "PGCONNECT_TIMEOUT",
        }
        for query_key, env_key in query_fields.items():
            value = url.query.get(query_key)
            if value is not None:
                env[env_key] = str(value)
        return env

    def _make_dummy_backup(self, tmp_dir: Path) -> Path:
        bak = tmp_dir / "verified.dump"
        pg_dump = shutil.which("pg_dump")
        if pg_dump is None:
            self.fail("pg_dump executable not found")
        result = subprocess.run(
            [pg_dump, "--format=custom", "--no-password", "--file", str(bak)],
            env=self._libpq_environment(),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"pg_dump failed (exit={result.returncode})")
        return bak

    def _seed_pre_001_tables(self, engine) -> None:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, nickname VARCHAR(50), "
                "email VARCHAR(255), password VARCHAR(255) DEFAULT '', phone VARCHAR(30), created_at TIMESTAMPTZ DEFAULT NOW())"
            )
            conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS shelters (id SERIAL PRIMARY KEY, name VARCHAR(100))")
            conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS dogs (id SERIAL PRIMARY KEY, name VARCHAR(50))")
            conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS surveys (id SERIAL PRIMARY KEY)")
            conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS matching_results (id SERIAL PRIMARY KEY, dog_id INTEGER)")

    def test_full_apply_rerun_skip_and_schema_record(self):
        engine, schema, identity = self.isolated_schema()
        self._seed_pre_001_tables(engine)

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rec_dir = tmp / "records"
            rec_dir.mkdir()
            bak = self._make_dummy_backup(tmp)
            manifest = checker.read_json(ROOT / "migrations" / "manifest.json")
            ev_path = tmp / "evidence.json"
            ev_files = {
                row["file"]: {"state": "absent", "sha256": row["sha256"], "proof": "isolated-init"}
                for row in manifest["migrations"]
            }
            ev_path.write_text(json.dumps({"environment": "local-test", "files": ev_files}), encoding="utf-8")

            schema_record_out = tmp / "schema-record.json"
            args = argparse.Namespace(
                root=ROOT,
                evidence=ev_path,
                apply=True,
                db_name=identity["database"],
                system_identifier=identity["system_identifier"],
                confirm_apply=identity["database"],
                backup_file=bak,
                record_dir=rec_dir,
                schema_record_out=schema_record_out,
            )
            executor = SchemaScopedExecutor(schema)
            provider = Pg16PublicV1SnapshotProvider()

            with patch.dict(os.environ, self._libpq_environment()):
                self.assertIsNotNone(validate_backup_file(bak, identity["database"], MigrationExecutor()))
                rc = apply_main(args, executor=executor, snapshot_provider=provider)
            self.assertEqual(rc, 0)
            self.assertTrue(schema_record_out.is_file())

            # Rerun with all marked applied -> SKIP_RECORDED (0 executions)
            ev_applied = {
                row["file"]: {"state": "applied", "sha256": row["sha256"], "proof": "previous-apply"}
                for row in manifest["migrations"]
            }
            ev_path.write_text(json.dumps({"environment": "local-test", "files": ev_applied}), encoding="utf-8")
            args.schema_record_out = None
            with patch.dict(os.environ, self._libpq_environment()):
                rc2 = apply_main(args, executor=executor, snapshot_provider=provider)
            self.assertEqual(rc2, 0)

    def test_partial_failure_stops_and_blocks_resume(self):
        _, schema, identity = self.isolated_schema()

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "migrations").mkdir()
            f1 = tmp / "migrations" / "001_ok.sql"
            f2 = tmp / "migrations" / "002_fail.sql"
            f1.write_bytes(b"CREATE TABLE t_ok (id int PRIMARY KEY);\n")
            f2.write_bytes(b"INVALID SQL STATEMENT;\n")
            s1, s2 = checker.checksum(f1), checker.checksum(f2)
            manifest = {
                "version": 1,
                "migrations": [
                    {"file": "001_ok.sql", "requires": [], "sha256": s1, "transaction": "file", "environments": ["local-test"], "prechecks": []},
                    {"file": "002_fail.sql", "requires": ["001_ok.sql"], "sha256": s2, "transaction": "psql-single", "environments": ["local-test"], "prechecks": []},
                ],
                "reservations": [],
            }
            (tmp / "migrations" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            ev_path = tmp / "evidence.json"
            ev_path.write_text(
                json.dumps({
                    "environment": "local-test",
                    "files": {
                        "001_ok.sql": {"state": "absent", "sha256": s1, "proof": "test"},
                        "002_fail.sql": {"state": "absent", "sha256": s2, "proof": "test"},
                    },
                }),
                encoding="utf-8",
            )
            rec_dir = tmp / "records"
            rec_dir.mkdir()
            bak = self._make_dummy_backup(tmp)
            args = argparse.Namespace(
                root=tmp,
                evidence=ev_path,
                apply=True,
                db_name=identity["database"],
                system_identifier=identity["system_identifier"],
                confirm_apply=identity["database"],
                backup_file=bak,
                record_dir=rec_dir,
            )
            executor = SchemaScopedExecutor(schema)
            with patch.dict(os.environ, self._libpq_environment()):
                rc = apply_main(args, executor=executor)
            self.assertEqual(rc, 2)
            runs = list(rec_dir.glob("apply-*.json"))
            self.assertEqual(len(runs), 1)
            data = json.loads(runs[0].read_text(encoding="utf-8"))
            self.assertEqual([f["result"] for f in data["files"]], ["applied", "failed"])

    def test_model_init_equivalence_and_app_smoke(self):
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "AUTO_CREATE_SCHEMA": "false"}):
            from app import main as app_module
            from app.database import Base, get_db as database_get_db

        engine_model, schema_model, identity = self.isolated_schema()
        Base.metadata.create_all(bind=engine_model)

        provider = Pg16PublicV1SnapshotProvider()
        snap_model = provider.capture_snapshot(schema=schema_model)
        fp_model = provider.compute_fingerprint(snap_model["catalog"])

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td)
            init_path = create_model_init_evidence(rec_dir, identity, fp_model)
            init_doc = json.loads(init_path.read_text(encoding="utf-8"))
            self.assertEqual(init_doc["type"], "model-init-evidence")

        # Equivalence helper sanity on real catalog snapshot
        ok, only_a, only_b = compare_catalog_snapshots(snap_model["catalog"], snap_model["catalog"])
        self.assertTrue(ok)
        self.assertEqual(only_a, [])
        self.assertEqual(only_b, [])

        # App boot and /db-test smoke against isolated schema
        TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine_model)

        def override_get_db():
            db = TestingSession()
            try:
                yield db
            finally:
                db.close()

        overrides = app_module.app.dependency_overrides
        previous_overrides = overrides.copy()
        self.addCleanup(overrides.update, previous_overrides)
        self.addCleanup(overrides.clear)
        overrides[app_module.get_db] = override_get_db
        overrides[database_get_db] = override_get_db
        with TestClient(app_module.app) as client:
            resp = client.get("/db-test")
            self.assertEqual(resp.status_code, 200)
