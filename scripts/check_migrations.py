"""Validate migration files and review recorded evidence without opening a DB connection."""
import argparse
from collections import Counter, defaultdict
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("migrations/manifest.json")
FILENAME = re.compile(r"(?!000)[0-9]{3}_[a-z0-9]+(?:_[a-z0-9]+)*\.sql")
DIGEST = re.compile(r"[0-9a-f]{64}")
LEGACY_DUPLICATES = {
    "017": {"017_f10_behavior_marker.sql", "017_remove_user_email.sql"},
    "018": {"018_f11_walk_overtime.sql", "018_user_consents.sql"},
}
ENVIRONMENTS = {"local-test", "dev", "production"}
TRANSACTIONS = {"file", "psql-single", "outside"}
EVIDENCE_STATES = {"applied", "absent", "schema-match", "partial", "unknown"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    with path.open(encoding="utf-8") as source:
        return json.load(source, object_pairs_hook=unique_object)


def checksum(path):
    """Hash exact bytes, with CRLF normalized to LF for Git checkouts."""
    return sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def filename(value):
    if not isinstance(value, str) or not FILENAME.fullmatch(value):
        raise ValueError(f"invalid migration filename: {value!r}")
    return value


def object_list(data, key):
    values = data[key]
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        raise ValueError(f"{key} must be a list of objects")
    return values


def references(row, field):
    values = row[field]
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{row['file']}: {field} must be a list of strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{row['file']}: duplicate {field}")
    return values


def number_groups(names):
    groups = defaultdict(set)
    for name in names:
        groups[name[:3]].add(name)
    return groups


def validate_order(names, graph):
    for name, dependencies in graph.items():
        unknown = set(dependencies) - set(names)
        if unknown:
            return [f"{name}: unknown prerequisite {dependency}" for dependency in sorted(unknown)]
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        return ["dependency cycle: " + " -> ".join(exc.args[1])]

    position = {name: index for index, name in enumerate(names)}
    return [
        f"{name}: prerequisite {dependency} must precede it"
        for name, dependencies in graph.items()
        for dependency in dependencies
        if position[dependency] >= position[name]
    ]


def validate_inventory(root, manifest):
    """Return static inventory errors separately from unmerged number conflicts."""
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("unsupported migration manifest version")

    live = object_list(manifest, "migrations")
    reservations = object_list(manifest, "reservations")
    if not live:
        raise ValueError("migrations must not be empty")
    live_names = [filename(row["file"]) for row in live]
    reserved_names = [filename(row["file"]) for row in reservations]
    names = live_names + reserved_names
    errors = [f"filename listed more than once: {name}" for name, count in Counter(names).items() if count > 1]
    graph = {row["file"]: references(row, "requires") for row in live + reservations}
    errors.extend(validate_order(names, graph))
    for row in live:
        name = row["file"]
        for dependency in graph[name]:
            if dependency in reserved_names:
                errors.append(f"{name}: dev migration depends on unmerged file {dependency}")
        if row["transaction"] not in TRANSACTIONS:
            errors.append(f"{name}: invalid transaction policy")
        environments = references(row, "environments")
        if not environments or set(environments) - ENVIRONMENTS:
            errors.append(f"{name}: invalid environments")
        digest = row["sha256"]
        path = root / "migrations" / name
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            errors.append(f"{name}: invalid SHA-256")
        elif path.is_file() and checksum(path) != digest:
            errors.append(f"{name}: SHA-256 mismatch")
        for script in references(row, "prechecks"):
            relative = Path(script)
            if relative.parts[:1] != ("scripts",) or ".." in relative.parts or not (root / relative).is_file():
                errors.append(f"{name}: missing or invalid precheck script {script}")

    for row in reservations:
        if row["state"] not in {"branch-only", "unmerged", "reserved"}:
            errors.append(f"{row['file']}: invalid reservation state")
        if not isinstance(row["source"], str) or not row["source"].strip():
            errors.append(f"{row['file']}: missing reservation source")

    folder = root / "migrations"
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*")
              if p.is_file() and p.suffix.lower() == ".sql"}
    for name in sorted(set(live_names) - actual):
        errors.append(f"missing SQL file: {name}")
    for name in sorted(actual - set(live_names)):
        errors.append(f"SQL file not listed for dev: {name}")
    local_groups = number_groups(name for name in actual if FILENAME.fullmatch(name))
    for prefix, group in number_groups(live_names).items():
        local_groups[prefix].update(group)
    for prefix, group in sorted(local_groups.items()):
        if len(group) > 1 and group != LEGACY_DUPLICATES.get(prefix):
            errors.append(f"duplicate dev number {prefix}: {', '.join(sorted(group))}")

    conflicts = []
    active_groups = number_groups(live_names + [name for name in actual if FILENAME.fullmatch(name)])
    for prefix, group in sorted(number_groups(reserved_names).items()):
        other = active_groups.get(prefix, set())
        if other or len(group) > 1:
            conflicts.append(f"reserved number {prefix}: {', '.join(sorted(group | other))}")
    return errors, conflicts


