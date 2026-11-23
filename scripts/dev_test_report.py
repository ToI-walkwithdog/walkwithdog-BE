"""Run the dev test suite and capture individual pytest outcomes for the gate."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys

import pytest
from _pytest.subtests import SubtestReport


def normalize(text):
    """Remove only transient timestamps and pytest repr truncation lengths."""
    text = re.sub(r"datetime\.datetime\(\d{4}, \d{1,2}, \d{1,2}, \d{1,2}, \d{1,2}(?:, \d+){0,2}\)",
                  "datetime.datetime(<timestamp>)", text)
    text = re.sub(r"datetime\.[a-z]*\[(?:\d+ chars|…)\]\d*\)\)", "datetime.datetime(<abbreviated>)", text)
    text = re.sub(r"\[\d+ chars\]", "[…]", text)
    text = text.replace("\r\n", "\n")
    # Pytest can print a paired ndiff change when a fixture save crosses a second.
    # If the two lines are identical after timestamp normalization, retain one
    # context line. Real field changes, deletions, and additions stay in the report.
    timestamp_diff = re.compile(
        r"(?m)^-(?P<value>[ \t]+datetime\.datetime\(<timestamp>\)[),]*)\n"
        r"(?:\?[^\n]*\n)?(?:[ \t]*\n)*"
        r"\+(?P=value)(?:\n\?[^\n]*(?:\n[ \t]*(?=\n))*)?(?=\n|$)"
    )
    return timestamp_diff.sub(r" \g<value>", text).strip()


class TestReport:
    def __init__(self):
        self.collected = []
        self.events = []
        self.counts = Counter()

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.capture(report, "collect")

    def pytest_runtest_logreport(self, report):
        if report.passed and report.when != "call" and not hasattr(report, "wasxfail"):
            return
        self.capture(report, report.when)

    def capture(self, report, phase):
        subtest = None
        if isinstance(report, SubtestReport):
            subtest = {"message": report.context.msg, "kwargs": dict(report.context.kwargs)}
        outcome = report.outcome
        if hasattr(report, "wasxfail"):
            outcome = "xfail" if report.skipped else "xpass"
            message = report.wasxfail
        elif report.passed:
            message = ""
        elif isinstance(report.longrepr, tuple):
            message = report.longrepr[2]
        else:
            crash = getattr(report.longrepr, "reprcrash", None)
            message = crash.message if crash else str(report.longrepr)
        self.counts[outcome] += 1
        self.events.append({
            "nodeid": report.nodeid,
            "phase": phase,
            "subtest": subtest,
            "outcome": outcome,
            "message": normalize(message),
        })


def validate_db_settings():
    """Require two different, explicitly named loopback test databases."""
    from urllib.parse import urlsplit

    if os.environ.get("DATABASE_URL") != "sqlite://":
        raise ValueError("DATABASE_URL must be sqlite://")
    names = set()
    selected = {"SIGNUP_TEST_DATABASE_URL", "REGION_TEST_DATABASE_URL"}
    for key in selected:
        url = urlsplit(os.environ.get(key, ""))
        if (url.scheme not in {"postgresql", "postgresql+psycopg2"}
                or url.hostname != "127.0.0.1" or not url.port
                or not re.fullmatch(r"/toi_walk_test_[a-z0-9_]+", url.path)
                or url.query or url.fragment):
            raise ValueError(f"{key} must use a named loopback test database")
        names.add(url.path)
    if len(names) != len(selected):
        raise ValueError("Test databases must be distinct")
    if any(key.endswith("_TEST_DATABASE_URL") and key not in selected for key in os.environ):
        raise ValueError("Unexpected PostgreSQL test group in the baseline run")
    if Path(".env").exists():
        raise ValueError("Run in an isolated checkout without .env")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate_db_settings()
    except ValueError as exc:
        parser.error(str(exc))
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        os.environ.pop(key, None)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    report = TestReport()
    status = pytest.main(["-q", "--strict-markers", "tests/"], plugins=[report])
    payload = {"exit_code": int(status), "counts": report.counts,
               "collected": report.collected, "events": report.events}
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # The gate evaluates the report. Interrupted and empty runs are always errors.
    return 0 if status in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED) else int(status)


if __name__ == "__main__":
    sys.exit(main())
