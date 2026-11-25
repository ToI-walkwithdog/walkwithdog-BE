"""Apply migrations offline: static verification, evidence-based plan, dry-run output.

This tool does NOT connect to a database. It reads the manifest, verifies checksums
and ordering, loads an offline evidence file, and prints the per-file execution plan.
The default mode is dry-run: only the plan is printed, no database is touched.

Protected functions are imported from scripts.check_migrations and not modified.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "migrations" / "manifest.json"

try:  # Allow running both as `python scripts/apply_migrations.py` and `python -m scripts.apply_migrations`
    from scripts import check_migrations as checker
except ModuleNotFoundError:  # pragma: no cover - fallback for direct script execution
    sys.path.insert(0, str(ROOT))
    from scripts import check_migrations as checker  # type: ignore[no-redef]


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_commit() -> str:
    """Return the short Git commit of the repository, or 'unknown' if unavailable."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def load_evidence(path: Path) -> dict:
    return checker.read_json(path)


def plan_actions(environment: str, evidence_records: dict, migrations: list[dict]):
    """Return (actions, blocked) using checker.evidence_plan without reimplementing it."""
    return checker.evidence_plan({"environment": environment, "files": evidence_records}, migrations)


def print_plan(environment: str, actions: list[tuple[str, str]]) -> None:
    print(f"evidence environment: {environment}")
    print("mode: dry-run (no database connection, no changes)")
    for order, (name, action) in enumerate(actions, 1):
        print(f"{order:02d}. {action}: {name}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="offline JSON evidence file (--evidence format)",
    )
    args = parser.parse_args(argv)

    try:
        manifest = checker.read_json(args.root / "migrations" / "manifest.json")
        errors, conflicts = checker.validate_inventory(args.root, manifest)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for problem in errors:
        print(f"ERROR: {problem}", file=sys.stderr)
    for prefix, names in conflicts:
        print(f"BLOCKED FUTURE MIGRATION: reserved number {prefix}: {', '.join(sorted(names))}")
    if errors:
        return 1

    try:
        evidence = load_evidence(args.evidence)
        environment = evidence["environment"]
        records = evidence["files"]
        migrations = manifest["migrations"]
        actions = plan_actions(environment, records, migrations)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    blocked = any(
        action.startswith(("RECONCILE", "BLOCKED", "OUTSIDE_ENVIRONMENT")) for _, action in actions
    )
    print_plan(environment, actions)

    commit = git_commit()
    print(f"git_commit: {commit}")
    print(f"started_at: {utc_now()}")
    print("OFFLINE EVIDENCE ONLY: confirm preconditions and approval before any database apply")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
