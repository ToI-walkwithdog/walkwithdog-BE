"""Unit tests for scripts/apply_migrations.py: plan calculation, guards, and apply flow.

These tests do not connect to a database and do not invoke real psql/pg_restore.
They use a fake executor / monkeypatched subprocess.run.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import time

import pytest

import scripts
from scripts import check_migrations as checker
from scripts.apply_migrations import (
    ALGORITHM_PG16_PUBLIC_V1,
    BACKUP_MAX_AGE_HOURS,
    MigrationExecutor,
    Pg16PublicV1SnapshotProvider,
    SafetyGuardError,
    apply_main,
    build_schema_record,
    compare_catalog_snapshots,
    create_model_init_evidence,
    extract_sqlstate,
    plan_actions,
    print_plan,
    validate_record_dir,
)

FIRST = "001_base.sql"
SECOND = "002_followup.sql"
THIRD = "003_future.sql"


@pytest.fixture
def inventory(tmp_path):
    (tmp_path / "migrations").mkdir()
    manifest = {"version": 1, "migrations": [], "reservations": []}
    return tmp_path, manifest


def add_migration(inventory, name, requires=(), sha256_digest=None, transaction="file", prechecks=None):
    root, manifest = inventory
    path = root / "migrations" / name
    path.write_bytes(b"SELECT 1;\n")
    digest = sha256_digest if sha256_digest is not None else checker.checksum(path)
    row = {
        "file": name,
        "requires": list(requires),
        "sha256": digest,
        "transaction": transaction,
        "environments": ["local-test", "dev"],
        "prechecks": prechecks if prechecks is not None else [],
    }
    manifest["migrations"].append(row)
    return row


def save_manifest(root, manifest):
    (root / checker.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def run_plan(root, manifest, environment, files):
    save_manifest(root, manifest)
    return plan_actions(environment, files, manifest["migrations"])


# ===== 1-B: Offline plan calculation tests =====


def test_plan_absent_is_executable_candidate(inventory):
    root, manifest = inventory
    first = add_migration(inventory, FIRST)
    actions = run_plan(
        root,
        manifest,
        "dev",
        {FIRST: {"state": "absent", "sha256": first["sha256"], "proof": "catalog comparison"}},
    )
    assert actions == [(FIRST, "MANUAL_PRECHECK_REQUIRED")]


def test_plan_applied_is_skipped(inventory):
    root, manifest = inventory
    first = add_migration(inventory, FIRST)
    actions = run_plan(
        root,
        manifest,
        "dev",
        {FIRST: {"state": "applied", "sha256": first["sha256"], "proof": "execution record"}},
    )
    assert actions == [(FIRST, "SKIP_RECORDED")]


def test_plan_schema_match_is_reconciled(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    actions = run_plan(
        root,
        manifest,
        "dev",
        {FIRST: {"state": "schema-match", "proof": "catalog inspection"}},
    )
    assert actions == [(FIRST, "RECONCILE_SCHEMA_MATCH")]


def test_plan_partial_is_reconciled(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    actions = run_plan(
        root,
        manifest,
        "dev",
        {FIRST: {"state": "partial", "proof": "partial inspection"}},
    )
    assert actions == [(FIRST, "RECONCILE_PARTIAL")]


def test_plan_unknown_is_reconciled(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    actions = run_plan(
        root,
        manifest,
        "dev",
        {FIRST: {"state": "unknown"}},
    )
    assert actions == [(FIRST, "RECONCILE_UNKNOWN")]


def test_plan_checksum_mismatch_blocks(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    actions = run_plan(
        root,
        manifest,
        "dev",
        {FIRST: {"state": "applied", "sha256": "0" * 64, "proof": "execution record"}},
    )
    assert actions == [(FIRST, "RECONCILE_CHECKSUM")]


def test_plan_missing_prerequisite_blocks(inventory):
    root, manifest = inventory
    first = add_migration(inventory, FIRST)
    second = add_migration(inventory, SECOND, [FIRST])
    actions = run_plan(
        root,
        manifest,
        "dev",
        {
            FIRST: {"state": "applied", "sha256": first["sha256"], "proof": "record"},
            SECOND: {"state": "absent", "sha256": second["sha256"], "proof": "catalog comparison"},
        },
    )
    assert actions == [
        (FIRST, "SKIP_RECORDED"),
        (SECOND, "MANUAL_PRECHECK_REQUIRED"),
    ]


def test_plan_blocked_prerequisites_when_dependency_not_verified(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    second = add_migration(inventory, SECOND, [FIRST])
    actions = run_plan(
        root,
        manifest,
        "dev",
        {
            FIRST: {"state": "unknown", "proof": "inspection"},
            SECOND: {"state": "absent", "sha256": second["sha256"], "proof": "catalog comparison"},
        },
    )
    assert actions == [
        (FIRST, "RECONCILE_UNKNOWN"),
        (SECOND, f"BLOCKED_PREREQUISITES ({FIRST})"),
    ]


def test_plan_blocked_returns_nonzero(inventory):
    _, manifest = inventory
    add_migration(inventory, FIRST)
    actions = plan_actions("dev", {FIRST: {"state": "unknown"}}, manifest["migrations"])
    assert any(action.startswith("RECONCILE") for _, action in actions)
    blocked = any(action.startswith(("RECONCILE", "BLOCKED", "OUTSIDE_ENVIRONMENT")) for _, action in actions)
    assert blocked is True


def test_print_plan_outputs_order_and_actions(capsys, inventory):
    _, manifest = inventory
    add_migration(inventory, FIRST)
    actions = plan_actions("dev", {FIRST: {"state": "absent", "proof": "x"}}, manifest["migrations"])
    print_plan("dev", actions)
    out = capsys.readouterr().out
    assert "mode: dry-run" in out
    assert FIRST in out
    assert "MANUAL_PRECHECK_REQUIRED" in out


# ===== 1-C: --apply path unit tests with fake executor =====


class FakeExecutor:
    """Fake executor for psql/pg_restore calls without touching any real DB."""

    def __init__(self, db_name="toi_walk_test_113", system_identifier="1234567890"):
        self.psql_available = True
        self.pg_restore_available = True
        self.backup_list_rc = 0
        self.backup_list_output = f";\n;     dbname: {db_name}\n;\n"
        self.db_name = db_name
        self.system_identifier = system_identifier
        self.identity_error: Exception | None = None
        self.execute_calls: list[tuple[bytes, str]] = []
        self.results: list[tuple[int, str | None]] = []
        self.default_result: tuple[int, str | None] = (0, None)

    def check_psql_available(self) -> bool:
        return self.psql_available

    def check_pg_restore_available(self) -> bool:
        return self.pg_restore_available

    def inspect_backup_list(self, backup_path: Path) -> tuple[int, str]:
        return self.backup_list_rc, self.backup_list_output

    def query_db_identity(self) -> tuple[str, str]:
        if self.identity_error is not None:
            raise self.identity_error
        return self.db_name, self.system_identifier

    def execute_bytes(self, sql_bytes: bytes, transaction: str) -> tuple[int, str | None]:
        self.execute_calls.append((sql_bytes, transaction))
        if self.results:
            return self.results.pop(0)
        return self.default_result


def make_args(tmp_path, root, apply=True, create_records=True, create_backup=True, **overrides):
    bak = overrides.get("backup_file", tmp_path / "backup.dump")
    if create_backup and not bak.exists():
        bak.write_bytes(b"PGDMP_FAKE_CUSTOM_ARCHIVE")
    rec_dir = overrides.get("record_dir", tmp_path / "records")
    if create_records and rec_dir is not None:
        rec_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "root": root,
        "evidence": tmp_path / "evidence.json",
        "apply": apply,
        "db_name": "toi_walk_test_113",
        "system_identifier": "1234567890",
        "confirm_apply": "toi_walk_test_113",
        "backup_file": bak,
        "record_dir": rec_dir,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def write_evidence(path, environment, files):
    path.write_text(json.dumps({"environment": environment, "files": files}), encoding="utf-8")


def test_apply_blocked_plan_refuses_before_db(inventory, tmp_path):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "unknown", "proof": "x"}})
    executor = FakeExecutor()
    rc = apply_main(args, executor=executor)
    assert rc == 1
    assert executor.execute_calls == []
    assert list(args.record_dir.glob("apply-*.json")) == []


def test_apply_confirm_apply_mismatch_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root, confirm_apply="other_db")
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    executor = FakeExecutor()
    rc = apply_main(args, executor=executor)
    assert rc == 3
    assert executor.execute_calls == []
    assert list(args.record_dir.glob("apply-*.json")) == []


def test_apply_db_identity_mismatch_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    executor = FakeExecutor(db_name="toi_walk_test_113", system_identifier="9999999999")
    rc = apply_main(args, executor=executor)
    assert rc == 3
    assert executor.execute_calls == []
    assert list(args.record_dir.glob("apply-*.json")) == []


def test_apply_db_identity_permission_denied_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    executor = FakeExecutor()
    executor.identity_error = SafetyGuardError("permission denied for function pg_control_system")
    rc = apply_main(args, executor=executor)
    assert rc == 3
    assert executor.execute_calls == []
    assert list(args.record_dir.glob("apply-*.json")) == []


def test_apply_missing_backup_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root, create_backup=False, backup_file=tmp_path / "nonexistent.dump")
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    rc = apply_main(args, executor=FakeExecutor())
    assert rc == 3
    assert list(args.record_dir.glob("apply-*.json")) == []


def test_apply_empty_backup_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    bak = tmp_path / "empty.dump"
    bak.write_bytes(b"")
    args = make_args(tmp_path, root, create_backup=False, backup_file=bak)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    rc = apply_main(args, executor=FakeExecutor())
    assert rc == 3


def test_apply_stale_backup_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    bak = tmp_path / "stale.dump"
    bak.write_bytes(b"PGDMP")
    stale_time = time.time() - (BACKUP_MAX_AGE_HOURS + 1) * 3600
    os.utime(bak, (stale_time, stale_time))
    args = make_args(tmp_path, root, create_backup=False, backup_file=bak)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    rc = apply_main(args, executor=FakeExecutor())
    assert rc == 3


def test_apply_future_backup_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    bak = tmp_path / "future.dump"
    bak.write_bytes(b"PGDMP")
    future_time = time.time() + 3600
    os.utime(bak, (future_time, future_time))
    args = make_args(tmp_path, root, create_backup=False, backup_file=bak)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    rc = apply_main(args, executor=FakeExecutor())
    assert rc == 3


def test_apply_backup_dbname_mismatch_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    executor = FakeExecutor()
    executor.backup_list_output = ";\n;     dbname: wrong_db\n;\n"
    rc = apply_main(args, executor=executor)
    assert rc == 3


def test_apply_backup_missing_dbname_header_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    executor = FakeExecutor()
    executor.backup_list_output = ";\n;     TOC Entries: 1\n;\n"
    rc = apply_main(args, executor=executor)
    assert rc == 3


def test_apply_record_dir_inside_repo_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    repo_sub = scripts.apply_migrations.ROOT / "scripts"
    args = make_args(tmp_path, root, create_records=False, record_dir=repo_sub)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    rc = apply_main(args, executor=FakeExecutor())
    assert rc == 3


def test_apply_record_dir_missing_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root, create_records=False, record_dir=tmp_path / "missing_records")
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    rc = apply_main(args, executor=FakeExecutor())
    assert rc == 3


def test_apply_missing_psql_returns_3(inventory, tmp_path):
    root, manifest = inventory
    row = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": row["sha256"], "proof": "x"}})
    executor = FakeExecutor()
    executor.psql_available = False
    rc = apply_main(args, executor=executor)
    assert rc == 3
    assert list(args.record_dir.glob("apply-*.json")) == []


def test_apply_psql_meta_command_returns_3(inventory, tmp_path):
    root, manifest = inventory
    sql_path = root / "migrations" / FIRST
    sql_path.write_bytes(b"SELECT 1;\n\\i other.sql\n")
    digest = checker.checksum(sql_path)
    add_migration(inventory, FIRST, sha256_digest=digest)
    sql_path.write_bytes(b"SELECT 1;\n\\i other.sql\n")
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": digest, "proof": "x"}})
    executor = FakeExecutor()
    rc = apply_main(args, executor=executor)
    assert rc == 3
    assert executor.execute_calls == []
    assert list(args.record_dir.glob("apply-*.json")) == []


def test_executor_psql_args_by_transaction_mode(monkeypatch):
    recorded = []

    class FakeProc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, input=None, env=None, capture_output=None, text=None, check=None):
        recorded.append((args, input, env))
        return FakeProc()

    monkeypatch.setattr("scripts.apply_migrations.subprocess.run", fake_run)
    executor = MigrationExecutor()
    payload = b"SELECT 1;\n"

    rc1, _ = executor.execute_bytes(payload, "file")
    rc2, _ = executor.execute_bytes(payload, "psql-single")

    assert rc1 == 0 and rc2 == 0
    assert "--single-transaction" not in recorded[0][0]
    assert "--single-transaction" in recorded[1][0]
    for args, inp, env in recorded:
        assert "--no-password" in args
        assert "-X" in args
        assert "ON_ERROR_STOP=1" in args
        assert "VERBOSITY=sqlstate" in args
        assert args[-2:] == ["-f", "-"]
        assert inp == payload
        assert env["PGCLIENTENCODING"] == "UTF8"


def test_apply_stdin_bytes_match_hashed_bytes(inventory, tmp_path):
    root, manifest = inventory
    raw_crlf = b"CREATE TABLE t1 (id int);\r\n"
    sql_path = root / "migrations" / FIRST
    sql_path.write_bytes(raw_crlf)
    expected_bytes = raw_crlf.replace(b"\r\n", b"\n")
    expected_sha = sha256(expected_bytes).hexdigest()
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "precheck_017_users.sql").write_text("-- precheck\n", encoding="utf-8")
    row = {
        "file": FIRST,
        "requires": [],
        "sha256": expected_sha,
        "transaction": "psql-single",
        "environments": ["dev"],
        "prechecks": ["scripts/precheck_017_users.sql"],
    }
    manifest["migrations"].append(row)
    save_manifest(root, manifest)

    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": expected_sha, "proof": "catalog"}})
    executor = FakeExecutor()
    rc = apply_main(args, executor=executor)
    assert rc == 0
    assert len(executor.execute_calls) == 1
    sent_bytes, sent_tx = executor.execute_calls[0]
    assert sent_bytes == expected_bytes
    assert sha256(sent_bytes).hexdigest() == expected_sha
    assert sent_tx == "psql-single"

    run_files = list(args.record_dir.glob("apply-*.json"))
    assert len(run_files) == 1
    record = json.loads(run_files[0].read_text(encoding="utf-8"))
    assert record["type"] == "sql-apply-run"
    assert record["files"][0]["prechecks"] == ["scripts/precheck_017_users.sql"]
    assert record["files"][0]["state"] == "absent"
    assert record["files"][0]["proof"] == "catalog"
    assert record["files"][0]["result"] == "applied"


def test_apply_failure_stops_next_file(inventory, tmp_path):
    root, manifest = inventory
    r1 = add_migration(inventory, FIRST, transaction="file")
    r2 = add_migration(inventory, SECOND, transaction="psql-single")
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(
        args.evidence,
        "dev",
        {
            FIRST: {"state": "absent", "sha256": r1["sha256"], "proof": "catalog"},
            SECOND: {"state": "absent", "sha256": r2["sha256"], "proof": "catalog"},
        },
    )
    executor = FakeExecutor()
    executor.results = [(1, "42P01")]
    rc = apply_main(args, executor=executor)
    assert rc == 2
    assert len(executor.execute_calls) == 1

    run_files = list(args.record_dir.glob("apply-*.json"))
    assert len(run_files) == 1
    record = json.loads(run_files[0].read_text(encoding="utf-8"))
    assert len(record["files"]) == 1
    assert record["files"][0]["file"] == FIRST
    assert record["files"][0]["result"] == "failed"
    assert record["files"][0]["sqlstate"] == "42P01"


def test_extract_sqlstate_from_verbosity_sqlstate_and_null_fallback():
    assert extract_sqlstate("ERROR:  42P01\n") == "42P01"
    assert extract_sqlstate("psql:stdin:1: ERROR:  23505\n") == "23505"
    assert extract_sqlstate("SQLSTATE [42601] syntax error") == "42601"
    assert extract_sqlstate("generic failure without code") is None


def test_apply_resume_refused_on_started(inventory, tmp_path):
    root, manifest = inventory
    r1 = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": r1["sha256"], "proof": "catalog"}})
    prior = {
        "type": "sql-apply-run",
        "environment": "dev",
        "files": [{"file": FIRST, "result": "started", "started_at": "2026-01-01T00:00:00Z", "finished_at": None}],
    }
    (args.record_dir / "apply-20260101T000000Z-100.json").write_text(json.dumps(prior), encoding="utf-8")
    executor = FakeExecutor()
    rc = apply_main(args, executor=executor)
    assert rc == 1
    assert executor.execute_calls == []


def test_apply_corrupted_record_file_stops(inventory, tmp_path):
    root, manifest = inventory
    r1 = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": r1["sha256"], "proof": "catalog"}})
    (args.record_dir / "apply-20260101T000000Z-100.json").write_text("{corrupted json", encoding="utf-8")
    executor = FakeExecutor()
    rc = apply_main(args, executor=executor)
    assert rc == 1
    assert executor.execute_calls == []


def test_apply_checksum_mismatch_stops(inventory, tmp_path):
    root, manifest = inventory
    path1 = root / "migrations" / FIRST
    path1.write_bytes(b"CREATE TABLE t1 (id int);\n")
    real_digest = checker.checksum(path1)
    row = {
        "file": FIRST,
        "requires": [],
        "sha256": "f" * 64,
        "transaction": "file",
        "environments": ["dev"],
        "prechecks": [],
    }
    manifest["migrations"].append(row)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": real_digest, "proof": "catalog"}})
    executor = FakeExecutor()
    rc = apply_main(args, executor=executor)
    assert rc == 1
    assert executor.execute_calls == []


def test_apply_output_and_record_omit_secrets_and_raw_errors(inventory, tmp_path, capsys, monkeypatch):
    root, manifest = inventory
    r1 = add_migration(inventory, FIRST)
    save_manifest(root, manifest)
    args = make_args(tmp_path, root)
    write_evidence(args.evidence, "dev", {FIRST: {"state": "absent", "sha256": r1["sha256"], "proof": "catalog"}})

    secret_val = "SUPER_SECRET_PASSWORD_123"
    raw_err_detail = "DETAIL:  Key (email)=(sensitive@example.com) already exists."
    monkeypatch.setenv("PGPASSWORD", secret_val)

    class FakeProc:
        returncode = 1
        stdout = b""
        stderr = f"ERROR:  23505\n{raw_err_detail}\n".encode("utf-8")

    def fake_run(cmd_args, input=None, env=None, capture_output=None, text=None, timeout=None, check=None, **kwargs):
        if cmd_args[0] == "git":
            class GitProc:
                returncode = 0
                stdout = "abc1234\n"
                stderr = ""
            return GitProc()
        if "pg_restore" in cmd_args[0]:
            p = FakeProc()
            p.returncode = 0
            p.stdout = b";     dbname: toi_walk_test_113\n"
            p.stderr = b""
            return p
        if "-c" in cmd_args:
            p = FakeProc()
            p.returncode = 0
            p.stdout = b"toi_walk_test_113|1234567890\n"
            p.stderr = b""
            return p
        return FakeProc()

    monkeypatch.setattr("scripts.apply_migrations.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("scripts.apply_migrations.subprocess.run", fake_run)

    rc = apply_main(args, executor=MigrationExecutor())
    assert rc == 2
    captured = capsys.readouterr()
    combined_out = captured.out + captured.err
    assert secret_val not in combined_out
    assert raw_err_detail not in combined_out
    assert "sensitive@example.com" not in combined_out

    run_files = list(args.record_dir.glob("apply-*.json"))
    assert len(run_files) == 1
    record_text = run_files[0].read_text(encoding="utf-8")
    assert secret_val not in record_text
    assert raw_err_detail not in record_text
    assert "sensitive@example.com" not in record_text
    assert '"sqlstate": "23505"' in record_text


# ===== 1-D: pg16-public-v1 fingerprint, schema-record, model-init, equivalence =====


def test_pg16_public_v1_fingerprint_matches_deployment_common_rule():
    catalog = [
        ["table", "users", "r", False, False, None, None],
        ["column", "users", 1, "id", "integer", True, "", "", None, "기본"],
        ["extension", "plpgsql", "pg_catalog", "1.0"],
    ]
    # Exact formula from scripts/deployment_common.sh schema_evidence()
    canonical = sorted(json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in catalog)
    expected = sha256(("\n".join(canonical) + "\n").encode("ascii")).hexdigest()

    assert Pg16PublicV1SnapshotProvider.compute_fingerprint(catalog) == expected
    assert Pg16PublicV1SnapshotProvider.compute_fingerprint(list(reversed(catalog))) == expected


def test_build_schema_record_and_emit_on_apply(inventory, tmp_path):
    root, manifest = inventory
    r1 = add_migration(inventory, FIRST)
    r2 = add_migration(inventory, SECOND, requires=[FIRST])
    save_manifest(root, manifest)

    schema_out = tmp_path / "schema-record.json"
    args = make_args(tmp_path, root, schema_record_out=schema_out)
    write_evidence(
        args.evidence,
        "dev",
        {
            FIRST: {"state": "applied", "sha256": r1["sha256"], "proof": "prior-run"},
            SECOND: {"state": "absent", "sha256": r2["sha256"], "proof": "catalog"},
        },
    )

    class FakeSnapshotProvider:
        def capture_snapshot(self, schema="public"):
            return {
                "version": 160016,
                "identity": {
                    "system_identifier": "1234567890",
                    "database": "toi_walk_test_113",
                    "database_oid": "16385",
                },
                "catalog": [["table", "users", "r", False, False, None, None]],
            }

        def capture_fingerprint(self, schema="public"):
            return Pg16PublicV1SnapshotProvider.compute_fingerprint(self.capture_snapshot(schema)["catalog"])

    executor = FakeExecutor()
    rc = apply_main(args, executor=executor, snapshot_provider=FakeSnapshotProvider())
    assert rc == 0
    assert schema_out.is_file()
    record = json.loads(schema_out.read_text(encoding="utf-8"))
    assert set(record) == {"db_identity", "algorithm", "fingerprint", "migrations"}
    assert record["algorithm"] == ALGORITHM_PG16_PUBLIC_V1
    assert record["db_identity"] == {
        "system_identifier": "1234567890",
        "database": "toi_walk_test_113",
        "database_oid": "16385",
    }
    assert record["migrations"] == [
        {"file": FIRST, "sha256": r1["sha256"]},
        {"file": SECOND, "sha256": r2["sha256"]},
    ]


def test_model_init_evidence_distinct_from_sql_apply_run(tmp_path):
    rec_dir = tmp_path / "records"
    rec_dir.mkdir()
    db_identity = {
        "system_identifier": "1234567890",
        "database": "toi_walk_test_113",
        "database_oid": "16385",
    }
    init_path = create_model_init_evidence(rec_dir, db_identity, "a" * 64, commit="deadbeef")
    assert init_path.name.startswith("init-")
    data = json.loads(init_path.read_text(encoding="utf-8"))
    assert data["type"] == "model-init-evidence"
    assert data["algorithm"] == ALGORITHM_PG16_PUBLIC_V1
    assert data["fingerprint"] == "a" * 64
    assert "migrations" not in data
    # init-*.json does not interfere with apply-*.json validation
    assert validate_record_dir(rec_dir) is None


def test_compare_catalog_snapshots_detects_equivalence_and_diffs():
    a = [["table", "users", "r"], ["column", "users", 1, "id"]]
    b = [["column", "users", 1, "id"], ["table", "users", "r"]]
    ok, only_a, only_b = compare_catalog_snapshots(a, b)
    assert ok is True and only_a == [] and only_b == []

    c = [["table", "users", "r"], ["table", "dogs", "r"]]
    ok2, only_a2, only_b2 = compare_catalog_snapshots(a, c)
    assert ok2 is False
    assert len(only_a2) == 1
    assert len(only_b2) == 1

