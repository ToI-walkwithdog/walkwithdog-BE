import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import dev_gate, dev_test_report, issue_delivery


def event(node="tests/test_example.py::test_case", outcome="failed", message="AssertionError: 12 != 10", subtest=None):
    return {"nodeid": node, "phase": "call", "subtest": subtest, "outcome": outcome, "message": message}


def baseline(*events):
    return {"source_commit": "a" * 40, "version": 2,
            "collected": sorted({item["nodeid"] for item in events}),
            "failures": [item for item in events if item["outcome"] == "failed"],
            "skips": [item for item in events if item["outcome"] == "skipped"]}


def report(*events):
    return {"collected": sorted({item["nodeid"] for item in events}),
            "exit_code": int(any(item["outcome"] == "failed" for item in events)),
            "events": events}


class TestRegressionGate(unittest.TestCase):
    def test_only_exact_failure_passes(self):
        known = event()
        self.assertEqual(dev_gate.compare(report(known), baseline(known), baseline(known)), [])
        changed_error = {**known, "message": "Different failure"}
        self.assertTrue(dev_gate.compare(report(changed_error), baseline(known), baseline(known)))

    def test_resolved_failure_must_be_removed_and_must_pass(self):
        known = event()
        passed = {**known, "outcome": "passed", "message": ""}
        self.assertTrue(dev_gate.compare(report(passed), baseline(known), baseline(known)))
        self.assertEqual(dev_gate.compare(report(passed), baseline(known), baseline(passed)), [])
        other = event("tests/other.py::test_other", "passed", "")
        self.assertTrue(dev_gate.compare(report(other), baseline(known), baseline(other)))

    def test_missing_collected_or_executed_tests_and_new_tests(self):
        old = event(outcome="passed", message="")
        new = {**old, "nodeid": "tests/test_example.py::test_added"}
        trusted = baseline(old)
        self.assertIn("Missing collected tests", dev_gate.compare(report(new), trusted, baseline(new))[0])
        self.assertIn("Candidate inventory", dev_gate.compare(report(old, new), trusted, trusted)[0])
        self.assertEqual(dev_gate.compare(report(old, new), trusted, baseline(old, new)), [])
        incomplete = {**report(new), "collected": [old["nodeid"], new["nodeid"]]}
        self.assertIn("Collected tests without outcomes",
                      dev_gate.compare(incomplete, trusted, baseline(old, new))[0])

    def test_test_removal_requires_manual_maintenance_and_exact_registered_id(self):
        old = event(outcome="passed", message="")
        replacement = {**old, "nodeid": "tests/test_example.py::test_renamed"}
        marker = '\n<!-- toi-test-removals\n' + json.dumps({"nodeids": [old["nodeid"]]}) + '\n-->'
        for issue in (44, None):
            scope = json.dumps({"issue": issue, "paths": ["tests/test_example.py"]})
            body = '<!-- toi-scope\n' + scope + '\n-->' + marker
            payload = {"pull_request": {"body": body}}
            if issue:
                with self.assertRaisesRegex(dev_gate.GateError, "manual maintenance"):
                    dev_gate.maintenance_removals(payload)
            else:
                removed = dev_gate.maintenance_removals(payload)
                self.assertEqual(dev_gate.compare(report(replacement), baseline(old), baseline(replacement),
                                                  removed), [])
                self.assertTrue(dev_gate.compare(report(old), baseline(old), baseline(old), removed))
                failing = event()
                self.assertTrue(dev_gate.compare(report(replacement), baseline(failing), baseline(replacement),
                                                 removed))

    def test_bootstrap_cannot_hide_a_dev_id_in_the_candidate_registry(self):
        old = event(outcome="passed", message="")
        replacement = {**old, "nodeid": "tests/test_example.py::test_renamed"}
        pr = {"body": '<!-- toi-scope\n{"issue":null,"paths":["tests/test_example.py"]}\n-->',
              "base": {"sha": "a" * 40, "ref": "dev"}}
        candidate = baseline(replacement)
        collected = {**report(old), "events": []}
        trusted = dev_gate.bootstrap_baseline(candidate, collected, {"pull_request": pr})
        self.assertTrue(dev_gate.compare(report(replacement), trusted, candidate))
        with self.assertRaisesRegex(dev_gate.GateError, "successful collection"):
            dev_gate.bootstrap_baseline(candidate, {**collected, "exit_code": 2}, {"pull_request": pr})

    def test_pytest_removal_skip_xfail_and_failure_are_blocked(self):
        reporter = Path(dev_test_report.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            fixture = tests / "test_probe.py"
            output = root / "report.json"
            env = {**os.environ, "DATABASE_URL": "sqlite://",
                   "SIGNUP_TEST_DATABASE_URL": "postgresql://127.0.0.1:1/toi_walk_test_signup",
                   "REGION_TEST_DATABASE_URL": "postgresql://127.0.0.1:1/toi_walk_test_regions"}
            for key in env.copy():
                if key.endswith("_TEST_DATABASE_URL") and key not in {
                    "SIGNUP_TEST_DATABASE_URL", "REGION_TEST_DATABASE_URL",
                }:
                    del env[key]
            stable = "import pytest\n\ndef test_stable():\n    assert True\n\n"
            old = event("tests/test_probe.py::test_protected", "passed", "")
            keep = event("tests/test_probe.py::test_stable", "passed", "")
            trusted = baseline(old, keep)
            cases = (
                ("", "Missing collected tests"),
                ("@pytest.mark.skip(reason='disabled')\ndef test_protected(): pass\n", "New or changed skipped"),
                ("@pytest.mark.xfail(reason='disabled')\ndef test_protected(): assert False\n", "New or changed xfail"),
                ("def test_protected(): assert False\n", "New or changed failed"),
                ("def test_protected(): assert True\ndef test_added(): assert True\n", None),
            )
            for source, expected in cases:
                with self.subTest(expected=expected):
                    fixture.write_text(stable + source, encoding="utf-8")
                    result = subprocess.run([sys.executable, str(reporter), "--out", str(output)],
                                            cwd=root, env=env, capture_output=True, check=True)
                    observed = dev_gate.load_json(output)
                    candidate = {**trusted, "collected": observed["collected"]}
                    errors = dev_gate.compare(observed, trusted, candidate)
                    if expected:
                        self.assertTrue(any(expected in error for error in errors), (errors, result.stdout))
                    else:
                        self.assertEqual(errors, [])

    def test_rejects_added_and_changed_skip_xfail_and_xpass(self):
        known = event(outcome="skipped", message="Skipped: isolated optional suite")
        for outcome in ("skipped", "xfail", "xpass"):
            with self.subTest(outcome=outcome):
                new = event("tests/test_example.py::test_new", outcome, "unreviewed")
                self.assertTrue(dev_gate.compare(report(known, new), baseline(known), baseline(known, new)))
        altered = {**known, "message": "different skip reason"}
        self.assertTrue(dev_gate.compare(report(altered), baseline(known), baseline(known)))

    def test_timestamp_rollover_does_not_mask_other_field_changes(self):
        context = "AssertionError: Tuples differ\n-    'INTERMEDIATE',\n+    'BEGINNER',\n"
        stable = context + "     datetime.datetime(2026, 11, 23, 17, 58, 23)),\n   [('slot', 1)])"
        rollover = (context + "-    datetime.datetime(2026, 11, 23, 17, 58, 24)),\n"
                    "?                                             ^\n\n"
                    "+    datetime.datetime(2026, 11, 23, 17, 58, 23)),\n"
                    "?                                             ^\n\n"
                    "   [('slot', 1)])")
        self.assertEqual(dev_test_report.normalize(stable), dev_test_report.normalize(rollover))
        self.assertNotEqual(dev_test_report.normalize(stable),
                            dev_test_report.normalize(rollover.replace("INTERMEDIATE", "EXPERIENCED")))

    def test_truncated_timestamp_accepts_seconds_from_hosted_ci(self):
        windows = "[(1, 'INTERMEDIATE', datetime.da[195 chars]0))]"
        ubuntu = "[(1, 'INTERMEDIATE', datetime.da[195 chars] 9))]"
        normalized = "[(1, 'INTERMEDIATE', datetime.datetime(<abbreviated>)]"
        self.assertEqual(dev_test_report.normalize(windows), normalized)
        self.assertEqual(dev_test_report.normalize(ubuntu), normalized)
        self.assertNotEqual(dev_test_report.normalize(ubuntu.replace("INTERMEDIATE", "EXPERIENCED")),
                            normalized)

    def test_uses_subtest_parameters(self):
        known = event(subtest={"message": None, "kwargs": {"body": "'INTERMEDIATE'"}})
        changed = {**known, "subtest": {"message": None, "kwargs": {"body": "'EXPERT'"}}}
        self.assertTrue(dev_gate.compare(report(changed), baseline(known), baseline(known)))

    def test_exact_declaration(self):
        pr = {"body": '<!-- toi-scope\n{"issue":44,"paths":["app/x.py"]}\n-->',
              "base": {"ref": "dev", "sha": "a" * 40}, "head": {"sha": "b" * 40}}
        with patch.object(dev_gate, "changed_paths", return_value={"app/x.py", ".github/new.yml"}):
            with self.assertRaises(dev_gate.GateError):
                dev_gate.check_scope({"pull_request": pr})
        for path in ("../app/x.py", "app/*.py", "/app/x.py", "a//b", "a\nb"):
            with self.subTest(path=path), self.assertRaises(dev_gate.GateError):
                dev_gate.paths([path])


class TestDeliveryGate(unittest.TestCase):
    def test_independent_review_for_the_exact_head(self):
        head = "b" * 40
        approval = {"state": "APPROVED", "user": {"login": "reviewer"}, "commit_id": head,
                    "body": '<!-- toi-review\n' + json.dumps({"issue": 44, "head": head, "blockers": []}) + '\n-->'}
        issue_delivery.check_reviews([approval], {"reviewer"}, {"reviewer"}, 44, head)
        with self.assertRaisesRegex(dev_gate.GateError, "No independent approval"):
            issue_delivery.check_reviews([], {"reviewer"}, {"reviewer"}, 44, head)
        for reviewers, reviewed_head in ((set(), head), ({"reviewer"}, "c" * 40)):
            with self.subTest(reviewers=reviewers), self.assertRaises(dev_gate.GateError):
                issue_delivery.check_reviews([approval], {"reviewer"}, reviewers, 44, reviewed_head)
        rejection = {**approval, "state": "CHANGES_REQUESTED"}
        with self.assertRaises(dev_gate.GateError):
            issue_delivery.check_reviews([approval, rejection], {"reviewer"}, {"reviewer"}, 44, head)

    def test_no_release_with_protection_bypass(self):
        rules = {
            "required_status_checks": {"strict": True, "checks": [{"context": issue_delivery.CHECK, "app_id": 15368}]},
            "enforce_admins": {"enabled": True}, "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_pull_request_reviews": {"required_approving_review_count": 1,
                                              "dismiss_stale_reviews": True, "require_last_push_approval": True},
        }
        issue_delivery.check_protection(rules)
        with self.assertRaises(dev_gate.GateError):
            issue_delivery.check_protection({})
        changed = copy.deepcopy(rules)
        changed["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {"users": ["maintainer"]}
        with self.assertRaises(dev_gate.GateError):
            issue_delivery.check_protection(changed)
        for key in ("enforce_admins", "required_status_checks", "allow_force_pushes"):
            with self.subTest(key=key):
                changed = copy.deepcopy(rules)
                for prop in ("enabled", "strict"):
                    if prop in changed[key]:
                        changed[key][prop] = not changed[key][prop]
                with self.assertRaises(dev_gate.GateError):
                    issue_delivery.check_protection(changed)

    def test_intake_registration_cannot_write_when_disabled(self):
        with patch.object(issue_delivery, "variable", return_value="false"), \
                patch.object(issue_delivery, "api") as api:
            with self.assertRaises(dev_gate.GateError):
                issue_delivery.register(argparse.Namespace())
        api.assert_not_called()

    def test_ambiguous_registration_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approval = {"issue": None, "issue_digest": issue_delivery.issue_digest("feature", "body"),
                        "paths": ["app/x.py"]}
            draft = root / "draft.json"
            draft.write_text(json.dumps({"title": "feature", "body": "body"}), encoding="utf-8")
            state = root / "state.json"
            issue_delivery.write_state(state, {"approval_comment": 42, "issue_digest": approval["issue_digest"],
                                               "paths": approval["paths"]})
            args = argparse.Namespace(draft=draft, state=state, approval_comment=42)
            with patch.object(issue_delivery, "require_enabled"), \
                    patch.object(issue_delivery, "read_approval", return_value=(approval, "today")), \
                    patch.object(issue_delivery, "rows", return_value=[]), patch.object(issue_delivery, "api") as api:
                with self.assertRaises(dev_gate.GateError):
                    issue_delivery.register(args)
                api.assert_not_called()

    def test_github_approval_requires_a_trusted_user_on_the_correct_issue(self):
        data = {"issue": 44, "issue_digest": "f" * 64, "paths": ["app/x.py"]}
        comment = {"user": {"login": "owner", "type": "User"},
                   "body": '<!-- toi-approval\n' + json.dumps(data) + '\n-->',
                   "issue_url": "https://api.github.com/repos/ToI-walkwithdog/walkwithdog-BE/issues/44",
                   "created_at": "2026-01-01T00:00:00Z"}
        with patch.object(issue_delivery, "logins", return_value={"owner"}), \
                patch.object(issue_delivery, "api") as api:
            api.return_value = comment
            self.assertEqual(issue_delivery.read_approval(42)[0], data)
            for rejected in (
                {**comment, "user": {"login": "other", "type": "User"}},
                {**comment, "user": {"login": "owner", "type": "Bot"}},
                {**comment, "issue_url": comment["issue_url"].replace("/44", "/43")},
            ):
                api.return_value = rejected
                with self.assertRaises(dev_gate.GateError):
                    issue_delivery.read_approval(42)

    def test_live_approval_changes_and_extra_paths_are_blocked(self):
        state = {"approval_comment": 42, "issue": 44, "issue_digest": "f" * 64, "paths": ["app/x.py"]}
        update = {"issue": 44, "issue_digest": "f" * 64, "paths": ["app/y.py"]}
        with patch.object(issue_delivery, "read_approval", return_value=(update, "today")):
            with self.assertRaisesRegex(dev_gate.GateError, "Approval changed"):
                issue_delivery.check_live_approval(state)
        with self.assertRaisesRegex(dev_gate.GateError, "changed paths"):
            issue_delivery.check_change_scope(state, {"app/x.py", "app/y.py"}, {"app/x.py", "app/y.py"})
        state["paths"].append(".github/workflows/dev-ci.yml")
        with self.assertRaisesRegex(dev_gate.GateError, "separate human review"):
            issue_delivery.check_change_scope(state, {state["paths"][-1]}, {state["paths"][-1]})

    def test_pr_recovers_existing_results_and_blocks_ambiguous_repeat(self):
        branch = "issue/44"
        state = {"issue": 44, "branch": branch, "paths": ["app/x.py"]}
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(state=Path(tmp) / "state.json")
            args.state.write_text(json.dumps(state), encoding="utf-8")
            pending = args.state.with_suffix(".json.pr-pending")
            pending.touch()
            with patch.object(issue_delivery, "require_enabled"), patch.object(issue_delivery, "check_live_approval"), \
                    patch.object(issue_delivery, "rows", return_value=[]) as rows, \
                    patch.object(issue_delivery, "api") as api:
                with self.assertRaisesRegex(dev_gate.GateError, "unknown outcome"):
                    issue_delivery.open_pr(args)
                pr = {"number": 68, "state": "open", "html_url": "https://example.com/pull/68"}
                rows.return_value = [pr]
                with patch.object(issue_delivery, "pull_paths", return_value={"app/x.py"}), \
                        patch.object(issue_delivery, "check_pr"):
                    issue_delivery.open_pr(args)
                    issue_delivery.open_pr(args)
                self.assertEqual(dev_gate.load_json(args.state)["pr"], 68)
                api.assert_not_called()

    def test_pr_missing_latest_dev_is_blocked(self):
        head, base, previous = "b" * 40, "c" * 40, "a" * 40
        pr = {"state": "open", "draft": False, "base": {"sha": base}, "mergeable": True,
              "mergeable_state": "clean"}
        with patch.object(issue_delivery, "api", return_value={"merge_base_commit": {"sha": previous}}):
            with self.assertRaisesRegex(dev_gate.GateError, "current dev"):
                issue_delivery.check_current_pr(pr, base, head)

    def test_ci_requires_success_for_the_exact_pr_base_and_head(self):
        head, base = "b" * 40, "a" * 40
        run = {"id": 99, "path": issue_delivery.WORKFLOW, "head_sha": head, "conclusion": "success",
               "pull_requests": [{"number": 68, "base": {"sha": base}}]}
        passed = {"jobs": [{"name": issue_delivery.CHECK, "conclusion": "success"}]}
        with patch.object(issue_delivery, "api", side_effect=[{"workflow_runs": [run]}, passed]):
            self.assertEqual(issue_delivery.check_ci(68, base, head), run)
        for stale in ({**run, "conclusion": "failure"}, {**run, "head_sha": "c" * 40},
                      {**run, "pull_requests": [{"number": 68, "base": {"sha": "c" * 40}}]}):
            with patch.object(issue_delivery, "api", return_value={"workflow_runs": [stale]}):
                with self.assertRaisesRegex(dev_gate.GateError, "exact base/head"):
                    issue_delivery.check_ci(68, base, head)

    def test_issue_edits_invalidate_approval(self):
        approval = {"issue": 44, "issue_digest": issue_delivery.issue_digest("feature", "details")}
        issue = {"state": "open", "number": 44, "title": "feature", "body": "details"}
        issue_delivery.check_issue(issue, approval, "")
        with self.assertRaises(dev_gate.GateError):
            issue_delivery.check_issue({**issue, "body": "different"}, approval, "")
