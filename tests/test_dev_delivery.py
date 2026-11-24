import argparse
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


class TestCodexApproval(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.object(issue_delivery.Path, "home", return_value=home))
        self.thread = "01234567-89ab-4cde-8fab-0123456789ab"
        self.timestamp = "2026-01-02T03:04:05Z"
        self.log = (home / ".codex" / "sessions" / "2026" / "01" / "02"
                    / f"rollout-2026-01-02T03-00-00-{self.thread}.jsonl")
        self.log.parent.mkdir(parents=True)
        self.issue = {"state": "open", "number": 44, "title": "Onboarding", "body": "Approved details"}
        self.scope = {"repository": issue_delivery.REPO, "issue": 44, "title": self.issue["title"],
                      "issue_digest": issue_delivery.issue_digest(self.issue["title"], self.issue["body"]),
                      "paths": ["app/x.py"]}
        self.prompt = issue_delivery.codex_approval_message(44)

    def write_events(self, *events):
        meta = {"type": "session_meta", "payload": {"id": self.thread}}
        self.log.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in (meta, *events)),
                            encoding="utf-8")

    def event(self, kind, message, timestamp=None, phase=None):
        return {"type": "event_msg", "timestamp": timestamp or self.timestamp,
                "payload": {"type": kind, "message": message, "phase": phase}}

    def proposal(self, **edits):
        scope = {**self.scope, **edits}
        text = "Review the exact scope\n```toi-approval-request\n" + json.dumps(scope) + "\n```"
        return self.event("agent_message", text, phase="final_answer")

    def saved_state(self):
        return {"issue": 44, "issue_digest": self.scope["issue_digest"], "paths": self.scope["paths"],
                "codex_approval": issue_delivery.read_codex_approval(44, self.thread)}

    def test_user_event_binds_proposal_and_can_be_revalidated_after_later_messages(self):
        actual = "\n" + self.prompt + "\n"
        self.write_events(self.proposal(), self.event("user_message", actual))
        state = self.saved_state()
        proof = state["codex_approval"]
        self.assertEqual(proof["message"], actual)
        self.assertEqual(proof["title"], "Onboarding")
        self.assertEqual(proof["thread"], self.thread)
        self.write_events(self.proposal(), self.event("user_message", actual),
                          self.event("user_message", "A later question", timestamp="2026-01-02T03:05:05Z"))
        with patch.object(issue_delivery, "api", return_value=self.issue) as api:
            issue_delivery.check_live_approval(state)
            api.assert_called_once_with("issues/44")
        with self.assertRaisesRegex(dev_gate.GateError, "not explicit approval"):
            issue_delivery.read_codex_approval(44, self.thread)

    def test_quoted_inferred_and_assistant_approvals_are_rejected(self):
        good = self.event("user_message", self.prompt)
        cases = (
            ((self.proposal(), self.event("agent_message", self.prompt, phase="final_answer")), "missing"),
            ((self.proposal(), self.event("user_message", f'Example: "{self.prompt}"')), "not explicit"),
            ((self.proposal(), self.event("user_message", "Please continue")), "not explicit"),
            ((self.proposal(), self.event("user_message", "Question", timestamp="2026-01-02T03:03:05Z"), good),
             "immediately follow"),
            ((self.proposal(issue=45), good), "preceding Codex proposal"),
            ((self.proposal(repository="another/repo"), good), "preceding Codex proposal"),
        )
        for rows, message in cases:
            with self.subTest(message=message):
                self.write_events(*rows)
                with self.assertRaisesRegex(dev_gate.GateError, message):
                    issue_delivery.read_codex_approval(44, self.thread)

    def test_missing_changed_and_mixed_evidence_block_before_delivery(self):
        user = self.event("user_message", self.prompt)
        self.write_events(self.proposal(), user)
        state = self.saved_state()
        with patch.object(issue_delivery, "api", return_value={**self.issue, "body": "Edited"}) as api:
            with self.assertRaisesRegex(dev_gate.GateError, "title/body has changed"):
                issue_delivery.check_live_approval(state)
            with self.assertRaisesRegex(dev_gate.GateError, "Approval changed"):
                issue_delivery.check_live_approval({**state, "paths": ["app/extra.py"]})
            with self.assertRaisesRegex(dev_gate.GateError, "Exactly one"):
                issue_delivery.check_live_approval({**state, "approval_comment": 42})
            with self.assertRaisesRegex(dev_gate.GateError, "Exactly one"):
                issue_delivery.check_live_approval({"issue": 44})
            self.assertEqual(api.call_count, 1)
        self.write_events(self.proposal(paths=["app/extra.py"]), user)
        with self.assertRaisesRegex(dev_gate.GateError, "no longer matches"):
            issue_delivery.check_live_approval(state)
        self.log.unlink()
        with self.assertRaisesRegex(dev_gate.GateError, "missing or ambiguous"):
            issue_delivery.check_live_approval(state)

    def test_prepare_records_active_conversation_without_github_comment(self):
        self.write_events(self.proposal(), self.event("user_message", self.prompt))
        target = self.log.parent / "worktree"
        state_file = self.log.parent / "state.json"
        args = argparse.Namespace(issue=44, state=state_file, worktree=target,
                                  codex_approval=True, approval_comment=None)
        base = "a" * 40

        def git(*args):
            if args[0] == "remote":
                return f"https://github.com/{issue_delivery.REPO}.git\n".encode()
            if args[0] == "rev-parse":
                return f"{base}\n".encode()
            self.assertIn(args[0], {"fetch", "worktree"})
            return b""

        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.thread}), \
                patch.object(issue_delivery, "api", return_value=self.issue) as api, \
                patch.object(issue_delivery, "dev_sha", return_value=base), \
                patch.object(issue_delivery, "git", side_effect=git) as git_mock:
            issue_delivery.prepare(args)
            api.assert_called_once_with("issues/44")
            git_mock.assert_any_call("worktree", "add", "-b", "issue/44", str(target), base)
        state = dev_gate.load_json(state_file)
        self.assertEqual(state["codex_approval"]["message"], self.prompt)
        self.assertEqual(state["paths"], self.scope["paths"])
        self.assertNotIn("approval_comment", state)


