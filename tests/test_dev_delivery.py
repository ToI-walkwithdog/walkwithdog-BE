import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import dev_gate, dev_test_report, issue_delivery


def event(node="tests/test_example.py::test_case", outcome="failed", message="AssertionError: 12 != 10", subtest=None):
    return {"nodeid": node, "phase": "call", "subtest": subtest, "outcome": outcome, "message": message}


def baseline(*events):
    return {"source_commit": "a" * 40, "version": 1,
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
        self.assertEqual(dev_gate.compare(report(passed), baseline(known), baseline()), [])
        other = event("tests/other.py::test_other", "passed", "")
        self.assertTrue(dev_gate.compare(report(other), baseline(known), baseline()))

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

    def test_issue_edits_invalidate_approval(self):
        approval = {"issue": 44, "issue_digest": issue_delivery.issue_digest("feature", "details")}
        issue = {"state": "open", "number": 44, "title": "feature", "body": "details"}
        issue_delivery.check_issue(issue, approval, "")
        with self.assertRaises(dev_gate.GateError):
            issue_delivery.check_issue({**issue, "body": "different"}, approval, "")
