"""Apply migrations with offline evidence plan, safety guards, and run records.

NOTE: Real dev/production DB execution still requires the 16-5 read-only
inspection evidence and 16-6 explicit operator approval workflow.

This tool reads migrations/manifest.json and an offline --evidence JSON file,
computes the per-file plan via scripts.check_migrations.evidence_plan(), and
defaults to dry-run output without touching any database.

When --apply is given, it enforces safety guards (blocked-plan rejection,
checksum and psql meta-command verification, --record-dir outside repo,
pg_restore --list backup validation with dbname match, psql availability,
and DB identity verification via psql current_database() / pg_control_system())
before creating an exclusive run record and executing each migration file.

In 1-D, Pg16PublicV1SnapshotProvider computes the pg16-public-v1 catalog
fingerprint matching scripts/deployment_common.sh, supports --schema-record-out,
distinguishes model-init-evidence (init-*.json) from sql-apply-run (apply-*.json),
and provides catalog equivalence comparison.

Note on DB permissions: querying pg_control_system() requires superuser,
the pg_monitor role, or explicit GRANT EXECUTE ON FUNCTION pg_control_system()
(pg_read_all_stats alone is not sufficient).

Protected functions are imported from scripts.check_migrations and not modified.
"""
from __future__ import annotations

import argparse
import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("migrations/manifest.json")
BACKUP_MAX_AGE_HOURS = 24

# psql meta-command at start of line (e.g., \i, \ir, \include, \c, \!)
META_COMMAND_RE = re.compile(r"(?m)^\s*\\")

# pg_restore --list header line: ";     dbname: <name>"
PG_RESTORE_DBNAME_RE = re.compile(r"^;\s+dbname:\s+(.+?)\s*$", re.MULTILINE)

# SQLSTATE extraction from psql stderr when invoked with `-v VERBOSITY=sqlstate`.
# With VERBOSITY=sqlstate, psql/libpq emits the 5-character SQLSTATE code on an
# error severity line (e.g. "ERROR:  42P01" or "FATAL:  28P01") or in
# "SQLSTATE [42P01]" format depending on client formatting.
# Only the 5-char [0-9A-Z]{5} code is extracted; raw error text is never stored.
SQLSTATE_RE = re.compile(
    r"(?:SQLSTATE\s*\[([0-9A-Z]{5})\]|(?:ERROR|FATAL|PANIC):\s+([0-9A-Z]{5})\b)"
)

try:  # Allow running both as `python scripts/apply_migrations.py` and `python -m scripts.apply_migrations`
    from scripts import check_migrations as checker
except ModuleNotFoundError:  # pragma: no cover - fallback for direct script execution
    sys.path.insert(0, str(ROOT))
    from scripts import check_migrations as checker  # type: ignore[no-redef]


class ApplyMigrationsError(RuntimeError):
    pass


class SafetyGuardError(ApplyMigrationsError):
    pass


class ExecutionError(ApplyMigrationsError):
    pass