class TestClaudeApproval(unittest.TestCase):
    """Synthetic transcripts shaped like Claude Code 2.1 session logs."""

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.object(issue_delivery.Path, "home", return_value=self.home))
        self.session = "fedcba98-7654-4321-8fed-cba987654321"
        self.other = "01234567-89ab-4cde-8fab-0123456789ab"
        self.log = self.home / ".claude" / "projects" / "c--work-walkwithdog-BE" / f"{self.session}.jsonl"
        self.log.parent.mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {issue_delivery.CLAUDE_SESSION: self.session}))
        self.issue = {"state": "open", "number": 44, "title": "Onboarding", "body": "Approved details"}
        self.scope = {"repository": issue_delivery.REPO, "issue": 44, "title": self.issue["title"],
                      "issue_digest": issue_delivery.issue_digest(self.issue["title"], self.issue["body"]),
                      "paths": ["app/x.py"]}
        self.prompt = issue_delivery.approval_message(44)
        self.count = 0

    def record(self, kind, **fields):
        self.count += 1
        return {"type": kind, "uuid": f"00000000-0000-4000-8000-{self.count:012d}", "sessionId": self.session,
                "isSidechain": False, "userType": "external", "timestamp": f"2026-01-02T03:04:{self.count:02d}Z",
                **fields}

    def answer(self, text, message_id=None, stop="end_turn", content=None, **fields):
        message = {"id": message_id or f"msg_{self.count + 1}", "role": "assistant", "model": "claude-opus-5-5",
                   "stop_reason": stop, "content": content or [{"type": "text", "text": text}]}
        return self.record("assistant", message=message, **fields)

    def proposal(self, **edits):
        scope = {**self.scope, **edits}
        return self.answer("Review the exact scope\n```toi-approval-request\n" + json.dumps(scope) + "\n```")

    def human(self, text, content=None, **fields):
        prompt = {"origin": {"kind": "human"}, "turnOrigin": "human", "promptSource": "sdk",
                  "message": {"role": "user", "content": content or [{"type": "text", "text": text}]}}
        return self.record("user", **{**prompt, **fields})

    def tool_output(self, text):
        return self.record("user", toolUseResult={"stdout": text},
                           message={"role": "user", "content": [{"type": "tool_result", "content": text}]})

    def write(self, *rows, log=None, partial=None):
        previous = None
        for row in rows:
            if "uuid" in row:
                row.setdefault("parentUuid", previous)
                previous = row["uuid"]
        lines = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        (log or self.log).write_text(lines + (partial or ""), encoding="utf-8")

    def saved_state(self):
        approval = issue_delivery.read_claude_approval(44, self.session)
        return {"issue": 44, "issue_digest": approval["issue_digest"], "paths": approval["paths"],
                "claude_approval": approval}

    def test_typed_prompt_binds_final_answer_and_revalidates_after_later_messages(self):
        thinking = self.answer(None, message_id="msg_final", content=[{"type": "thinking", "thinking": ""}])
        final = self.proposal()
        final["message"]["id"] = "msg_final"
        ide = {"type": "text", "text": "<ide_opened_file>The user opened app/x.py</ide_opened_file>"}
        approval = self.human(None, content=[ide, {"type": "text", "text": self.prompt + "\n"}])
        rows = [self.human("Plan Issue #44"), self.tool_output("noise"), thinking, final,
                self.record("system", subtype="turn_duration"), approval]
        self.write(*rows[:4], {"type": "file-history-snapshot"}, *rows[4:])
        state = self.saved_state()
        proof = state["claude_approval"]
        self.assertEqual(proof["message"], self.prompt + "\n")
        self.assertEqual((proof["session"], proof["uuid"], proof["log"]),
                         (self.session, approval["uuid"], str(self.log)))
        self.assertEqual((proof["title"], proof["paths"]), ("Onboarding", ["app/x.py"]))
        self.write(*rows, self.answer("Working"), self.human("A later question"))
        with patch.object(issue_delivery, "api", return_value=self.issue) as api:
            issue_delivery.check_live_approval(state)
            api.assert_called_once_with("issues/44")
        with self.assertRaisesRegex(dev_gate.GateError, "not explicit approval"):
            issue_delivery.read_claude_approval(44, self.session)

    def test_assistant_tool_quoted_and_unattended_approvals_are_rejected(self):
        good = lambda: self.human(self.prompt)
        tool_call = [{"type": "text", "text": self.proposal()["message"]["content"][0]["text"]},
                     {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]
        pasted = f'<pasted_content id="ab12">\n{self.prompt}\n</pasted_content>'
        image = [{"type": "image", "source": {}}, {"type": "text", "text": self.prompt}]
        synthetic = lambda row: {**row, "message": {**row["message"], "model": "<synthetic>"}}
        cases = (
            (lambda: (self.proposal(),), "prompt is missing"),
            (lambda: (self.proposal(), self.answer(self.prompt)), "prompt is missing"),
            (lambda: (self.proposal(), self.tool_output(self.prompt)), "prompt is missing"),
            (lambda: (self.proposal(), self.human(self.prompt, isCompactSummary=True)), "prompt is missing"),
            (lambda: (self.proposal(), self.human(self.prompt, isMeta=True)), "prompt is missing"),
            (lambda: (self.proposal(), self.human(self.prompt, isSidechain=True)), "prompt is missing"),
            (lambda: (self.proposal(), self.human(self.prompt, origin={"kind": "task-notification"},
                                                  turnOrigin="task_notification", promptSource="system")),
             "prompt is missing"),
            (lambda: (self.proposal(), self.human(self.prompt, origin=None)), "prompt is missing"),
            (lambda: (self.answer(f"Send this: {self.prompt}"), self.human("ok")), "not explicit"),
            (lambda: (self.proposal(), self.human(f"> {self.prompt}")), "not explicit"),
            (lambda: (self.proposal(), self.human(f'Example: "{self.prompt}"')), "not explicit"),
            (lambda: (self.proposal(), self.human(pasted)), "not explicit"),
            (lambda: (self.proposal(), self.human(None, content=[{"type": "text", "text": self.prompt},
                                                                 {"type": "text", "text": "and more"}])),
             "not explicit"),
            (lambda: (self.proposal(), self.human(None, content=image)), "not explicit"),
            (lambda: (self.proposal(), good(), self.human("Please continue")), "not explicit"),
            (lambda: (self.proposal(), self.human("Question"), good()), "immediately follow"),
            (lambda: (self.answer(None, stop="tool_use", content=tool_call), good()), "immediately follow"),
            (lambda: ({**self.proposal(), "isApiErrorMessage": True}, good()), "immediately follow"),
            (lambda: (self.answer(None, stop="tool_use", content=tool_call[:1]), good()), "immediately follow"),
            (lambda: (synthetic(self.proposal()), good()), "immediately follow"),
            (lambda: (self.record("system", subtype="compact_boundary"), good()), "immediately follow"),
            (lambda: (self.proposal(issue=45), good()), "preceding Claude Code proposal"),
            (lambda: (self.proposal(repository="another/repo"), good()), "preceding Claude Code proposal"),
        )
        for rows, message in cases:
            with self.subTest(message=message, case=self.count):
                self.write(*rows())
                with self.assertRaisesRegex(dev_gate.GateError, message):
                    issue_delivery.read_claude_approval(44, self.session)
        incomplete = json.dumps({**good(), "parentUuid": None})
        self.write(self.proposal(), partial=incomplete)
        with self.assertRaisesRegex(dev_gate.GateError, "prompt is missing"):
            issue_delivery.read_claude_approval(44, self.session)

    def test_another_session_cannot_supply_or_reuse_approval(self):
        other_log = self.log.parent / f"{self.other}.jsonl"
        self.write(self.proposal(), self.human(self.prompt), log=other_log)
        self.write(self.proposal())
        with self.assertRaisesRegex(dev_gate.GateError, "prompt is missing"):
            issue_delivery.read_claude_approval(44, self.session)
        self.write(self.proposal(), self.human(self.prompt, sessionId=self.other))
        with self.assertRaisesRegex(dev_gate.GateError, "another session"):
            issue_delivery.read_claude_approval(44, self.session)
        self.write({**self.proposal(), "sessionId": self.other}, self.human(self.prompt))
        with self.assertRaisesRegex(dev_gate.GateError, "immediately follow"):
            issue_delivery.read_claude_approval(44, self.session)
        self.write(self.proposal(), self.human(self.prompt))
        state = self.saved_state()
        with patch.dict(os.environ, {issue_delivery.CLAUDE_SESSION: self.other}), \
                patch.object(issue_delivery, "api", return_value=self.issue) as api:
            with self.assertRaisesRegex(dev_gate.GateError, "original Claude Code session"):
                issue_delivery.check_live_approval(state)
            api.assert_not_called()
        copy = self.home / ".claude" / "projects" / "c--copy" / self.log.name
        copy.parent.mkdir()
        copy.write_bytes(self.log.read_bytes())
        with self.assertRaisesRegex(dev_gate.GateError, "missing or ambiguous"):
            issue_delivery.read_claude_approval(44, self.session)
        copy.unlink()
        for missing in (None, "", "not-a-session"):
            with self.subTest(session=missing), self.assertRaisesRegex(dev_gate.GateError, "session ID"):
                issue_delivery.read_claude_approval(44, missing)

    def test_scope_issue_log_and_state_changes_block_delivery(self):
        rows = [self.proposal(), self.human("Add a test file"), self.proposal(paths=["app/x.py", "tests/x.py"]),
                self.human(self.prompt)]
        self.write(*rows)
        self.assertEqual(self.saved_state()["paths"], ["app/x.py", "tests/x.py"])
        self.write(*rows[2:], self.proposal(paths=["app/x.py", "app/later.py"]))
        state = self.saved_state()
        self.assertEqual(state["paths"], ["app/x.py", "tests/x.py"])
        with self.assertRaisesRegex(dev_gate.GateError, "changed paths"):
            issue_delivery.check_change_scope(state, {"app/later.py"}, {"app/later.py"})
        for edited in ({**self.issue, "body": "Edited"}, {**self.issue, "title": "Renamed"}):
            with patch.object(issue_delivery, "api", return_value=edited):
                with self.assertRaisesRegex(dev_gate.GateError, "title/body has changed"):
                    issue_delivery.check_live_approval(state)
        proof = state["claude_approval"]
        tampered = (
            ({**state, "paths": ["app/x.py", "tests/x.py", "app/extra.py"]}, "Approval changed"),
            ({**state, "issue_digest": "f" * 64}, "Approval changed"),
            ({**state, "claude_approval": {**proof, "paths": ["app/x.py"]}}, "no longer matches"),
            ({**state, "claude_approval": {**proof, "message": "edited"}}, "no longer matches"),
            ({**state, "claude_approval": {**proof, "timestamp": "2026-01-01T00:00:00Z"}}, "no longer matches"),
            ({**state, "claude_approval": {**proof, "log": "elsewhere.jsonl"}}, "no longer matches"),
            ({**state, "claude_approval": {**proof, "uuid": rows[2]["uuid"]}}, "prompt is missing"),
            ({**state, "claude_approval": {**proof, "session": self.other}}, "original Claude Code session"),
            ({**state, "claude_approval": "approved"}, "unambiguous"),
            ({**state, "approval_comment": 42}, "Exactly one"),
            ({**state, "codex_approval": proof}, "Exactly one"),
        )
        with patch.object(issue_delivery, "api", return_value=self.issue):
            issue_delivery.check_live_approval(state)
            for changed, message in tampered:
                with self.subTest(message=message), self.assertRaisesRegex(dev_gate.GateError, message):
                    issue_delivery.check_live_approval(changed)
            wider = self.proposal(paths=["app/x.py", "tests/x.py", "app/extra.py"])["message"]["content"]
            self.write({**rows[2], "message": {**rows[2]["message"], "content": wider}}, rows[3])
            with self.assertRaisesRegex(dev_gate.GateError, "no longer matches"):
                issue_delivery.check_live_approval(state)
            self.log.unlink()
            with self.assertRaisesRegex(dev_gate.GateError, "missing or ambiguous"):
                issue_delivery.check_live_approval(state)

    def test_prepare_records_active_session_and_keeps_its_source(self):
        self.write(self.proposal(), self.human(self.prompt))
        target = self.log.parent / "worktree"
        state_file = self.home / "state.json"
        args = argparse.Namespace(issue=44, state=state_file, worktree=target,
                                  codex_approval=False, claude_approval=True, approval_comment=None)
        base = "a" * 40

        def git(*args):
            if args[0] == "remote":
                return f"https://github.com/{issue_delivery.REPO}.git\n".encode()
            if args[0] == "rev-parse":
                return f"{base}\n".encode()
            self.assertIn(args[0], {"fetch", "worktree"})
            return b""

        with patch.object(issue_delivery, "api", return_value=self.issue) as api, \
                patch.object(issue_delivery, "dev_sha", return_value=base), \
                patch.object(issue_delivery, "git", side_effect=git) as git_mock:
            issue_delivery.prepare(args)
            api.assert_called_once_with("issues/44")
            git_mock.assert_any_call("worktree", "add", "-b", "issue/44", str(target), base)
            state = dev_gate.load_json(state_file)
            self.assertEqual(state["claude_approval"]["message"], self.prompt)
            self.assertEqual(state["paths"], self.scope["paths"])
            self.assertNotIn("approval_comment", state)
            self.assertNotIn("codex_approval", state)
            for other_source in ({"codex_approval": True, "claude_approval": False},
                                 {"codex_approval": False, "claude_approval": False, "approval_comment": 42}):
                with self.subTest(source=other_source), self.assertRaises(dev_gate.GateError):
                    issue_delivery.prepare(argparse.Namespace(**{**vars(args), **other_source}))
            self.assertEqual(dev_gate.load_json(state_file), state)

    def test_cli_offers_three_mutually_exclusive_approval_sources(self):
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, "-m", "scripts.issue_delivery", "prepare", "44", "worktree", "--state", "s.json"]
        shown = subprocess.run([*command, "--help"], cwd=root, capture_output=True, text=True, check=True)
        for option in ("--approval-comment", "--codex-approval", "--claude-approval"):
            self.assertIn(option, shown.stdout)
        both = subprocess.run([*command, "--codex-approval", "--claude-approval"], cwd=root,
                              capture_output=True, text=True)
        self.assertEqual(both.returncode, 2)
        self.assertIn("not allowed with", both.stderr)


