"""Compare pytest outcomes with the dev baseline and check exact PR paths."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

REGISTRY = Path("docs/dev-automation/test-baseline.json")
SCOPE = re.compile(r"<!-- toi-scope\n(.*?)\n-->", re.DOTALL)
REMOVALS = re.compile(r"<!-- toi-test-removals\n(.*?)\n-->", re.DOTALL)
OBJECT_ID = re.compile(r"[0-9a-f]{40}")


class GateError(ValueError):
    pass


def git(*args):
    return subprocess.check_output(["git", *args])


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def marker(body, pattern):
    values = pattern.findall((body or "").replace("\r\n", "\n"))
    if len(values) != 1:
        raise GateError("Expected exactly one gate metadata block")
    value = json.loads(values[0])
    if not isinstance(value, dict):
        raise GateError("Metadata must be an object")
    return value


def paths(value):
    if not isinstance(value, list) or not value:
        raise GateError("Expected a nonempty list of exact repository paths")
    for path in value:
        if (not isinstance(path, str) or not path or any(c in path for c in "\\*?:")
                or any(ord(char) < 32 for char in path) or PurePosixPath(path).is_absolute()
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise GateError(f"Invalid exact path {path!r}")
    if len(set(value)) != len(value):
        raise GateError("Duplicate scope path")
    return set(value)


def parse_scope(body):
    scope = marker(body, SCOPE)
    if set(scope) != {"issue", "paths"}:
        raise GateError("Scope requires issue and paths")
    if scope["issue"] is not None and (type(scope["issue"]) is not int or scope["issue"] < 1):
        raise GateError("Issue must be positive or null for manual maintenance")
    return scope["issue"], paths(scope["paths"])


def changed_paths(base, head):
    if not OBJECT_ID.fullmatch(base) or not OBJECT_ID.fullmatch(head):
        raise GateError("Expected full base and head object IDs")
    result = git("diff", "--name-only", "--no-renames", "-z", f"{base}...{head}", "--")
    return set(result.decode("utf-8").rstrip("\0").split("\0")) if result else set()


def check_scope(event):
    pr = event["pull_request"]
    issue, declared = parse_scope(pr["body"])
    if pr["base"]["ref"] != "dev":
        raise GateError("Expected a dev-bound pull request")
    actual = changed_paths(pr["base"]["sha"], pr["head"]["sha"])
    if actual != declared:
        raise GateError(f"Undeclared: {sorted(actual - declared)}; unused: {sorted(declared - actual)}")
    print(f"Scope matches {len(actual)} paths for " + (f"Issue #{issue}" if issue else "manual maintenance"))


def event_key(event):
    return (event["nodeid"], event["phase"], json.dumps(event["subtest"], sort_keys=True))


def entries(document):
    if document["version"] != 2:
        raise GateError("Unsupported test baseline version")
    result = {}
    for field, outcome in (("failures", "failed"), ("skips", "skipped")):
        for entry in document[field]:
            key = event_key(entry)
            if key in result or entry["outcome"] != outcome:
                raise GateError(f"Invalid or duplicate baseline entry: {key}")
            result[key] = entry
    return result


def collection_ids(value):
    if (not isinstance(value, list) or not value
            or any(not isinstance(node, str) or not node for node in value)
            or len(value) != len(set(value))):
        raise GateError("Expected a complete, unique test collection")
    return set(value)


def maintenance_removals(event):
    if event is None:
        return set()
    body = event["pull_request"]["body"]
    issue, _ = parse_scope(body)
    if not REMOVALS.search(body.replace("\r\n", "\n")):
        return set()
    if issue is not None:
        raise GateError("Test removals require a separately approved manual maintenance PR")
    removal = marker(body, REMOVALS)
    if set(removal) != {"nodeids"}:
        raise GateError("Expected exact nodeids for maintenance removals")
    return collection_ids(removal["nodeids"])


def bootstrap_baseline(candidate, base_collection, event):
    pr = event["pull_request"]
    issue, _ = parse_scope(pr["body"])
    if (issue is not None or pr["base"]["ref"] != "dev"
            or candidate["source_commit"] != pr["base"]["sha"]):
        raise GateError("Bootstrap requires manual maintenance and a baseline recorded on the current dev")
    if base_collection["exit_code"] != 0 or base_collection["events"]:
        raise GateError("Bootstrap requires successful collection on the actual dev tree")
    return {**candidate, "collected": base_collection["collected"]}


def compare(report, trusted, proposed, approved_removals=frozenset()):
    collected = collection_ids(report["collected"])
    if report["exit_code"] not in {0, 1}:
        return ["The test suite did not complete"]
    old = entries(trusted)
    current = entries(proposed)
    previous_collection = collection_ids(trusted["collected"])
    proposed_collection = collection_ids(proposed["collected"])
    errors = []
    missing = previous_collection - collected
    if missing != approved_removals:
        errors.append(f"Missing collected tests: {sorted(missing - approved_removals)}; "
                      f"unused maintenance removals: {sorted(approved_removals - missing)}")
    if proposed_collection != collected:
        errors.append(f"Candidate inventory must match collection. Add: {sorted(collected - proposed_collection)}; "
                      f"remove: {sorted(proposed_collection - collected)}")
    if proposed["source_commit"] != trusted["source_commit"]:
        errors.append("Baseline source commit is immutable in feature PRs")
    for key, entry in current.items():
        if old.get(key) != entry:
            errors.append(f"Baseline expansion or modification: {key}")

    bad = {}
    good = set()
    executed = set()
    for event in report["events"]:
        key = event_key(event)
        if key[1] in {"setup", "call"} and event["subtest"] is None:
            executed.add(key[0])
        if event["outcome"] == "passed":
            good.add(key)
        else:
            if key in bad:
                errors.append(f"Multiple adverse outcomes for {key}")
            bad[key] = event
    if collected != executed:
        errors.append(f"Collected tests without outcomes: {sorted(collected - executed)}; "
                      f"outcomes without collection: {sorted(executed - collected)}")
    if (report["exit_code"] == 1) != any(event["outcome"] == "failed" for event in bad.values()):
        errors.append("Pytest exit code and failure reports disagree")

    for key, event in bad.items():
        expected = old.get(key)
        if expected is None or any(event[k] != expected[k] for k in ("outcome", "message")):
            errors.append(f"New or changed {event['outcome']} {key}: {event['message'].splitlines()[0:1]}")
        elif key not in current:
            errors.append(f"Removed baseline entry still occurs: {key}")
    for key, expected in old.items():
        if key in bad:
            continue
        passed_key = (key[0], "call", key[2]) if key[1] == "setup" else key
        if passed_key not in good:
            errors.append(f"Baseline entry is missing without a matching pass: {key}")
        elif key in current:
            errors.append(f"Resolved {expected['outcome']}; remove this entry from candidate baseline: {key}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scope = commands.add_parser("scope")
    scope.add_argument("event", type=Path)
    tests = commands.add_parser("tests")
    tests.add_argument("report", type=Path)
    baseline = tests.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--base", type=Path)
    baseline.add_argument("--bootstrap-collection", type=Path)
    tests.add_argument("--event", type=Path)
    tests.add_argument("--candidate", type=Path, default=REGISTRY)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("report", type=Path)
    inventory.add_argument("--candidate", type=Path, default=REGISTRY)
    args = parser.parse_args()
    try:
        if args.command == "scope":
            check_scope(load_json(args.event))
        elif args.command == "inventory":
            report = load_json(args.report)
            if report["exit_code"] not in {0, 1}:
                raise GateError("Cannot update inventory from incomplete collection")
            candidate = load_json(args.candidate)
            candidate["collected"] = sorted(collection_ids(report["collected"]))
            args.candidate.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            report = load_json(args.report)
            candidate = load_json(args.candidate)
            event = load_json(args.event) if args.event else None
            if args.bootstrap_collection:
                if not event:
                    raise GateError("Bootstrap requires a PR event")
                trusted = bootstrap_baseline(candidate, load_json(args.bootstrap_collection), event)
                removals = set()
            else:
                trusted = load_json(args.base)
                removals = maintenance_removals(event)
            errors = compare(report, trusted, candidate, removals)
            print(f"Collected {len(report['collected'])} tests. Outcomes: {report['counts']}")
            if errors:
                raise GateError("\n".join(errors))
            print("Test collection and adverse outcomes match the reviewed baseline")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Gate blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