ALGORITHM_PG16_PUBLIC_V1 = "pg16-public-v1"
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Catalog snapshot SQL matching scripts/deployment_common.sh catalog_snapshot().
# Format parameter {schema_literal} is a validated SQL identifier literal (default 'public').
CATALOG_SNAPSHOT_SQL_TEMPLATE = """\
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '12s';
SET LOCAL search_path = pg_catalog;
WITH objects(item) AS (
  SELECT jsonb_build_array('table', c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
     pg_get_partkeydef(c.oid), CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound,c.oid) END)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='{schema}' AND c.relkind IN ('r','p','v','m','f','S')
  UNION ALL
  SELECT jsonb_build_array('column', c.relname, a.attnum, a.attname,
     format_type(a.atttypid,a.atttypmod), a.attnotnull,a.attidentity,a.attgenerated,
     pg_get_expr(d.adbin,d.adrelid), col.collname)
    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
    LEFT JOIN pg_collation col ON col.oid=a.attcollation
    WHERE n.nspname='{schema}' AND c.relkind IN ('r','p','v','m','f')
      AND a.attnum>0 AND NOT a.attisdropped
  UNION ALL
  SELECT jsonb_build_array('constraint', c.relname, k.conname, k.convalidated, pg_get_constraintdef(k.oid))
    FROM pg_constraint k JOIN pg_namespace n ON n.oid=k.connamespace
    LEFT JOIN pg_class c ON c.oid=k.conrelid WHERE n.nspname='{schema}'
  UNION ALL
  SELECT jsonb_build_array('index', c.relname, i.indisvalid, pg_get_indexdef(c.oid))
    FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='{schema}'
  UNION ALL
  SELECT jsonb_build_array('enum', t.typname, e.enumsortorder, e.enumlabel)
    FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid
    JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='{schema}'
  UNION ALL
  SELECT jsonb_build_array('sequence', c.relname, s.seqtypid::regtype::text, s.seqstart, s.seqincrement,
      s.seqmin, s.seqmax, s.seqcache, s.seqcycle, owned.relname, a.attname)
    FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid
      AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') AND d.refobjsubid>0
    LEFT JOIN pg_class owned ON owned.oid=d.refobjid
    LEFT JOIN pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
    WHERE n.nspname='{schema}'
  UNION ALL
  SELECT jsonb_build_array('routine', p.proname, pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid))
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='{schema}' AND p.prokind IN ('f','p','w')
  UNION ALL
  SELECT jsonb_build_array('view', c.relname, pg_get_viewdef(c.oid), c.reloptions)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='{schema}' AND c.relkind IN ('v','m')
  UNION ALL
  SELECT jsonb_build_array('trigger', c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid))
    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='{schema}' AND NOT t.tgisinternal
  UNION ALL
  SELECT jsonb_build_array('policy', p.tablename, p.policyname, p.permissive, p.roles,p.cmd,p.qual,p.with_check)
    FROM pg_policies p WHERE p.schemaname='{schema}'
  UNION ALL
  SELECT jsonb_build_array('extension', e.extname, n.nspname, e.extversion)
    FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
)
SELECT jsonb_build_object('version', current_setting('server_version_num')::int,
    'identity', jsonb_build_object('system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
                                'database', current_database(), 'database_oid',
                                (SELECT oid::text FROM pg_database WHERE datname=current_database())),
    'catalog', COALESCE((SELECT jsonb_agg(item) FROM objects), '[]'::jsonb));
COMMIT;
"""


class CatalogSnapshotProvider(Protocol):
    """Injectable interface for catalog snapshot / fingerprint verification."""

    def capture_snapshot(self, schema: str = "public") -> dict:
        """Return {'version': int, 'identity': dict, 'catalog': list}."""
        ...

    def capture_fingerprint(self, schema: str = "public") -> str:
        """Return a normalized catalog fingerprint string (e.g. pg16-public-v1)."""
        ...