class TestDeliveryGate(unittest.TestCase):
    def test_independent_review_for_the_exact_head(self):
        head = "b" * 40
        evidence = {"issue": 44, "head": head, "context": "separate-review-task",
                    "summary": "Checked API contract and regression tests", "blockers": []}

        def body(review):
            return '<!-- toi-review\n' + json.dumps(review) + '\n-->'

        issue_delivery.check_reviews(body(evidence), 44, head)
        for invalid in ("No review", body(evidence) + body(evidence),
                        body({**evidence, "issue": 45}), body({**evidence, "head": "c" * 40}),
                        body({**evidence, "context": " "}), body({**evidence, "summary": None}),
                        body({**evidence, "blockers": ["Potential data loss"]}),
                        body({**evidence, "blockers": None})):
            with self.subTest(body=invalid), self.assertRaises(dev_gate.GateError):
                issue_delivery.check_reviews(invalid, 44, head)

    def test_no_release_with_protection_bypass(self):
        rules = {
            "required_status_checks": {"strict": True, "checks": [{"context": issue_delivery.CHECK, "app_id": 15368}]},
            "enforce_admins": {"enabled": True}, "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_pull_request_reviews": {"required_approving_review_count": 0},
        }
        issue_delivery.check_protection(rules)
        for change in ({"required_pull_request_reviews": None},
                       {"required_pull_request_reviews": {"required_approving_review_count": 1}},
                       {"required_pull_request_reviews": {"required_approving_review_count": 0,
                                                          "require_code_owner_reviews": True}},
                       {"required_pull_request_reviews": {"required_approving_review_count": 0,
                                                          "require_last_push_approval": True}},
                       {"required_pull_request_reviews": {"required_approving_review_count": 0,
                                                          "bypass_pull_request_allowances": {"users": ["maintainer"]}}},
                       {"enforce_admins": {"enabled": False}}, {"allow_force_pushes": {"enabled": True}},
                       {"allow_deletions": {"enabled": True}}):
            with self.subTest(change=change), self.assertRaises(dev_gate.GateError):
                issue_delivery.check_protection({**rules, **change})
        for change in ({"required_status_checks": {"strict": False, "checks": rules["required_status_checks"]["checks"]}},
                       {"required_status_checks": {"strict": True,
                                                   "checks": [{"context": issue_delivery.CHECK, "app_id": 1}]}},
                       {"required_linear_history": {"enabled": True}}):
            with self.subTest(change=change), self.assertRaises(dev_gate.GateError):
                issue_delivery.check_protection({**rules, **change})
        issue_delivery.check_protection({**rules, "required_status_checks": None}, "main")
        with self.assertRaises(dev_gate.GateError):
            issue_delivery.check_protection({}, "main")

    def test_preview_uses_review_record_without_github_approvals(self):
        number, issue = 68, 44
        base, head = "a" * 40, "b" * 40
        branch = f"issue/{issue}"
        repo = {"full_name": issue_delivery.REPO}
        scope = {"issue": issue, "paths": ["app/x.py"]}
        evidence = {"issue": issue, "head": head, "context": "separate-review-task",
                    "summary": "Reviewed the change and its tests", "blockers": []}
        body = '<!-- toi-scope\n' + json.dumps(scope) + '\n-->\n'
        body += '<!-- toi-review\n' + json.dumps(evidence) + '\n-->'
        pr = {"body": body, "base": {"ref": "dev", "sha": base, "repo": repo},
              "head": {"ref": branch, "sha": head, "repo": repo}, "merged": False,
              "state": "open", "draft": False, "mergeable": True, "mergeable_state": "clean"}
        protection = {"enforce_admins": {"enabled": True}, "allow_force_pushes": {"enabled": False},
                      "allow_deletions": {"enabled": False},
                      "required_pull_request_reviews": {"required_approving_review_count": 0}}
        responses = {
            f"pulls/{number}": pr,
            f"compare/{base}...{head}": {"merge_base_commit": {"sha": base}},
            "branches/main/protection": protection,
            "branches/dev/protection": {**protection, "required_status_checks": {
                "strict": True, "checks": [{"context": issue_delivery.CHECK, "app_id": 15368}],
            }},
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            issue_delivery.write_state(state, {**scope, "pr": number, "branch": branch})
            saved = state.read_bytes()
            args = argparse.Namespace(pr=number, state=state, execute=False)
            with patch.object(issue_delivery, "api", side_effect=responses.__getitem__) as api, \
                    patch.object(issue_delivery, "pull_paths", return_value={"app/x.py"}), \
                    patch.object(issue_delivery, "check_live_approval") as approval, \
                    patch.object(issue_delivery, "dev_sha", return_value=base), \
                    patch.object(issue_delivery, "check_ci") as ci:
                issue_delivery.deliver(args)
                approval.assert_called_once()
                ci.assert_called_once_with(number, base, head)
                self.assertEqual(api.call_count, 4)
            self.assertEqual(state.read_bytes(), saved)

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
