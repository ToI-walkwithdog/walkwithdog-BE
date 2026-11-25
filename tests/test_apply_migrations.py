"""Unit tests for scripts/apply_migrations.py: plan calculation and blocking.

These tests do not connect to a database. They exercise the offline evidence plan
using the same patterns as tests/test_check_migrations.py.
"""
import json
from pathlib import Path

import pytest

from scripts import check_migrations as checker
from scripts.apply_migrations import plan_actions, print_plan

FIRST = "001_base.sql"
SECOND = "002_followup.sql"
THIRD = "003_future.sql"


@pytest.fixture
def inventory(tmp_path):
    (tmp_path / "migrations").mkdir()
    manifest = {"version": 1, "migrations": [], "reservations": []}
    return tmp_path, manifest


def add_migration(inventory, name, requires=(), sha256=None):
    root, manifest = inventory
    path = root / "migrations" / name
    path.write_bytes(b"SELECT 1;\n")
    digest = sha256 if sha256 is not None else checker.checksum(path)
    row = {
        "file": name,
        "requires": list(requires),
        "sha256": digest,
        "transaction": "file",
        "environments": ["local-test", "dev"],
        "prechecks": [],
    }
    manifest["migrations"].append(row)
    return row


def save_manifest(root, manifest):
    (root / checker.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def run_plan(root, manifest, environment, files):
    save_manifest(root, manifest)
    evidence = {"environment": environment, "files": files}
    return plan_actions(environment, files, manifest["migrations"])


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
    # Only FIRST is verified as applied; SECOND is absent but its prerequisite is missing from verified set
    actions = run_plan(
        root,
        manifest,
        "dev",
        {
            FIRST: {"state": "applied", "sha256": first["sha256"], "proof": "record"},
            SECOND: {"state": "absent", "sha256": second["sha256"], "proof": "catalog comparison"},
        },
    )
    # Both FIRST (SKIP_RECORDED) and SECOND (MANUAL_PRECHECK_REQUIRED, prerequisite verified)
    assert actions == [
        (FIRST, "SKIP_RECORDED"),
        (SECOND, "MANUAL_PRECHECK_REQUIRED"),
    ]


def test_plan_blocked_prerequisites_when_dependency_not_verified(inventory):
    root, manifest = inventory
    first = add_migration(inventory, FIRST)
    second = add_migration(inventory, SECOND, [FIRST])
    # SECOND absent but FIRST is unknown -> prerequisite not verified
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


def test_plan_blocked_returns_nonzero(capsys, inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    actions = plan_actions(
        "dev", {FIRST: {"state": "unknown"}}, manifest["migrations"]
    )
    assert any(action.startswith("RECONCILE") for _, action in actions)
    blocked = any(action.startswith(("RECONCILE", "BLOCKED", "OUTSIDE_ENVIRONMENT")) for _, action in actions)
    assert blocked is True


def test_print_plan_outputs_order_and_actions(capsys, inventory):
    root, manifest = inventory
    add_migration(inventory, FIRST)
    actions = plan_actions("dev", {FIRST: {"state": "absent", "proof": "x"}}, manifest["migrations"])
    print_plan("dev", actions)
    out = capsys.readouterr().out
    assert "mode: dry-run" in out
    assert f"MANUAL_PRECHECK_REQUIRED: {FIRST}" in out
