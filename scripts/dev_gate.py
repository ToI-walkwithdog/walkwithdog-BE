"""Compare pytest outcomes with the dev baseline and check exact PR paths."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

REGISTRY = Path("docs/dev-automation/test-baseline.json")
SCOPE = re.compile(r"<!-- toi-scope\n(.*?)\n-->", re.DOTALL)
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
    if document["version"] != 1:
        raise GateError("Unsupported test baseline version")
    result = {}
    for field, outcome in (("failures", "failed"), ("skips", "skipped")):
        for entry in document[field]:
            key = event_key(entry)
            if key in result or entry["outcome"] != outcome:
                raise GateError(f"Invalid or duplicate baseline entry: {key}")
            result[key] = entry
    return result


def compare(report, trusted, proposed):
    collected = report["collected"]
    if report["exit_code"] not in {0, 1} or not collected or len(collected) != len(set(collected)):
        return ["The test suite did not produce a complete, unique collection"]
    old = entries(trusted)
    current = entries(proposed)
    errors = []
    if proposed["source_commit"] != trusted["source_commit"]:
        errors.append("Baseline source commit is immutable in feature PRs")
    for key, entry in current.items():
        if old.get(key) != entry:
            errors.append(f"Baseline expansion or modification: {key}")

    bad = {}
    good = set()
    for event in report["events"]:
        key = event_key(event)
        if event["outcome"] == "passed":
            good.add(key)
        else:
            if key in bad:
                errors.append(f"Multiple adverse outcomes for {key}")
            bad[key] = event
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
    tests.add_argument("--base", type=Path, required=True)
    tests.add_argument("--candidate", type=Path, default=REGISTRY)
    args = parser.parse_args()
    try:
        if args.command == "scope":
            check_scope(load_json(args.event))
        else:
            report = load_json(args.report)
            errors = compare(report, load_json(args.base), load_json(args.candidate))
            print(f"Collected {len(report['collected'])} tests. Outcomes: {report['counts']}")
            if errors:
                raise GateError("\n".join(errors))
            print("All adverse outcomes match the reviewed baseline")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Gate blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
