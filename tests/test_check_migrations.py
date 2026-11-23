import json
from pathlib import Path
import socket
import sqlite3
import subprocess

import pytest

from scripts import check_migrations as checker


FIRST = "001_base.sql"
SECOND = "002_followup.sql"
FUTURE = "003_future.sql"


@pytest.fixture
def inventory(tmp_path):
    (tmp_path / "migrations").mkdir()
    manifest = {"version": 1, "migrations": [], "reservations": []}
    return tmp_path, manifest


def add_migration(inventory, name, requires=()):
    root, manifest = inventory
    path = root / "migrations" / name
    path.write_bytes(b"SELECT 1;\n")
    row = {
        "file": name,
        "requires": list(requires),
        "sha256": checker.checksum(path),
        "transaction": "file",
        "environments": ["local-test", "dev"],
        "prechecks": [],
    }
    manifest["migrations"].append(row)
    return row


def add_reservation(manifest, name, state="unmerged"):
    manifest["reservations"].append(
        {"file": name, "state": state, "source": "test branch", "requires": [FIRST]}
    )


def save_manifest(root, manifest):
    (root / checker.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def test_repository_inventory():
    manifest = checker.read_json(checker.ROOT / checker.MANIFEST)
    errors, _ = checker.validate_inventory(checker.ROOT, manifest)
    assert errors == []


def test_normal_inventory_and_line_endings(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    add_migration(inventory, SECOND, [FIRST])
    (root / "migrations" / FIRST).write_bytes(b"SELECT 1;\r\n")
    assert checker.validate_inventory(root, manifest) == ([], [])


def test_new_duplicate_number_is_rejected(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    add_migration(inventory, "001_duplicate.sql")
    errors, _ = checker.validate_inventory(root, manifest)
    assert any("duplicate dev number 001" in error for error in errors)


def test_legacy_number_pairs_are_exact(inventory):
    root, manifest = inventory
    for name in (
        "017_f10_behavior_marker.sql",
        "017_remove_user_email.sql",
        "018_f11_walk_overtime.sql",
        "018_user_consents.sql",
    ):
        add_migration(inventory, name)
    assert checker.validate_inventory(root, manifest) == ([], [])

    add_migration(inventory, "018_extra.sql")
    errors, _ = checker.validate_inventory(root, manifest)
    assert any("duplicate dev number 018" in error for error in errors)


def test_file_missing_or_unlisted(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    (root / "migrations" / FIRST).unlink()
    (root / "migrations" / SECOND).write_text("SELECT 1;", encoding="utf-8")
    errors, _ = checker.validate_inventory(root, manifest)
    assert errors == [f"missing SQL file: {FIRST}", f"SQL file not listed for dev: {SECOND}"]


def test_dev_cannot_depend_on_reserved_file(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    add_migration(inventory, SECOND, [FUTURE])
    add_reservation(manifest, FUTURE)
    errors, _ = checker.validate_inventory(root, manifest)
    assert f"{SECOND}: dev migration depends on unmerged file {FUTURE}" in errors


def test_unknown_dependency(inventory):
    root, manifest = inventory
    add_migration(inventory, SECOND, [FIRST])
    errors, _ = checker.validate_inventory(root, manifest)
    assert errors == [f"{SECOND}: unknown prerequisite {FIRST}"]


def test_cycle(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST, [SECOND])
    add_migration(inventory, SECOND, [FIRST])
    errors, _ = checker.validate_inventory(root, manifest)
    assert len(errors) == 1
    assert errors[0].startswith("dependency cycle:")
    assert FIRST in errors[0] and SECOND in errors[0]


def test_wrong_order(inventory):
    root, manifest = inventory
    add_migration(inventory, SECOND, [FIRST])
    add_migration(inventory, FIRST)
    errors, _ = checker.validate_inventory(root, manifest)
    assert errors == [f"{SECOND}: prerequisite {FIRST} must precede it"]


def test_modified_file_and_missing_precheck(inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)["prechecks"] = ["scripts/missing.sql"]
    (root / "migrations" / FIRST).write_bytes(b"SELECT 2;\n")
    errors, _ = checker.validate_inventory(root, manifest)
    assert errors == [
        f"{FIRST}: SHA-256 mismatch",
        f"{FIRST}: missing or invalid precheck script scripts/missing.sql",
    ]


def test_unmerged_sql_is_never_listed_for_apply(inventory, capsys):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    add_reservation(manifest, "001_onboarding.sql")
    add_reservation(manifest, FUTURE, "reserved")
    save_manifest(root, manifest)
    args = ["--root", str(root)]
    assert checker.main(args) == 0
    output = capsys.readouterr().out
    assert f"01. {FIRST}" in output and "02. " not in output
    assert "BLOCKED FUTURE MIGRATION: reserved number 001" in output
    assert f"RESERVED {FUTURE}" in output
    assert checker.main(args + ["--check-reservations"]) == 1
    assert checker.main(args + ["--propose", "004_new.sql"]) == 0
    assert checker.main(args + ["--propose", "003_new.sql"]) == 1
    assert checker.main(args + ["--propose", "002_new.sql"]) == 1


def test_evidence_rechecks_history_and_dependencies(inventory):
    _, manifest = inventory
    first = add_migration(inventory, FIRST)
    add_migration(inventory, SECOND, [FIRST])
    evidence = {"environment": "dev", "files": {
        FIRST: {"state": "applied", "sha256": first["sha256"], "proof": "first record"},
        SECOND: {"state": "absent", "proof": "second catalog comparison"},
    }}
    assert checker.evidence_plan(evidence, manifest["migrations"]) == [
        (FIRST, "SKIP_RECORDED"), (SECOND, "MANUAL_PRECHECK_REQUIRED"),
    ]

    evidence["files"][FIRST]["sha256"] = "0" * 64
    assert checker.evidence_plan(evidence, manifest["migrations"]) == [
        (FIRST, "RECONCILE_CHECKSUM"), (SECOND, f"BLOCKED_PREREQUISITES ({FIRST})"),
    ]


@pytest.mark.parametrize("state", ["schema-match", "partial", "unknown"])
def test_evidence_does_not_infer_application(inventory, state):
    _, manifest = inventory
    add_migration(inventory, FIRST)
    evidence = {"environment": "dev", "files": {
        FIRST: {"state": state, "proof": "inspection record"},
    }}
    assert checker.evidence_plan(evidence, manifest["migrations"]) == [
        (FIRST, "RECONCILE_" + state.upper().replace("-", "_")),
    ]


def test_dry_run_does_not_connect_or_change_a_database(inventory, capsys, monkeypatch):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    sql = root / "migrations" / FIRST
    sql.write_bytes(b"DROP TABLE sentinel;\n")
    manifest["migrations"][0]["sha256"] = checker.checksum(sql)
    save_manifest(root, manifest)

    db = root / "database.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER)")
    before = db.read_bytes()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")

    def fail_on_execution(*args, **kwargs):
        pytest.fail("dry run attempted to connect or execute another program")

    with monkeypatch.context() as network_guard:
        network_guard.setattr(socket, "socket", fail_on_execution)
        network_guard.setattr(sqlite3, "connect", fail_on_execution)
        network_guard.setattr(subprocess, "Popen", fail_on_execution)
        assert checker.main(["--root", str(root)]) == 0

    assert db.read_bytes() == before
    assert "DATABASE STATE UNKNOWN" in capsys.readouterr().out


def test_new_sql_must_move_from_reservation_into_dev_inventory(inventory, capsys):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    add_reservation(manifest, SECOND, "reserved")
    (root / "migrations" / SECOND).write_bytes(b"SELECT 1;\n")
    args = ["--root", str(root), "--check-reservations-for", SECOND]

    save_manifest(root, manifest)
    assert checker.main(args) == 1
    assert f"SQL file not listed for dev: {SECOND}" in capsys.readouterr().err

    row = add_migration(inventory, SECOND, [FIRST])
    digest = row["sha256"]
    save_manifest(root, manifest)
    assert checker.main(args) == 1
    assert f"filename listed more than once: {SECOND}" in capsys.readouterr().err

    manifest["reservations"].clear()
    row["sha256"] = "0" * 64
    save_manifest(root, manifest)
    assert checker.main(args) == 1
    assert f"{SECOND}: SHA-256 mismatch" in capsys.readouterr().err

    row["sha256"] = digest
    save_manifest(root, manifest)
    assert checker.main(args) == 0
    output = capsys.readouterr().out
    assert f"02. {SECOND}" in output
    assert f"RESERVED {SECOND}" not in output
    assert "DATABASE STATE UNKNOWN" in output


def test_candidate_reservation_check_blocks_only_selected_collisions(inventory, capsys):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    add_migration(inventory, SECOND, [FIRST])
    add_reservation(manifest, "001_colliding.sql")
    save_manifest(root, manifest)
    args = ["--root", str(root), "--check-reservations-for", SECOND]

    assert checker.main(args) == 0
    assert "BLOCKED FUTURE MIGRATION: reserved number 001" in capsys.readouterr().out
    assert checker.main(args + ["--check-reservations-for", "001_colliding.sql"]) == 1

    add_reservation(manifest, "002_another_branch.sql")
    save_manifest(root, manifest)
    assert checker.main(args) == 1
    assert "BLOCKED FUTURE MIGRATION: reserved number 002" in capsys.readouterr().out


def test_candidate_reservation_check_rejects_unlisted_target(inventory, capsys):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    save_manifest(root, manifest)

    assert checker.main(["--root", str(root), "--check-reservations-for", FUTURE]) == 1
    assert f"file selected for reservation check is not listed: {FUTURE}" in capsys.readouterr().err