class Pg16PublicV1SnapshotProvider:
    """pg16-public-v1 catalog snapshot provider matching scripts/deployment_common.sh."""

    def __init__(self, psql_path: str = "psql"):
        self.psql_path = psql_path

    @staticmethod
    def build_snapshot_sql(schema: str = "public") -> str:
        if not SAFE_IDENTIFIER_RE.fullmatch(schema):
            raise SafetyGuardError(f"unsafe schema identifier: {schema!r}")
        return CATALOG_SNAPSHOT_SQL_TEMPLATE.format(schema=schema)

    @staticmethod
    def compute_fingerprint(catalog: list) -> str:
        canonical = sorted(json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in catalog)
        return sha256(("\n".join(canonical) + "\n").encode("ascii")).hexdigest()

    def capture_snapshot(self, schema: str = "public") -> dict:
        sql = self.build_snapshot_sql(schema)
        proc_env = os.environ.copy()
        proc_env["PGCLIENTENCODING"] = "UTF8"
        args = [
            self.psql_path,
            "-X",
            "--no-password",
            "--no-align",
            "--tuples-only",
            "--quiet",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ]
        try:
            result = subprocess.run(
                args,
                input=sql.encode("utf-8"),
                env=proc_env,
                capture_output=True,
                text=False,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SafetyGuardError("catalog snapshot query failed") from exc
        if result.returncode != 0:
            raise SafetyGuardError("catalog snapshot query failed (verify connection and permissions)")
        stdout_text = result.stdout.decode("utf-8", errors="replace").strip()
        if not stdout_text:
            raise SafetyGuardError("empty catalog snapshot response")
        try:
            observed = json.loads(stdout_text)
        except ValueError as exc:
            raise SafetyGuardError("invalid catalog snapshot JSON") from exc
        if not isinstance(observed, dict) or not isinstance(observed.get("catalog"), list):
            raise SafetyGuardError("malformed catalog snapshot payload")
        return observed

    def capture_fingerprint(self, schema: str = "public") -> str:
        snapshot = self.capture_snapshot(schema=schema)
        return self.compute_fingerprint(snapshot["catalog"])


def build_schema_record(db_identity: dict, fingerprint: str, applied_migrations: list[dict]) -> dict:
    """Build a schema-record.json payload compatible with scripts/deployment_common.sh."""
    return {
        "db_identity": {
            "system_identifier": str(db_identity["system_identifier"]),
            "database": str(db_identity["database"]),
            "database_oid": str(db_identity["database_oid"]),
        },
        "algorithm": ALGORITHM_PG16_PUBLIC_V1,
        "fingerprint": fingerprint,
        "migrations": [
            {"file": row["file"], "sha256": row["sha256"]}
            for row in applied_migrations
        ],
    }


def create_model_init_evidence(
    record_dir: Path,
    db_identity: dict,
    fingerprint: str,
    commit: str | None = None,
) -> Path:
    """Write a model-init-evidence JSON file (init-*.json), distinct from sql-apply-run."""
    if is_repo_path(record_dir):
        raise SafetyGuardError("record_dir must be outside the repository")
    if not record_dir.is_dir():
        raise SafetyGuardError("record_dir does not exist")
    payload = {
        "type": "model-init-evidence",
        "algorithm": ALGORITHM_PG16_PUBLIC_V1,
        "git_commit": commit or git_commit(),
        "db_identity": {
            "system_identifier": str(db_identity["system_identifier"]),
            "database": str(db_identity["database"]),
            "database_oid": str(db_identity["database_oid"]),
        },
        "fingerprint": fingerprint,
        "initialized_at": utc_now(),
    }
    path = record_dir / f"init-{utc_timestamp_compact()}-{os.getpid()}.json"
    write_record_exclusive(path, payload)
    return path


def compare_catalog_snapshots(
    expected_catalog: list,
    actual_catalog: list,
) -> tuple[bool, list[str], list[str]]:
    """Compare two normalized catalog lists and return (is_equal, only_expected, only_actual)."""
    expected_set = {json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in expected_catalog}
    actual_set = {json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in actual_catalog}
    only_expected = sorted(expected_set - actual_set)
    only_actual = sorted(actual_set - expected_set)
    return (not only_expected and not only_actual), only_expected, only_actual


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_timestamp_compact() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def git_commit() -> str:
    """Return the short Git commit of the repository, or 'unknown' if unavailable."""
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
    """Return planned (file, action) pairs using checker.evidence_plan."""
    return checker.evidence_plan({"environment": environment, "files": evidence_records}, migrations)


def is_blocked_action(action: str) -> bool:
    return action.startswith(("RECONCILE", "BLOCKED", "OUTSIDE_ENVIRONMENT"))


def print_plan(environment: str, actions: list[tuple[str, str]], manifest_by_file: dict[str, dict] | None = None) -> None:
    print(f"evidence environment: {environment}")
    print("mode: dry-run (no database connection, no changes)")
    for order, (name, action) in enumerate(actions, 1):
        note_parts = []
        if manifest_by_file and name in manifest_by_file:
            row = manifest_by_file[name]
            note_parts.append(f"tx={row['transaction']}")
            if row.get("prechecks"):
                note_parts.append(f"prechecks={','.join(row['prechecks'])}")
        note = " ".join(note_parts) if note_parts else "-"
        print(f"{order:02d}. {name} - {action} - {note}")


def is_repo_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
        root_resolved = ROOT.resolve()
        return resolved == root_resolved or str(resolved).startswith(str(root_resolved) + os.sep)
    except (OSError, ValueError):
        return True


def write_record_exclusive(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def update_record(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def extract_sqlstate(stderr_text: str) -> str | None:
    match = SQLSTATE_RE.search(stderr_text)
    if not match:
        return None
    return match.group(1) or match.group(2)


def parse_backup_dbname(pg_restore_list_output: str) -> str | None:
    match = PG_RESTORE_DBNAME_RE.search(pg_restore_list_output)
    return match.group(1).strip() if match else None


def has_psql_meta_command(sql_text: str) -> bool:
    return META_COMMAND_RE.search(sql_text) is not None


class MigrationExecutor:
    """Subprocess wrapper around psql and pg_restore (injectable for unit tests)."""

    def __init__(self, psql_path: str = "psql", pg_restore_path: str = "pg_restore"):
        self.psql_path = psql_path
        self.pg_restore_path = pg_restore_path

    def check_psql_available(self) -> bool:
        return shutil.which(self.psql_path) is not None

    def check_pg_restore_available(self) -> bool:
        return shutil.which(self.pg_restore_path) is not None

    def inspect_backup_list(self, backup_path: Path) -> tuple[int, str]:
        proc_env = os.environ.copy()
        proc_env["PGCLIENTENCODING"] = "UTF8"
        try:
            result = subprocess.run(
                [self.pg_restore_path, "--list", "--no-password", str(backup_path)],
                env=proc_env,
                capture_output=True,
                text=False,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SafetyGuardError("pg_restore execution failed") from exc
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        return result.returncode, stdout_text

    def query_db_identity(self) -> tuple[str, str]:
        """Query current_database() and pg_control_system().system_identifier via psql."""
        proc_env = os.environ.copy()
        proc_env["PGCLIENTENCODING"] = "UTF8"
        args = [
            self.psql_path,
            "-X",
            "--no-password",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-F",
            "|",
            "-c",
            "SELECT current_database(), system_identifier FROM pg_control_system();",
        ]
        try:
            result = subprocess.run(
                args,
                env=proc_env,
                capture_output=True,
                text=False,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SafetyGuardError("psql identity query failed") from exc

        if result.returncode != 0:
            raise SafetyGuardError(
                "failed to query database identity (verify connection and pg_control_system() permission)"
            )
        stdout_text = result.stdout.decode("utf-8", errors="replace").strip()
        lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
        if len(lines) != 1 or "|" not in lines[0]:
            raise SafetyGuardError("unexpected database identity response")
        db_name, sys_id = lines[0].split("|", 1)
        if not db_name or not sys_id:
            raise SafetyGuardError("empty database identity values")
        return db_name, sys_id

    def execute_bytes(self, sql_bytes: bytes, transaction: str) -> tuple[int, str | None]:
        args = [
            self.psql_path,
            "-X",
            "--no-password",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "VERBOSITY=sqlstate",
            "-f",
            "-",
        ]
        if transaction == "psql-single":
            args.insert(2, "--single-transaction")

        proc_env = os.environ.copy()
        proc_env["PGCLIENTENCODING"] = "UTF8"

        try:
            result = subprocess.run(
                args,
                input=sql_bytes,
                env=proc_env,
                capture_output=True,
                text=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError("psql execution failed") from exc

        stderr_text = result.stderr.decode("utf-8", errors="replace")
        sqlstate = extract_sqlstate(stderr_text) if result.returncode != 0 else None
        return result.returncode, sqlstate


def validate_record_dir(record_dir: Path) -> int | None:
    """Validate --record-dir and scan apply-*.json for incomplete or corrupted runs."""
    if is_repo_path(record_dir):
        print("ERROR: --record-dir must be outside the repository", file=sys.stderr)
        return 3
    if not record_dir.is_dir():
        print("ERROR: --record-dir does not exist", file=sys.stderr)
        return 3

    for existing in sorted(record_dir.glob("apply-*.json")):
        try:
            data = checker.read_json(existing)
        except (OSError, ValueError):
            print(f"ERROR: corrupted or unreadable run record: {existing.name}", file=sys.stderr)
            return 1
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            print(f"ERROR: invalid run record structure: {existing.name}", file=sys.stderr)
            return 1
        for entry in data["files"]:
            if not isinstance(entry, dict):
                print(f"ERROR: invalid file entry in run record: {existing.name}", file=sys.stderr)
                return 1
            if entry.get("result") == "started" or (
                entry.get("started_at") is not None and entry.get("finished_at") is None
            ):
                print(
                    f"ERROR: previous run ({existing.name}) left {entry.get('file')} in 'started' state; refusing to resume",
                    file=sys.stderr,
                )
                return 1
    return None


def validate_backup_file(backup_path: Path, db_name: str, executor: MigrationExecutor) -> str | None:
    """Validate backup file age, format via pg_restore --list, and dbname header. Returns sha256 or None."""
    if not backup_path.is_file():
        print("ERROR: backup file does not exist", file=sys.stderr)
        return None
    if backup_path.stat().st_size == 0:
        print("ERROR: backup file is empty", file=sys.stderr)
        return None

    mtime = datetime.datetime.fromtimestamp(backup_path.stat().st_mtime, tz=datetime.timezone.utc)
    age_seconds = (datetime.datetime.now(datetime.timezone.utc) - mtime).total_seconds()
    if age_seconds < 0 or age_seconds > BACKUP_MAX_AGE_HOURS * 3600:
        print(
            f"ERROR: backup file is older than {BACKUP_MAX_AGE_HOURS}h or has a future timestamp",
            file=sys.stderr,
        )
        return None

    if not executor.check_pg_restore_available():
        print("ERROR: pg_restore executable not found", file=sys.stderr)
        return None

    try:
        rc, list_output = executor.inspect_backup_list(backup_path)
    except SafetyGuardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None

    if rc != 0:
        print("ERROR: backup archive validation failed (pg_restore --list)", file=sys.stderr)
        return None

    archive_dbname = parse_backup_dbname(list_output)
    if not archive_dbname or archive_dbname != db_name:
        print("ERROR: backup archive dbname does not match --db-name", file=sys.stderr)
        return None

    return checker.checksum(backup_path)


def dry_run_main(args) -> int:
    try:
        manifest = checker.read_json(args.root / MANIFEST)
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

    blocked = any(is_blocked_action(action) for _, action in actions)
    manifest_by_file = {row["file"]: row for row in migrations}
    print_plan(environment, actions, manifest_by_file=manifest_by_file)

    commit = git_commit()
    print(f"git_commit: {commit}")
    print(f"started_at: {utc_now()}")
    print("OFFLINE EVIDENCE ONLY: confirm preconditions and approval before any database apply")
    return 1 if blocked else 0


def apply_main(
    args,
    executor: MigrationExecutor | None = None,
    snapshot_provider: CatalogSnapshotProvider | None = None,
) -> int:
    if executor is None:
        executor = MigrationExecutor()
    if snapshot_provider is None:
        snapshot_provider = Pg16PublicV1SnapshotProvider(psql_path=getattr(executor, "psql_path", "psql"))

    missing = [
        f"--{name}"
        for name, value in (
            ("evidence", args.evidence),
            ("db-name", args.db_name),
            ("system-identifier", args.system_identifier),
            ("confirm-apply", args.confirm_apply),
            ("backup-file", args.backup_file),
            ("record-dir", args.record_dir),
        )
        if value is None
    ]
    if missing:
        print(f"ERROR: --apply requires: {', '.join(missing)}", file=sys.stderr)
        return 3

    if not args.db_name or not args.system_identifier:
        print("ERROR: --db-name and --system-identifier must be non-empty", file=sys.stderr)
        return 3

    if args.confirm_apply != args.db_name:
        print("ERROR: --confirm-apply must match --db-name", file=sys.stderr)
        return 3

    # (a) Load manifest & evidence + static validation
    try:
        manifest = checker.read_json(args.root / MANIFEST)
        errors, _ = checker.validate_inventory(args.root, manifest)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for problem in errors:
        print(f"ERROR: {problem}", file=sys.stderr)
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

    # (b) Reject blocked plans before any DB connection
    if any(is_blocked_action(action) for _, action in actions):
        for name, action in actions:
            print(f"{action}: {name}")
        print("ERROR: plan contains blocked actions; refusing to connect to database", file=sys.stderr)
        return 1

    # (c) Read SQL files once, verify checksums, and reject psql meta-commands
    manifest_by_file = {row["file"]: row for row in migrations}
    prepared_files: list[tuple[dict, bytes, str, dict]] = []
    for name, action in actions:
        if action == "SKIP_RECORDED":
            continue
        row = manifest_by_file.get(name)
        if row is None:
            print(f"ERROR: {name} not in manifest", file=sys.stderr)
            return 1
        sql_path = args.root / "migrations" / name
        try:
            sql_bytes = sql_path.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            print(f"ERROR: unable to read migration file: {name}", file=sys.stderr)
            return 1
        digest = sha256(sql_bytes).hexdigest()
        if digest != row["sha256"]:
            print(f"ERROR: checksum mismatch for {name}; refusing to apply", file=sys.stderr)
            return 1
        if has_psql_meta_command(sql_bytes.decode("utf-8", errors="replace")):
            print(f"ERROR: psql meta-command detected in {name}; refusing to apply", file=sys.stderr)
            return 3
        prepared_files.append((row, sql_bytes, digest, records.get(name, {})))

    # (d) Validate --record-dir and check existing apply-*.json for started/corrupted runs
    record_err = validate_record_dir(args.record_dir)
    if record_err is not None:
        return record_err

    # (e) Validate backup file
    backup_sha256 = validate_backup_file(args.backup_file, args.db_name, executor)
    if backup_sha256 is None:
        return 3

    # (f) Check psql availability
    if not executor.check_psql_available():
        print("ERROR: psql executable not found", file=sys.stderr)
        return 3

    # (g) Connect via psql and verify DB identity
    try:
        actual_db_name, actual_sys_id = executor.query_db_identity()
    except SafetyGuardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    if actual_db_name != args.db_name or actual_sys_id != args.system_identifier:
        print("ERROR: target database identity mismatch", file=sys.stderr)
        return 3

    schema_record_out = getattr(args, "schema_record_out", None)
    if schema_record_out is not None and is_repo_path(schema_record_out):
        print("ERROR: --schema-record-out must be outside the repository", file=sys.stderr)
        return 3

    # (h) All guards passed: create exclusive run record, then execute per-file
    commit = git_commit()
    run_record_path = args.record_dir / f"apply-{utc_timestamp_compact()}-{os.getpid()}.json"
    run_record = {
        "type": "sql-apply-run",
        "environment": environment,
        "git_commit": commit,
        "db_name": args.db_name,
        "system_identifier": args.system_identifier,
        "backup_sha256": backup_sha256,
        "catalog_provider": type(snapshot_provider).__name__,
        "files": [],
    }
    try:
        write_record_exclusive(run_record_path, run_record)
    except OSError as exc:
        print(f"ERROR: unable to create run record exclusively: {exc}", file=sys.stderr)
        return 1

    applied_count = 0
    for idx, (row, sql_bytes, digest, ev_rec) in enumerate(prepared_files, 1):
        name = row["file"]
        entry = {
            "file": name,
            "sha256": digest,
            "transaction": row["transaction"],
            "prechecks": list(row.get("prechecks", [])),
            "state": ev_rec.get("state"),
            "proof": ev_rec.get("proof"),
            "started_at": utc_now(),
            "finished_at": None,
            "result": "started",
            "sqlstate": None,
            "precheck": None,
            "postcheck": None,
        }
        run_record["files"].append(entry)
        update_record(run_record_path, run_record)

        try:
            returncode, sqlstate = executor.execute_bytes(sql_bytes, row["transaction"])
        except ExecutionError:
            entry["finished_at"] = utc_now()
            entry["result"] = "failed"
            update_record(run_record_path, run_record)
            print(f"{idx:02d}. {name} - failed (execution error)")
            print(f"SUMMARY: {applied_count} applied, 1 failed, git_commit={commit}")
            return 2

        entry["finished_at"] = utc_now()
        if returncode != 0:
            entry["result"] = "failed"
            entry["sqlstate"] = sqlstate
            update_record(run_record_path, run_record)
            state_note = f" sqlstate={sqlstate}" if sqlstate else ""
            print(f"{idx:02d}. {name} - failed (exit={returncode}{state_note})")
            print(f"SUMMARY: {applied_count} applied, 1 failed, git_commit={commit}")
            return 2

        entry["result"] = "applied"
        update_record(run_record_path, run_record)
        applied_count += 1
        print(f"{idx:02d}. {name} - applied")

    if schema_record_out is not None:
        try:
            snapshot = snapshot_provider.capture_snapshot()
        except SafetyGuardError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3
        fingerprint = Pg16PublicV1SnapshotProvider.compute_fingerprint(snapshot["catalog"])
        applied_set = {name for name, action in actions if action == "SKIP_RECORDED"} | {
            row["file"] for row, _, _, _ in prepared_files
        }
        applied_migrations = [
            {"file": row["file"], "sha256": row["sha256"]}
            for row in migrations
            if row["file"] in applied_set
        ]
        schema_record = build_schema_record(snapshot["identity"], fingerprint, applied_migrations)
        write_record_exclusive(schema_record_out, schema_record)

    print(f"SUMMARY: {applied_count} applied, 0 failed, git_commit={commit}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="offline JSON evidence file (--evidence format)",
    )
    parser.add_argument("--apply", action="store_true", help="apply migrations to the target database")
    parser.add_argument("--db-name", type=str, help="target database name (--apply only)")
    parser.add_argument(
        "--system-identifier",
        type=str,
        help="target system_identifier; requires pg_control_system() permission (--apply only)",
    )
    parser.add_argument("--confirm-apply", type=str, help="must equal --db-name (--apply only)")
    parser.add_argument("--backup-file", type=Path, help="verified pg_dump custom backup file (--apply only)")
    parser.add_argument("--record-dir", type=Path, help="record directory outside repository (--apply only)")
    parser.add_argument(
        "--schema-record-out",
        type=Path,
        help="optional output path outside repository for schema-record.json (--apply only)",
    )
    args = parser.parse_args(argv)

    if args.apply:
        return apply_main(args)
    return dry_run_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