def proposal_error(proposed, manifest):
    name = filename(proposed)
    used = number_groups(row["file"] for row in manifest["migrations"] + manifest["reservations"])
    prefix = name[:3]
    if prefix in used:
        return f"{name}: number {prefix} is already used or reserved by {', '.join(sorted(used[prefix]))}"
    if int(prefix) <= max(map(int, used)):
        return f"{name}: use the next number above {max(used)}; historical gaps are not reusable"
    if int(prefix) != max(map(int, used)) + 1:
        return f"{name}: expected next number {max(map(int, used)) + 1:03d}"
    return None


def evidence_plan(evidence, migrations):
    if not isinstance(evidence, dict) or evidence.get("environment") not in ENVIRONMENTS:
        raise ValueError("evidence must identify a supported environment")
    environment, records = evidence["environment"], evidence["files"]
    known = {row["file"] for row in migrations}
    if not isinstance(records, dict) or set(records) - known:
        raise ValueError("evidence files must reference listed dev migrations")
    for name, record in records.items():
        if not isinstance(record, dict) or record.get("state") not in EVIDENCE_STATES:
            raise ValueError(f"{name}: invalid evidence state")
        if record["state"] != "unknown" and (
            not isinstance(record.get("proof"), str) or not record["proof"].strip()
        ):
            raise ValueError(f"{name}: provide a proof reference for recorded evidence")

    verified = set()
    actions = []
    for row in migrations:
        name = row["file"]
        record = records.get(name, {})
        state = record.get("state", "unknown")
        missing = [dependency for dependency in row["requires"] if dependency not in verified]
        if environment not in row["environments"]:
            action = "OUTSIDE_ENVIRONMENT"
        elif state == "applied" and record.get("sha256") != row["sha256"]:
            action = "RECONCILE_CHECKSUM"
        elif state in {"applied", "absent"} and missing:
            action = "BLOCKED_PREREQUISITES (" + ", ".join(missing) + ")"
        elif state == "applied":
            verified.add(name)
            action = "SKIP_RECORDED"
        elif state == "absent":
            action = "MANUAL_PRECHECK_REQUIRED"
        else:
            action = f"RECONCILE_{state.upper().replace('-', '_')}"
        actions.append((name, action))
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--evidence", type=Path, help="offline JSON with reviewed per-file evidence")
    parser.add_argument("--check-reservations", action="store_true", help="fail on unmerged reservation conflicts")
    parser.add_argument("--propose", metavar="FILENAME", help="check a proposed new migration filename")
    args = parser.parse_args(argv)
    try:
        manifest = read_json(args.root / MANIFEST)
        errors, conflicts = validate_inventory(args.root, manifest)
        if args.propose and not errors:
            proposal = proposal_error(args.propose, manifest)
            if proposal:
                errors.append(proposal)
        actions = evidence_plan(read_json(args.evidence), manifest["migrations"]) if args.evidence and not errors else []
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for problem in errors:
        print(f"ERROR: {problem}", file=sys.stderr)
    for conflict in conflicts:
        print(f"BLOCKED FUTURE MIGRATION: {conflict}")
    if errors:
        return 1

    rows = manifest["migrations"]
    for order, row in enumerate(rows, 1):
        print(f"{order:02d}. {row['file']} [{row['transaction']}]")
    for row in manifest["reservations"]:
        print(f"RESERVED {row['file']} [{row['state']}; {row['source']}]")
    if args.propose:
        print(f"AVAILABLE IN THIS MANIFEST: {args.propose}; check remote branches and PRs before reserving")
    for name, action in actions:
        print(f"{action}: {name}")
    if not args.evidence:
        print("DATABASE STATE UNKNOWN: static checks only; no migration can be inferred as applied")
    else:
        print("OFFLINE EVIDENCE ONLY: confirm live preconditions and approval before any manual apply")
    blocked = any(action.startswith(("RECONCILE", "BLOCKED")) for _, action in actions)
    return int(bool(blocked or (args.check_reservations and conflicts)))


if __name__ == "__main__":
    raise SystemExit(main())
