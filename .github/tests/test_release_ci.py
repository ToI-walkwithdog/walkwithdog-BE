"""Unit and blocking tests for the Release CI verifier. Uses synthetic data only."""
import base64
from contextlib import redirect_stdout
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts" / "release_ci.py"
SPEC = importlib.util.spec_from_file_location("release_ci", MODULE)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)

_ENVIRONMENT = patch.dict(os.environ)


def setUpModule():
    # Synthetic runs must never write a job summary or workflow commands of a real run.
    _ENVIRONMENT.start()
    for key in ("GITHUB_STEP_SUMMARY", "GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_ENV"):
        os.environ.pop(key, None)


def tearDownModule():
    _ENVIRONMENT.stop()


HEAD = "b" * 40
MAIN = "a" * 40
TREE = "c" * 40
DIGEST = "d" * 64
REVIEW = f"https://github.com/{ci.REPOSITORY}/pull/9#issuecomment-1"


def entries(items):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for item in items:
            name, content = item[:2]
            info = tarfile.TarInfo(name)
            if len(item) == 3:
                info.type, info.linkname = item[2]
                archive.addfile(info)
                continue
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


class Repo:
    """Throwaway Git repository for exact change-list and static inventory checks."""

    def __init__(self, path):
        self.path = path
        for args in (["init", "-q", "-b", "main"], ["config", "user.name", "Synthetic"],
                     ["config", "user.email", "synthetic@example.invalid"], ["config", "core.autocrlf", "false"],
                     ["config", "commit.gpgsign", "false"]):
            self.git(*args)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.path), *args], text=True).strip()

    def commit(self, files, message="synthetic"):
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


class RepoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Repo(Path(temporary.name))


class ScopeTests(RepoTest):
    def test_change_list_covers_modify_delete_and_rename(self):
        base = self.repo.commit({"a.txt": "one", "b.txt": "two", "c.txt": "three"})
        head = self.repo.commit({"a.txt": "changed", "b.txt": None, "c.txt": None, "d.txt": "three"})
        tree = ci.merge_tree(self.repo.path, base, head)
        pairs = ci.changes(self.repo.path, base, tree)
        self.assertEqual(pairs, [("a.txt", "M"), ("b.txt", "D"), ("c.txt", "D"), ("d.txt", "A")])
        digest = ci.change_digest(pairs)
        self.assertNotEqual(digest, ci.change_digest(pairs[:-1]))
        self.assertNotEqual(digest, ci.change_digest([*pairs[:-1], ("d.txt", "M")]))

    def test_conflicting_candidate_is_blocked(self):
        base = self.repo.commit({"a.txt": "one\n"})
        self.repo.git("checkout", "-q", "-b", "candidate")
        head = self.repo.commit({"a.txt": "candidate\n"})
        self.repo.git("checkout", "-q", "main")
        main = self.repo.commit({"a.txt": "main\n"})
        self.assertNotEqual(base, main)
        with self.assertRaisesRegex(ci.Blocked, "merge cleanly"):
            ci.merge_tree(self.repo.path, main, head)

    def test_pinned_inputs_must_match_byte_for_byte(self):
        files = {path: f"reviewed {path}\n" for path in ci.INPUTS}
        commit = self.repo.commit(files)
        doc = {"files": {path: ci.sha(content.encode()) for path, content in files.items()}}
        self.assertEqual(ci.input_findings(self.repo.path, commit, doc), [])
        changed = self.repo.commit({"Dockerfile": "FROM scratch\n", ci.BASELINE: "{}\n",
                                    "scripts/dev_gate.py": None})
        findings = ci.input_findings(self.repo.path, changed, doc)
        self.assertEqual(len(findings), 2)
        self.assertTrue(any("Dockerfile" in finding for finding in findings))
        self.assertTrue(any("dev_gate.py" in finding for finding in findings))


class HistoricalTests(RepoTest):
    SOURCE = (
        "import unittest\n\n"
        "def test_top():\n    pass\n\n"
        "async def test_async():\n    pass\n\n"
        "def helper():\n    def test_nested():\n        pass\n\n"
        "class Cases(unittest.TestCase):\n"
        "    def test_one(self):\n        pass\n\n"
        "    def setUp(self):\n        pass\n"
    )

    def setUp(self):
        super().setUp()
        self.commit = self.repo.commit({"tests/test_sample.py": self.SOURCE, "tests/helpers.py": "def test_x(): pass\n",
                                        "tests/sub/test_more.py": "def test_more():\n    pass\n"})
        self.baseline = {"collected": ["tests/test_sample.py::test_top",
                                       "tests/test_sample.py::Cases::test_one[case]",
                                       "tests/sub/test_more.py::test_more",
                                       "tests/test_new.py::test_replacement"]}

    def record(self, **changes):
        return {"commit": self.commit, "definitions": 4, "mappings": {
            "tests/test_sample.py::test_async": {"reason": "Synthetic", "commit": "e" * 40,
                                                 "tests": ["tests/test_new.py::test_replacement"]}}, **changes}

    def test_static_definitions_without_import(self):
        self.assertEqual(ci.historical_definitions(self.repo.path, self.commit), {
            "tests/test_sample.py::test_top", "tests/test_sample.py::test_async",
            "tests/test_sample.py::Cases::test_one", "tests/sub/test_more.py::test_more"})
        with redirect_stdout(io.StringIO()):
            ci.historical_inventory(self.repo.path, self.record(), self.baseline)

    def test_unmapped_stale_or_uncovered_tests_are_blocked(self):
        cases = (
            (self.record(definitions=5), "expected 5"),
            (self.record(mappings={}), "Unmapped"),
            (self.record(mappings={**self.record()["mappings"],
                                   "tests/test_sample.py::test_top": {"reason": "x", "commit": "e" * 40,
                                                                      "tests": ["tests/test_new.py::test_replacement"]}}),
             "stale"),
            (self.record(mappings={"tests/test_sample.py::test_async": {
                "reason": "x", "commit": "e" * 40, "tests": ["tests/test_new.py::test_missing"]}}),
             "replacement coverage"),
        )
        for record, message in cases:
            with self.subTest(message=message), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ci.Blocked, message):
                    ci.historical_inventory(self.repo.path, record, self.baseline)


class MigrationTests(RepoTest):
    def manifest(self, *rows):
        return json.dumps({"version": 1, "migrations": list(rows), "reservations": []})

    def row(self, name, content):
        return {"file": name, "sha256": ci.sha(content.encode()), "requires": []}

    def setUp(self):
        super().setUp()
        self.sql = "CREATE TABLE sample (id integer);\n"
        self.reference = self.repo.commit({"migrations/001_sample.sql": self.sql,
                                           ci.MANIFEST: self.manifest(self.row("001_sample.sql", self.sql))})
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tools = Path(temporary.name)
        (self.tools / "scripts").mkdir()
        (self.tools / "scripts/check_migrations.py").write_text("import sys\nsys.exit(0)\n")

    def check(self, candidate):
        with patch.object(ci, "REFERENCE", self.reference):
            ci.migration_checks(self.repo.path, self.tools, self.tools, self.reference, candidate)

    def test_new_migration_is_accepted(self):
        added = "CREATE TABLE more (id integer);\n"
        self.check(self.repo.commit({"migrations/002_more.sql": added, ci.MANIFEST: self.manifest(
            self.row("001_sample.sql", self.sql), self.row("002_more.sql", added))}))

    def test_sql_and_manifest_changed_together_are_blocked(self):
        altered = "DROP TABLE users;\n"
        candidate = self.repo.commit({"migrations/001_sample.sql": altered,
                                      ci.MANIFEST: self.manifest(self.row("001_sample.sql", altered))})
        with self.assertRaisesRegex(ci.Blocked, "approved migration"):
            self.check(candidate)

    def test_sql_change_or_removal_is_blocked(self):
        for files in ({"migrations/001_sample.sql": "DROP TABLE users;\n"}, {"migrations/001_sample.sql": None}):
            with self.subTest(files=files):
                self.repo.git("reset", "-q", "--hard", self.reference)
                with self.assertRaisesRegex(ci.Blocked, "Approved migration SQL"):
                    self.check(self.repo.commit(files))

    def test_manifest_record_changes_are_blocked(self):
        record = {"file": "015_review.sql", "sha256": "1" * 64, "depends_on": ["014_previous.sql"]}
        for candidate in ({"migrations": []}, {"migrations": [{**record, "sha256": "2" * 64}]}):
            with self.assertRaisesRegex(ci.Blocked, "approved migration"):
                ci.protect_migrations({"migrations": [record]}, candidate)


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((ROOT / ci.TRUST).read_text(encoding="utf-8"))
        self.files = {path: f"reviewed {path}".encode() for path in ci.INPUTS}

    def blob(self, document):
        def read(_, treeish, path):
            if path == ci.TRUST:
                return json.dumps(document).encode() if isinstance(document, dict) else document
            self.assertEqual(treeish, ci.REFERENCE)
            return self.files[path]
        return read

    def synthetic(self, **changes):
        doc = json.loads(json.dumps(self.document))
        doc["files"] = {path: ci.sha(content) for path, content in self.files.items()}
        doc.update(changes)
        return doc

    def test_committed_record_keeps_reference_and_adds_exact_exception(self):
        doc = self.document
        self.assertEqual((doc["issue"], doc["base"], doc["reference"], doc["dev_quality_run"]),
                         (125, ci.BASE, ci.REFERENCE, 45602907730))
        self.assertEqual(ci.REFERENCE, "f8546096672a697e8607d6f12e4766767984d2de")
        self.assertEqual(set(doc["files"]), ci.INPUTS)
        ci.check_history_record(doc["historical_main"])
        ci.check_exception_record(doc["image_exception"])
        self.assertEqual(doc["historical_main"]["definitions"], 364)
        self.assertEqual(len(doc["historical_main"]["mappings"]), 2)
        keys = doc["image_exception"]["keys"]
        self.assertEqual(len(keys), 6)
        self.assertIn("DSA PRIVATE KEY", {key["label"] for key in keys})

    def test_reference_pins_are_rechecked(self):
        with patch.object(ci, "blob", side_effect=self.blob(self.synthetic())):
            ci.load_trust(Path("."), "f" * 40)
        tampered = self.synthetic()
        tampered["files"]["requirements.txt"] = "0" * 64
        changed_exception = self.synthetic()
        changed_exception["image_exception"]["layer"] = "sha256:" + "0" * 64
        weakened = self.synthetic()
        weakened["image_exception"]["keys"].append(dict(weakened["image_exception"]["keys"][0]))
        cases = ((tampered, "checksum mismatch"), (self.synthetic(reference="e" * 40), "installation reference"),
                 (self.synthetic(extra=True), "fields"), (changed_exception, "exception target"),
                 (weakened, "fingerprints"),
                 (b'{"issue": 125, "issue": 125}', "Duplicate"))
        for document, message in cases:
            with self.subTest(message=message), patch.object(ci, "blob", side_effect=self.blob(document)):
                with self.assertRaisesRegex(ci.Blocked, message):
                    ci.load_trust(Path("."), "f" * 40)


class ApprovalTests(unittest.TestCase):
    def record(self, **changes):
        data = {"pr": 9, "base": MAIN, "head": HEAD, "merge_tree": TREE, "changes": DIGEST,
                "review": {"head": HEAD, "record": REVIEW, "blocking": 0}}
        data.update(changes)
        return data

    def comment(self, record=None, body=None, **changes):
        body = body if body is not None else "Approved.\n```toi-release-approval\n" + json.dumps(record or self.record()) + "\n```\n"
        data = {"id": 77, "issue_url": f"https://api.github.com/repos/{ci.REPOSITORY}/issues/9",
                "user": {"login": "release-admin", "type": "User"}, "created_at": "2026-11-25T10:00:00Z",
                "updated_at": "2026-11-25T10:00:00Z", "body": body}
        data.update(changes)
        return data

    def permission(self, login="release-admin", level="admin"):
        return {"permission": level, "user": {"login": login}}

    def env(self, **changes):
        data = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                "GITHUB_REPOSITORY": ci.REPOSITORY,
                "GITHUB_WORKFLOW_REF": f"{ci.REPOSITORY}/{ci.WORKFLOW}@refs/heads/main",
                "GITHUB_ACTOR": "release-admin", "GITHUB_TRIGGERING_ACTOR": "release-admin"}
        data.update(changes)
        return data

    def test_valid_administrator_approval(self):
        self.assertEqual(ci.approval_record(self.comment(), 9, self.permission()), self.record())
        self.assertEqual(ci.dispatcher(self.env(), self.permission()), "release-admin")

    def test_dispatch_inputs_alone_are_not_approval(self):
        with self.assertRaisesRegex(ci.Blocked, "exactly one"):
            ci.approval_record(self.comment(body="pr_number=9 approval_comment_id=77"), 9, self.permission())

    def test_invalid_approval_comments_are_blocked(self):
        review = self.record()["review"]
        cases = (
            (self.comment(), self.permission(level="write"), "administrator"),
            (self.comment(), self.permission(login="someone-else"), "administrator"),
            (self.comment(user={"login": "release-bot", "type": "Bot"}), self.permission("release-bot"), "person"),
            (self.comment(updated_at="2026-11-25T11:00:00Z"), self.permission(), "Edited"),
            (self.comment(issue_url=f"https://api.github.com/repos/{ci.REPOSITORY}/issues/8"), self.permission(),
             "another issue"),
            (self.comment(body=self.comment()["body"] * 2), self.permission(), "exactly one"),
            (self.comment(self.record(extra=1)), self.permission(), "fields"),
            (self.comment(self.record(review={**review, "head": MAIN})), self.permission(), "independent review"),
            (self.comment(self.record(review={**review, "blocking": 1})), self.permission(), "independent review"),
            (self.comment(self.record(review={**review, "blocking": False})), self.permission(), "independent review"),
            (self.comment(self.record(review={**review, "record": "https://example.com/review"})), self.permission(),
             "independent review"),
        )
        for comment, permission, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ci.Blocked, message):
                ci.approval_record(comment, 9, permission)

    def test_dispatcher_must_be_an_administrator_on_protected_main(self):
        cases = (
            (self.env(GITHUB_REF="refs/heads/dev"), self.permission(), "protected main"),
            (self.env(GITHUB_EVENT_NAME="pull_request"), self.permission(), "protected main"),
            (self.env(GITHUB_WORKFLOW_REF=f"{ci.REPOSITORY}/{ci.WORKFLOW}@refs/heads/dev"), self.permission(),
             "protected main"),
            (self.env(GITHUB_TRIGGERING_ACTOR="other-admin"), self.permission(), "triggering actor"),
            (self.env(), self.permission(level="maintain"), "administrators"),
        )
        for env, permission, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ci.Blocked, message):
                ci.dispatcher(env, permission)

    def test_pull_request_state_and_origin(self):
        repo = {"full_name": ci.REPOSITORY}
        pr = {"number": 9, "state": "open", "merged": False, "base": {"ref": "main", "repo": repo},
              "head": {"sha": HEAD, "repo": repo}}
        ci.check_pull_request(pr, 9, HEAD)
        for changed in ({"state": "closed"}, {"head": {"sha": HEAD, "repo": {"full_name": "fork/repo"}}},
                        {"head": {"sha": HEAD, "repo": None}}, {"base": {"ref": "dev", "repo": repo}}):
            with self.subTest(changed=changed), self.assertRaises(ci.Blocked):
                ci.check_pull_request({**pr, **changed}, 9, HEAD)
        with self.assertRaises(ci.Blocked):
            ci.check_pull_request(pr, 9, "e" * 40)

    def test_collect_fetches_permissions_for_validated_logins_only(self):
        calls = []

        def fetch(path, token):
            calls.append(path)
            if "/issues/comments/" in path:
                return self.comment()
            return {"permission": "admin", "user": {"login": path.split("/")[-2]}}

        env = {"GH_TOKEN": "synthetic", "PR_NUMBER": "9", "APPROVAL_COMMENT_ID": "77",
               "GITHUB_TRIGGERING_ACTOR": "release-admin"}
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            ci.collect(Path(temp) / "evidence", env, fetch)
            self.assertEqual(sorted(p.name for p in (Path(temp) / "evidence").iterdir()),
                             ["approval.json", "approver.json", "dispatcher.json", "pr.json"])
        self.assertIn(f"repos/{ci.REPOSITORY}/collaborators/release-admin/permission", calls)
        for changes in ({"PR_NUMBER": "9; rm -rf /"}, {"APPROVAL_COMMENT_ID": "0"}, {"GH_TOKEN": ""},
                        {"GITHUB_TRIGGERING_ACTOR": "../admin"}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ci.Blocked):
                    ci.collect(Path(temp) / "evidence", {**env, **changes}, fetch)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ci.Blocked, "approval author"):
                ci.collect(Path(temp) / "evidence", env,
                           lambda path, token: self.comment(user={"login": "../x", "type": "User"}))


class ReleaseFlowTests(ApprovalTests):
    """The official run stops before any candidate execution when evidence does not match."""

    def run_release(self, record=None, refs=None, pairs=(("app/main.py", "M"),), comment_id="77", final_refs=None):
        repo = {"full_name": ci.REPOSITORY}
        evidence = {"pr": {"number": 9, "state": "open", "merged": False, "base": {"ref": "main", "repo": repo},
                           "head": {"sha": HEAD, "repo": repo}},
                    "approval": self.comment(record or self.record()), "dispatcher": self.permission(),
                    "approver": self.permission()}
        expected_refs = {"main": MAIN, "head": HEAD}
        ref_answers = [refs or expected_refs, final_refs or expected_refs]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name, document in evidence.items():
                (folder / f"{name}.json").write_text(json.dumps(document))
            env = {**self.env(), "PR_NUMBER": "9", "APPROVAL_COMMENT_ID": comment_id, "GITHUB_SHA": MAIN}
            with patch.object(ci, "rev_parse", side_effect=lambda root, ref: MAIN if ref == "HEAD" else HEAD), \
                    patch.object(ci, "git"), patch.object(ci, "ensure_commit"), \
                    patch.object(ci, "remote_refs", side_effect=ref_answers), \
                    patch.object(ci, "merge_tree", return_value=TREE), \
                    patch.object(ci, "changes", return_value=list(pairs)), \
                    patch.object(ci, "change_digest", return_value=DIGEST), \
                    patch.object(ci, "load_trust", return_value={"files": {}}), \
                    patch.object(ci, "input_findings", return_value=[]), \
                    patch.object(ci, "verify", return_value=[("synthetic", True, "")]) as verify, \
                    redirect_stdout(io.StringIO()):
                try:
                    ci.release(Path("."), folder, env)
                finally:
                    self.verify_calls = verify.call_count

    def test_matching_approval_runs_verification(self):
        self.run_release()
        self.assertEqual(self.verify_calls, 1)

    def test_changed_candidate_is_not_verified(self):
        cases = (
            ({"record": self.record(merge_tree="e" * 40)}, "merge_tree"),
            ({"record": self.record(head="e" * 40, review={"head": "e" * 40, "record": REVIEW, "blocking": 0})},
             "head"),
            ({"record": self.record(changes="0" * 64)}, "changes"),
            ({"record": self.record(base="e" * 40)}, "base"),
            ({"refs": {"main": "e" * 40, "head": HEAD}}, "changed; rerun"),
            ({"pairs": (("scripts/release_ci.py", "M"),)}, "maintenance PR"),
            ({"comment_id": "78"}, "dispatch input"),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                self.verify_calls = None
                with self.assertRaisesRegex(ci.Blocked, message):
                    self.run_release(**arguments)
                self.assertIn(self.verify_calls, (None, 0))

    def test_moved_refs_after_verification_fail_the_verdict(self):
        with self.assertRaisesRegex(ci.Blocked, "Base and Head unchanged"):
            self.run_release(final_refs={"main": MAIN, "head": "e" * 40})


class InstallationTests(unittest.TestCase):
    def event(self, **head):
        repo = {"full_name": ci.REPOSITORY}
        return {"pull_request": {"number": 127, "base": {"sha": ci.BASE, "ref": "main", "repo": repo},
                                 "head": {"sha": HEAD, "ref": "chore/release-ci", "repo": repo, **head}}}

    def run_installation(self, event=None, installed=False, pairs=None):
        pairs = pairs if pairs is not None else sorted((path, "A") for path in ci.PATHS)
        with patch.object(ci, "has_path", return_value=installed), \
                patch.object(ci, "rev_parse", side_effect=lambda root, ref: TREE if "tree" in ref else "f" * 40), \
                patch.object(ci, "git", side_effect=lambda root, *args: (
                    f"{'f' * 40} {ci.BASE} {HEAD}".encode() if args[0] == "rev-list" else b"content")), \
                patch.object(ci, "merge_tree", return_value=TREE), patch.object(ci, "changes", return_value=pairs), \
                patch.object(ci, "ensure_commit"), patch.object(ci, "load_trust", return_value={}), \
                patch.object(ci, "verify", return_value=[("synthetic", True, "")]) as verify, \
                redirect_stdout(io.StringIO()):
            ci.installation(Path("."), event or self.event())
            return verify

    def test_exact_installation_verifies_the_reference_snapshot(self):
        verify = self.run_installation()
        self.assertEqual(verify.call_args.args[1], ci.REFERENCE)

    def test_installation_scope_is_exact(self):
        paths = sorted((path, "A") for path in ci.PATHS)
        cases = (
            ({"pairs": [*paths, ("requirements.txt", "M")]}, "four approved paths"),
            ({"pairs": paths[1:]}, "four approved paths"),
            ({"pairs": [(paths[0][0], "M"), *paths[1:]]}, "four approved paths"),
            ({"installed": True}, "already installed"),
            ({"event": self.event(ref="feature/other")}, "Unexpected installation PR"),
            ({"event": self.event(repo={"full_name": "fork/repo"})}, "Unexpected installation PR"),
        )
        for arguments, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ci.Blocked, message):
                self.run_installation(**arguments)


class PreflightTests(unittest.TestCase):
    def test_preflight_reads_git_objects_only(self):
        repo = {"full_name": ci.REPOSITORY}
        event = {"pull_request": {"number": 9, "base": {"sha": MAIN, "ref": "main", "repo": repo},
                                  "head": {"sha": HEAD, "repo": repo}}}
        forbidden = AssertionError("candidate execution in preflight")
        for pairs, findings in (([("app/main.py", "M")], []), ([(ci.WORKFLOW, "M")], ["maintenance PR"])):
            with self.subTest(pairs=pairs), \
                    patch.object(ci, "rev_parse", side_effect=lambda root, ref: MAIN if ref == "HEAD" else HEAD), \
                    patch.object(ci, "git"), patch.object(ci, "ensure_commit"), \
                    patch.object(ci, "merge_tree", return_value=TREE), \
                    patch.object(ci, "changes", return_value=pairs), \
                    patch.object(ci, "load_trust", return_value={}), \
                    patch.object(ci, "static_findings", return_value=[]), \
                    patch.object(ci, "docker", side_effect=forbidden), \
                    patch.object(ci, "run_candidate", side_effect=forbidden), \
                    patch.object(ci, "smoke", side_effect=forbidden):
                output = io.StringIO()
                with redirect_stdout(output):
                    if findings:
                        with self.assertRaisesRegex(ci.Blocked, findings[0]):
                            ci.preflight(Path("."), event)
                    else:
                        ci.preflight(Path("."), event)
                self.assertIn("```toi-release-approval", output.getvalue())


class ResultTests(unittest.TestCase):
    def test_missing_core_pass_and_new_skip_rejected(self):
        event = {"nodeid": "example", "phase": "call", "subtest": None, "outcome": "passed"}
        report = {"exit_code": 0, "collected": ["example"], "events": [event]}
        ci.require_passes(["example"], report)
        for rejected in ({**report, "events": []},
                         {**report, "events": [{**event, "outcome": "skipped"}]},
                         {**report, "collected": ["example", "unexpected"]},
                         {**report, "exit_code": 1}):
            with self.assertRaises(ci.Blocked):
                ci.require_passes(["example"], rejected)

    def test_baseline_skip_inventory_rejects_changed_groups(self):
        baseline = {"skips": [
            {"nodeid": f"{path}::test_{index}", "message": prefix + "_TEST_DATABASE_URL"}
            for path, (prefix, count) in ci.CORE_GROUPS.items() for index in range(count)
        ]}
        groups = ci.core_inventory(baseline)
        self.assertEqual(sum(map(len, groups.values())), 82)
        baseline["skips"][0]["message"] = "candidate skip"
        with self.assertRaises(ci.Blocked):
            ci.core_inventory(baseline)

    def suite(self, gate_status, clean=True):
        with tempfile.TemporaryDirectory() as temp:
            scratch = Path(temp)
            work, out = scratch / "work", scratch / "out"
            work.mkdir()
            out.mkdir()

            def candidate(*args):
                # A candidate can print success text and write any report it likes.
                print("::notice::Protected-main release verification passed")
                (out / "report.json").write_text(json.dumps({"exit_code": 0, "collected": [], "events": []}))
                return 0

            with patch.object(ci, "phase_dir", return_value=(work, out)), \
                    patch.object(ci, "create_database"), patch.object(ci, "database_clean", return_value=clean), \
                    patch.object(ci, "run_candidate", side_effect=candidate), \
                    patch.object(ci, "blob", return_value=b"{}"), \
                    patch.object(ci.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], gate_status)) as gate, \
                    redirect_stdout(io.StringIO()):
                ci.full_suite(Path("."), TREE, scratch / "reference", scratch, "image", "pg")
            command = gate.call_args.args[0]
            self.assertEqual(command[3:5], [str(scratch / "reference/scripts/dev_gate.py"), "tests"])
            self.assertEqual(command[command.index("--base") + 1], str(scratch / "reference" / ci.BASELINE))

    def test_verdict_comes_from_the_reference_gate_not_candidate_output(self):
        self.suite(0)
        with self.assertRaisesRegex(ci.Blocked, "protected baseline"):
            self.suite(1)

    def test_leftover_test_data_fails_even_when_the_gate_passes(self):
        with self.assertRaisesRegex(ci.Blocked, "remained"):
            self.suite(0, clean=False)

    def test_candidate_runs_stop_when_isolation_probe_fails(self):
        with patch.object(ci, "start_postgres", return_value="pg"), patch.object(ci, "quiet"), \
                patch.object(ci, "probe", side_effect=ci.Blocked("Isolation probe: dns was allowed")), \
                patch.object(ci, "full_suite", side_effect=AssertionError("ran candidate")), \
                patch.object(ci, "core_group", side_effect=AssertionError("ran candidate")), \
                redirect_stdout(io.StringIO()):
            results = ci.candidate_runs(Path("."), TREE, Path("."), Path("."), "image", {"tests/x.py": ["n"]})
        self.assertEqual([(name, ok) for name, ok, _ in results], [("candidate isolation probe", False)])


class IsolationTests(unittest.TestCase):
    def test_candidate_container_settings(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic", "ACTIONS_RUNTIME_TOKEN": "synthetic"}):
            args = ci.candidate_command("image", "pg", Path("/w"), Path("/r"), Path("/o"),
                                        ci.CANDIDATE_ENV, ["python", "-B", "-c", "pass"])
        text = " ".join(args)
        for expected in ("--network container:pg", "--user 65534:65534", "--cap-drop ALL",
                         "--security-opt no-new-privileges", "--pids-limit", "--read-only",
                         f"source={Path('/r')},target=/reference,readonly"):
            self.assertIn(expected, text)
        for forbidden in ("docker.sock", "--privileged", "--network host", "GITHUB_", "ACTIONS_", "TOKEN"):
            self.assertNotIn(forbidden, text)

    def probe_result(self, **changes):
        result = {"tcp_external": "blocked", "tcp_public": "blocked", "dns": "blocked",
                  "write_reference": "blocked", "write_root": "blocked", "superuser": "blocked",
                  "test_role": "allowed", "write_out": "allowed", "interfaces": ["lo"], "docker_socket": False,
                  "uid": 65534, "cap_eff": "0000000000000000", "no_new_privs": "1",
                  "env": sorted([*ci.CANDIDATE_ENV, "HOSTNAME", "GPG_KEY", "PYTHON_VERSION"])}
        result.update(changes)
        return result

    def test_isolation_probe_blocks_each_escape(self):
        ci.evaluate_probe(self.probe_result())
        cases = ({"tcp_external": "allowed"}, {"dns": "allowed"}, {"interfaces": ["eth0", "lo"]},
                 {"write_reference": "allowed"}, {"superuser": "allowed"}, {"uid": 0},
                 {"cap_eff": "00000000a80425fb"}, {"no_new_privs": "0"}, {"docker_socket": True},
                 {"env": ["GITHUB_TOKEN", "PATH"]}, {"env": ["ACTIONS_ID_TOKEN_REQUEST_URL"]},
                 {"env": ["AWS_SECRET_ACCESS_KEY"]}, {"test_role": "blocked"})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                ci.evaluate_probe(self.probe_result(**changes))

    def test_postgres_has_no_network_and_a_non_superuser_test_role(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(ci, "docker", side_effect=lambda *args, **kw: calls.append(args)), \
                patch.object(ci, "quiet", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(ci, "psql", side_effect=lambda c, d, *s: calls.append(s) or "160016"):
            ci.start_postgres(Path(temp) / "pg")
            hba = (Path(temp) / "pg/pg_hba.conf").read_text()
        run = " ".join(calls[0])
        self.assertIn("--network none", run)
        self.assertIn("--cap-drop ALL", run)
        self.assertEqual(hba, "local all postgres trust\nhost all toi_ci 127.0.0.1/32 trust\n")
        self.assertTrue(any("NOSUPERUSER" in statement for call in calls[1:] for statement in call))

    def test_trusted_queries_resolve_names_from_pg_catalog_only(self):
        with patch.object(ci, "docker", return_value="0") as docker:
            ci.psql("pg", "toi_walk_test_ci_signup", "SELECT 1")
        command = list(docker.call_args.args)
        self.assertLess(command.index("SET search_path = pg_catalog"), command.index("SELECT 1"))
        self.assertEqual(command[command.index("SET search_path = pg_catalog") - 1], "-c")

    def reclaim(self, scratch, uid=1001, owner=1001):
        with patch.object(ci.os, "geteuid", create=True, return_value=uid), \
                patch.object(ci.os, "getegid", create=True, return_value=uid), \
                patch.object(ci, "owner", return_value=owner), \
                patch.object(ci.subprocess, "run") as run:
            ci.reclaim(scratch)
        return run

    def test_reclaim_changes_only_the_run_scratch_without_following_links(self):
        with tempfile.TemporaryDirectory(prefix="toi-release-") as temp:
            run = self.reclaim(Path(temp))
            self.assertEqual(run.call_args.args[0], [
                "sudo", "-n", "chown", "-R", "-P", "--no-dereference", "--preserve-root", "1001:1001", "--",
                str(Path(temp).resolve())])
            self.assertEqual(self.reclaim(Path(temp), uid=0, owner=0).call_count, 0)
            nested = Path(temp) / "toi-release-nested"
            nested.mkdir()
            for target, kwargs in ((nested, {}), (Path(temp), {"owner": 0})):
                with self.subTest(target=target, kwargs=kwargs), self.assertRaisesRegex(ci.Blocked, "outside"):
                    self.reclaim(target, **kwargs)
        with tempfile.TemporaryDirectory(prefix="other-") as temp:
            with self.assertRaisesRegex(ci.Blocked, "outside"):
                self.reclaim(Path(temp))
            link = Path(tempfile.gettempdir()) / f"toi-release-link-{os.getpid()}"
            try:
                link.symlink_to(temp, target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks are unavailable on this platform")
            try:
                with self.assertRaisesRegex(ci.Blocked, "real directory"):
                    self.reclaim(link)
            finally:
                link.unlink()

    def test_candidate_containers_wait_for_the_network_check(self):
        self.assertEqual(ci.compose_services(["up", "-d", "--wait", "--wait-timeout", "120", "db"]), ["db"])
        self.assertEqual(ci.compose_services(["start", "--wait", "--wait-timeout", "120"]), [])
        calls = []

        def docker(*args, capture=False):
            calls.append(args)
            if not capture:
                return None
            if args[-2:] == ("-q", "api"):
                return ""
            if "ps" in args:
                return "db1"
            if args[:2] == ("inspect", "--format"):
                return json.dumps({"toi_default": {}})
            return json.dumps([{"HostConfig": {}, "Mounts": []}])

        prefix = ["compose", "--project-name", "toi", "--file", "docker-compose.yml"]
        base = "sha256:" + "1" * 64
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "exception.json").write_text(json.dumps({"base_manifest": base}))
            env = {"TOI_SMOKE_SCRATCH": temp, "TOI_SMOKE_EXCEPTION": str(Path(temp) / "exception.json")}
            with patch.dict(os.environ, env), patch.object(ci, "docker", side_effect=docker), \
                    patch.object(ci, "bridge_probe") as probe:
                for operation in (["run", "--rm", "api", "python"], ["exec", "-T", "api", "id"],
                                  ["up", "-d", "--wait", "--no-build"], ["start", "--wait"]):
                    with self.subTest(operation=operation), self.assertRaisesRegex(ci.Blocked, "network isolation"):
                        ci.smoke_docker([*prefix, *operation])
                self.assertEqual(calls, [])
                ci.smoke_docker([*prefix, "up", "-d", "--wait", "--wait-timeout", "120", "db"])
                probe.assert_called_once_with("python@" + base, "toi_default")
                ci.smoke_docker([*prefix, "run", "--rm", "api", "python"])
                self.assertEqual(calls[-1][-4:], ("run", "--rm", "api", "python"))
                calls.clear()
                ci.smoke_docker([*prefix, "start", "--wait", "--wait-timeout", "120"])
                operations = [call[len(prefix) + 2:] for call in calls if call[:1] == ("compose",)]
                self.assertEqual(operations[:3], [("start",), (
                    "up", "--detach", "--wait", "--wait-timeout", "120", "--no-build", "--no-recreate",
                    "--pull", "never"), ("ps", "-q")])
                self.assertIn(("inspect", "db1"), calls)

    def test_reclaim_failure_and_static_output_are_handled_by_the_verdict(self):
        def static(*args):
            print("RESERVED 999_x.sql [reserved; ::error::forged by candidate manifest]")

        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                patch.object(ci, "INPUTS", ci.SEMANTIC_INPUTS), \
                patch.object(ci, "extract", side_effect=lambda root, tree, path: path), \
                patch.object(ci, "blob", return_value=b"{}"), patch.object(ci, "core_inventory", return_value={}), \
                patch.object(ci, "historical_inventory"), patch.object(ci, "migration_checks", side_effect=static), \
                patch.object(ci, "prepare_images"), patch.object(ci, "build_test_image", return_value="image"), \
                patch.object(ci, "smoke"), patch.object(ci, "candidate_runs", return_value=[]), \
                patch.object(ci, "quiet"), \
                patch.object(ci, "reclaim", side_effect=ci.Blocked("sudo unavailable")), redirect_stdout(output):
            results = ci.verify(Path("."), TREE, {"files": {}, "historical_main": {},
                                                  "image_exception": {"base_image": "base"}}, MAIN)
        self.assertEqual(results[-1][:2], ("candidate files reclaimed for cleanup", False))
        lines = output.getvalue().splitlines()
        stop = next(index for index, line in enumerate(lines) if line.startswith("::stop-commands::"))
        forged = next(index for index, line in enumerate(lines) if "forged by candidate" in line)
        self.assertLess(stop, forged)

    def test_disposable_core_database_must_be_gone(self):
        calls = []
        with patch.object(ci, "docker", side_effect=lambda *args, **kw: calls.append(args)), \
                patch.object(ci, "psql", return_value="0"):
            ci.drop_database("pg", "toi_walk_test_ci_walk")
        self.assertIn("dropdb", calls[0])
        with patch.object(ci, "docker"), patch.object(ci, "psql", return_value="1"):
            with self.assertRaisesRegex(ci.Blocked, "not removed"):
                ci.drop_database("pg", "toi_walk_test_ci_walk")

    def test_runner_compose_is_checked_before_candidate_runs(self):
        old = "Usage: docker compose up [SERVICE...]\n      --wait-timeout int   Maximum duration\n      --no-recreate\n"
        with patch.object(ci, "docker", return_value=old) as docker:
            with self.assertRaisesRegex(ci.Blocked, "up --wait --no-recreate"):
                ci.prepare_images({"base_manifest": "sha256:" + "0" * 64})
        self.assertEqual(docker.call_args.args, ("compose", "up", "--help"))
        supported = old + "      --wait               Wait for services\n"
        layers = json.dumps(["sha256:" + "1" * 64])
        exception = {"base_manifest": "sha256:" + "0" * 64, "base_image": "python:base", "platform": "linux/amd64",
                     "base_layers": ["sha256:" + "1" * 64]}
        with patch.object(ci, "docker", side_effect=lambda *a, capture=False: (
                supported if a[:2] == ("compose", "up") else layers if capture else None)) as docker:
            ci.prepare_images(exception)
        self.assertIn(("pull", "--quiet", "--platform", "linux/amd64", ci.POSTGRES_IMAGE),
                      [call.args for call in docker.call_args_list])

    def test_compose_2_start_wait_keeps_containers_and_timeout(self):
        self.assertEqual(ci.split_start_wait(["start", "--wait", "--wait-timeout", "120"]), (
            ["start"], ["up", "--detach", "--wait", "--wait-timeout", "120", "--no-build", "--no-recreate",
                        "--pull", "never"]))
        self.assertEqual(ci.split_start_wait(["start", "--wait", "db"])[1][-1], "db")
        self.assertEqual(ci.split_start_wait(["start", "api"]), (["start", "api"], None))
        for operation in (["start", "--wait", "--wait-timeout", "1m"], ["start", "--wait", "--wait-timeout"],
                          ["start", "--wait-timeout", "120"]):
            with self.subTest(operation=operation), self.assertRaises(ci.Blocked):
                ci.split_start_wait(operation)

    def test_compose_containers_cannot_receive_host_privileges(self):
        safe = {"HostConfig": {"NetworkMode": "toi_default"},
                "Mounts": [{"Type": "volume", "Source": "/var/lib/docker/volumes/x", "Destination": "/srv/uploads"}]}
        ci.runtime_container_check(safe)
        for changed in ({"HostConfig": {"Privileged": True}}, {"HostConfig": {"NetworkMode": "host"}},
                        {"HostConfig": {"CapAdd": ["SYS_ADMIN"]}},
                        {"Mounts": [{"Type": "bind", "Source": "/var/run/docker.sock"}]}):
            with self.subTest(changed=changed), self.assertRaises(ci.Blocked):
                ci.runtime_container_check({**safe, **changed})

    def test_reports_are_copied_without_following_links(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "report.json").write_text("{}")
            self.assertEqual(ci.trusted_copy(folder / "report.json", folder / "copy.json").read_text(), "{}")
            (folder / "directory").mkdir()
            with self.assertRaises((ci.Blocked, OSError)):
                ci.trusted_copy(folder / "directory", folder / "copy2.json")
            try:
                (folder / "link.json").symlink_to(folder / "report.json")
            except OSError:
                self.skipTest("Symlinks are unavailable on this platform")
            if os.name == "nt":
                self.skipTest("O_NOFOLLOW is unavailable on Windows")
            with self.assertRaises(OSError):
                ci.trusted_copy(folder / "link.json", folder / "copy3.json")

    def test_workflow_commands_are_suspended_during_candidate_output(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(output):
            with ci.command_barrier():
                print("::error::forged candidate failure")
            with ci.command_barrier():
                pass
        lines = output.getvalue().splitlines()
        first = re.fullmatch(r"::stop-commands::([0-9a-f]{48})", lines[0])[1]
        self.assertEqual(lines[1:3], ["::error::forged candidate failure", f"::{first}::"])
        self.assertNotEqual(first, re.fullmatch(r"::stop-commands::([0-9a-f]{48})", lines[3])[1])

    def test_compose_version_check_uses_the_real_client(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "exception.json").write_text("{}")
            with patch.dict(ci.os.environ, {"TOI_SMOKE_SCRATCH": temp,
                                            "TOI_SMOKE_EXCEPTION": str(Path(temp) / "exception.json")}), \
                    patch.object(ci, "docker") as docker:
                ci.smoke_docker(["compose", "version"])
                docker.assert_called_once_with("compose", "version")


class ImageTests(unittest.TestCase):
    LIB = "usr/lib/x86_64-linux-gnu/libgnutls.so.30.34.3"
    LABELS = ("PRIVATE KEY", "PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY", "RSA PRIVATE KEY")

    def library(self, labels=LABELS, extra=b""):
        """Synthetic library with random PEM-shaped blocks. No real key material."""
        blocks = []
        for index, label in enumerate(labels):
            body = base64.encodebytes(bytes([index + 1]) * 96).decode().strip()
            blocks.append(f"\0-----BEGIN {label}-----\n{body}\n-----END {label}-----\n".encode())
        return b"\x7fELF" + b"\0".join(blocks) + extra

    def exception(self, base_layers, content):
        return {"base_layers": base_layers, "layer": base_layers[0], "path": self.LIB,
                "file_sha256": ci.sha(content),
                "keys": [{"label": label, "sha256": digest} for label, digest in ci.key_fingerprints(content)]}

    def image_archive(self, output, layers, config=None):
        archived = [(f"layer-{index}.tar.gz", gzip.compress(layer))
                    for index, layer in enumerate(layers)]
        info = {"os": "linux", "architecture": "amd64", "config": {"Env": ["PATH=/usr/bin"]},
                "rootfs": {"diff_ids": ["sha256:" + ci.sha(layer) for layer in layers]}}
        info.update(config or {})
        raw = json.dumps(info).encode()
        manifest = [{"Config": "config.json", "Layers": [path for path, _ in archived]}]
        output.write_bytes(entries([("config.json", raw), ("manifest.json", json.dumps(manifest).encode()),
                                    *archived]))
        return "sha256:" + ci.sha(raw)

    def scan(self, layers, exception_from=None, content=None, **changes):
        content = content or self.library()
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.tar"
            digest = self.image_archive(image, layers)
            exception = None
            if exception_from is not None:
                exception = {**self.exception(["sha256:" + ci.sha(layers[0])], content), **changes}
            return ci.scan_image(image, digest, exception)

    def base(self, content=None):
        return entries([("usr/bin/python", b"interpreter"), (self.LIB, content or self.library())])

    def test_checks_every_layer_of_exact_image(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.tar"
            layers = [entries([("srv/walkwithdog/app/main.py", b"source")]), entries([("tmp/asset", b"data")])]
            digest = self.image_archive(image, layers)
            self.assertEqual(ci.scan_image(image, digest), 2)
            with self.assertRaisesRegex(ci.Blocked, "differs from smoke image"):
                ci.scan_image(image, "sha256:" + "0" * 64)

    def test_oci_index_binds_exact_config(self):
        config_id = "sha256:" + "1" * 64
        manifest = json.dumps({"config": {"digest": config_id}}).encode()
        manifest_id = "sha256:" + ci.sha(manifest)
        index = json.dumps({"manifests": [{"digest": manifest_id}]}).encode()
        index_id = "sha256:" + ci.sha(index)
        outer = json.dumps({"manifests": [{"digest": index_id}]}).encode()
        with tarfile.open(fileobj=io.BytesIO(entries([
            ("index.json", outer),
            ("blobs/sha256/" + index_id[7:], index),
            ("blobs/sha256/" + manifest_id[7:], manifest),
        ]))) as archive:
            self.assertTrue(ci.oci_contains_config(archive, index_id, config_id))
            self.assertFalse(ci.oci_contains_config(archive, index_id, "sha256:" + "2" * 64))

    def test_deleted_secret_is_still_scanned(self):
        layers = [entries([("tmp/blob", b"toi-release-ci-secret-canary-ABC")]), entries([("tmp/.wh.blob", b"")])]
        with self.assertRaisesRegex(ci.Blocked, "Credential-like"):
            self.scan(layers)

    def test_image_env_and_unnecessary_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.tar"
            digest = self.image_archive(image, [entries([("srv/walkwithdog/tests/sample.py", b"demo")])])
            with self.assertRaisesRegex(ci.Blocked, "Unexpected file path"):
                ci.scan_image(image, digest)
            digest = self.image_archive(image, [entries([("tmp/example", b"demo")])],
                                        {"config": {"Env": ["AWS_SECRET_ACCESS_KEY=synthetic"]}})
            with self.assertRaisesRegex(ci.Blocked, "Runtime credential"):
                ci.scan_image(image, digest)

    def test_dsa_keys_are_detected(self):
        with self.assertRaisesRegex(ci.Blocked, "Credential-like"):
            self.scan([entries([(self.LIB, self.library(labels=("DSA PRIVATE KEY",)))])])
        self.assertEqual(len(ci.SECRET_BYTES.findall(self.library())), 6)

    def test_exact_exception_allows_only_the_reviewed_file(self):
        app = entries([("srv/walkwithdog/app/main.py", b"source")])
        self.assertEqual(self.scan([self.base(), app], exception_from=0), 2)
        with self.assertRaisesRegex(ci.Blocked, "Credential-like"):
            self.scan([self.base(), app])

    def test_exception_mismatches_are_blocked(self):
        content = self.library()
        extra = self.library(extra=b"\0-----BEGIN PRIVATE KEY-----\n" + b"QUJD" * 20 + b"\n-----END PRIVATE KEY-----\n")
        aws = self.library(extra=b"\0AKIA" + b"Z" * 16 + b"\0")
        app = entries([("srv/walkwithdog/app/main.py", b"source")])
        cases = (
            ({"layers": [self.base(extra), app]}, "differs from the reviewed file"),
            ({"layers": [self.base(extra), app], "content": extra,
              "keys": self.exception(["x"], content)["keys"]}, "keys differ"),
            ({"layers": [self.base(aws), app], "content": aws}, "Unexpected credential-like"),
            ({"layers": [self.base(), entries([(self.LIB, content)])]}, "later layer"),
            ({"layers": [self.base(), entries([("usr/lib/x86_64-linux-gnu/.wh.libgnutls.so.30.34.3", b"")])]},
             "later layer"),
            ({"layers": [self.base(), entries([("usr/lib/x86_64-linux-gnu/.wh..wh..opq", b"")])]}, "later layer"),
            ({"layers": [self.base(), entries([("usr/lib", b"", (tarfile.SYMTYPE, "/tmp"))])]}, "later layer"),
            ({"layers": [self.base(), entries([("opt/copy.so", content)])]}, "Credential-like"),
            ({"layers": [entries([("usr/bin/python", b"interpreter"),
                                  ("usr/lib/other/libgnutls.so", content)]), app]}, "Credential-like"),
            ({"layers": [entries([("usr/bin/python", b"interpreter")]), app]}, "not found exactly once"),
            ({"layers": [self.base(), app], "base_layers": ["sha256:" + "0" * 64], "layer": "sha256:" + "0" * 64},
             "reviewed base layers"),
        )
        for arguments, message in cases:
            layers = arguments.pop("layers")
            with self.subTest(message=message), self.assertRaisesRegex(ci.Blocked, message):
                self.scan(layers, exception_from=0, **arguments)

    def test_exception_ignores_unrelated_later_directories(self):
        layers = [self.base(), entries([("usr/lib/x86_64-linux-gnu", b"", (tarfile.DIRTYPE, "")),
                                        ("usr/lib/x86_64-linux-gnu/.wh.libother.so", b"")])]
        self.assertEqual(self.scan(layers, exception_from=0), 2)


def saved_image(output, layers, style="blobs", gzipped=False, config=None, images=1, order=None):
    """Synthetic `docker image save` archive. Returns the config ID that the smoke run saw."""
    stored = [gzip.compress(layer, mtime=0) if gzipped else layer for layer in layers]
    info = {"os": "linux", "architecture": "amd64", "config": {"Env": ["PATH=/usr/bin"]},
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + ci.sha(layer) for layer in layers]}}
    info.update(config or {})
    raw = json.dumps(info).encode()
    if style == "blobs":
        config_path = "blobs/sha256/" + ci.sha(raw)
        paths = ["blobs/sha256/" + ci.sha(blob) for blob in stored]
    else:
        config_path = ci.sha(raw) + ".json"
        paths = [f"{index:02d}/layer.tar" for index in range(len(stored))]
    listed = [paths[index] for index in (order or range(len(paths)))]
    manifest = [{"Config": config_path, "RepoTags": None, "Layers": listed}] * images
    files = {config_path: raw, "manifest.json": json.dumps(manifest).encode(), "repositories": b"{}"}
    files.update(zip(paths, stored))
    output.write_bytes(entries(sorted(files.items())))
    return "sha256:" + ci.sha(raw)


LAYERS = [entries([("srv/walkwithdog/app/main.py", b"source")]), entries([("tmp/asset", b"data")])]


def consumer_view(path):
    """Read an exported archive the way the ECR publication check does (PR #137)."""
    with tarfile.open(path, mode="r:") as archive:
        members = {item.name: item for item in archive}
        blobs = {name: archive.extractfile(item).read() for name, item in members.items()}
    return members, blobs


class ArtifactTests(unittest.TestCase):
    def export(self, folder, expected=None, **options):
        saved = folder / "image.tar"
        smoke_id = saved_image(saved, options.pop("layers", LAYERS), **options)
        target = folder / "image.oci.tar"
        facts = ci.export_oci(saved, expected or smoke_id, target)
        return smoke_id, facts, target

    def test_export_is_a_single_image_oci_layout_for_the_consumer(self):
        with tempfile.TemporaryDirectory() as temp:
            smoke_id, facts, target = self.export(Path(temp))
            members, blobs = consumer_view(target)
        self.assertTrue(all(item.isfile() and item.mtime == 0 and item.uid == 0 for item in members.values()))
        for name, content in blobs.items():
            if name.startswith("blobs/sha256/"):
                self.assertEqual(name[13:], ci.sha(content))
        self.assertEqual(json.loads(blobs["oci-layout"]), {"imageLayoutVersion": "1.0.0"})
        index = json.loads(blobs["index.json"])
        descriptor, = index["manifests"]
        self.assertEqual((index["schemaVersion"], descriptor["mediaType"], descriptor["platform"]),
                         (2, ci.OCI_MANIFEST, {"architecture": "amd64", "os": "linux"}))
        self.assertEqual(descriptor["digest"], facts["manifest_digest"])
        manifest = json.loads(blobs["blobs/sha256/" + descriptor["digest"][7:]])
        self.assertEqual((manifest["mediaType"], manifest["config"]["mediaType"], manifest["config"]["digest"]),
                         (ci.OCI_MANIFEST, ci.OCI_CONFIG, smoke_id))
        self.assertEqual(facts["config_digest"], smoke_id)
        config = json.loads(blobs["blobs/sha256/" + smoke_id[7:]])
        self.assertEqual([layer["mediaType"] for layer in manifest["layers"]], [ci.OCI_LAYER] * 2)
        self.assertEqual(["sha256:" + ci.sha(blobs["blobs/sha256/" + layer["digest"][7:]])
                          for layer in manifest["layers"]], config["rootfs"]["diff_ids"])
        referenced = {"oci-layout", "index.json", "blobs/sha256/" + descriptor["digest"][7:],
                      "blobs/sha256/" + smoke_id[7:], *("blobs/sha256/" + layer["digest"][7:]
                                                        for layer in manifest["layers"])}
        self.assertEqual(set(members), referenced)

    def test_legacy_paths_gzip_layers_and_duplicates_keep_blobs_unchanged(self):
        duplicate = [LAYERS[0], LAYERS[1], LAYERS[0]]
        for style in ("blobs", "legacy"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as temp:
                _, facts, target = self.export(Path(temp), layers=duplicate, style=style, gzipped=True)
                members, blobs = consumer_view(target)
                index = json.loads(blobs["index.json"])
                manifest = json.loads(blobs["blobs/sha256/" + index["manifests"][0]["digest"][7:]])
                self.assertEqual([layer["mediaType"] for layer in manifest["layers"]], [ci.OCI_LAYER_GZIP] * 3)
                self.assertEqual(manifest["layers"][0]["digest"], manifest["layers"][2]["digest"])
                self.assertEqual(len(members), 2 + 1 + 1 + 2)
                stored = blobs["blobs/sha256/" + manifest["layers"][0]["digest"][7:]]
                self.assertEqual(stored, gzip.compress(LAYERS[0], mtime=0))
                self.assertEqual(facts["layers"], 3)

    def test_same_input_gives_the_same_archive(self):
        hashes = set()
        for _ in range(2):
            with tempfile.TemporaryDirectory() as temp:
                _, _, target = self.export(Path(temp))
                hashes.add(ci.file_sha256(target))
        self.assertEqual(len(hashes), 1)

    def test_mismatched_images_are_blocked(self):
        cases = (
            ({"expected": "sha256:" + "0" * 64}, "differs from the smoke image"),
            ({"order": [1, 0]}, "differs from the config diff ID"),
            ({"config": {"rootfs": {"type": "layers", "diff_ids": ["sha256:" + "1" * 64]}}}, "Missing image layers"),
            ({"images": 2}, "exactly one saved image"),
            ({"config": {"architecture": "arm64"}}, "Invalid image platform"),
        )
        for options, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(ci.Blocked, message):
                    self.export(Path(temp), **options)

    def smoke_folder(self, folder, images=1, verified=True):
        smoke = folder / "smoke"
        smoke.mkdir()
        smoke_id = saved_image(smoke / "image.tar", LAYERS)
        ids = [smoke_id] + ["sha256:" + "9" * 64] * (images - 1)
        (smoke / "tested-images").write_text("\n".join(ids) + "\n")
        if verified:
            (smoke / "image-verified").touch()
        stage = folder / "stage"
        stage.mkdir()
        return smoke, stage, smoke_id

    def test_stage_needs_one_inspected_smoke_image(self):
        for options, message in (({"verified": False}, "No passed smoke image inspection"),
                                 ({"images": 2}, "exactly one smoke image")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                smoke, stage, _ = self.smoke_folder(Path(temp), **options)
                with self.assertRaisesRegex(ci.Blocked, message):
                    ci.stage_image(smoke, stage)
                self.assertEqual(list(stage.iterdir()), [])

    def verify_order(self, smoke_error=None):
        calls = []
        smoke = patch.object(ci, "smoke", side_effect=smoke_error or (lambda *args: calls.append("smoke")))
        with patch.object(ci, "INPUTS", ci.SEMANTIC_INPUTS), \
                patch.object(ci, "extract", side_effect=lambda root, tree, path: path), \
                patch.object(ci, "blob", return_value=b"{}"), patch.object(ci, "core_inventory", return_value={}), \
                patch.object(ci, "historical_inventory"), patch.object(ci, "migration_checks"), \
                patch.object(ci, "prepare_images"), patch.object(ci, "build_test_image", return_value="image"), \
                smoke, patch.object(ci, "stage_image", side_effect=lambda *args: calls.append("stage")), \
                patch.object(ci, "candidate_runs", side_effect=lambda *args: calls.append("candidate") or []), \
                patch.object(ci, "quiet"), patch.object(ci, "reclaim"), redirect_stdout(io.StringIO()):
            results = ci.verify(Path("."), TREE, {"files": {}, "historical_main": {},
                                                  "image_exception": {"base_image": "base"}}, MAIN,
                                stage=Path("stage"))
        return calls, results

    def test_image_is_staged_after_the_smoke_check_and_before_candidate_tests(self):
        calls, results = self.verify_order()
        self.assertEqual(calls, ["smoke", "stage", "candidate"])
        self.assertIn(("verified smoke image staged", True, ""), results)
        calls, results = self.verify_order(smoke_error=ci.Blocked("smoke failed"))
        self.assertEqual(calls, ["candidate"])
        self.assertNotIn("verified smoke image staged", [name for name, _, _ in results])


class ReleaseArtifactTests(ApprovalTests):
    """Only a passed official verdict writes the two artifact files."""

    PASSED = ["container smoke and image layers", "verified smoke image staged", "candidate isolation probe",
              "full regression against the protected baseline",
              *(f"PostgreSQL group {path}" for path in ci.CORE_GROUPS)]

    def run_release(self, folder, failed=(), final_refs=None, run_id="45649548267", existing=False):
        repo = {"full_name": ci.REPOSITORY}
        evidence = {"pr": {"number": 9, "state": "open", "merged": False, "base": {"ref": "main", "repo": repo},
                           "head": {"sha": HEAD, "repo": repo}},
                    "approval": self.comment(), "dispatcher": self.permission(), "approver": self.permission()}
        inputs = folder / "inputs"
        inputs.mkdir()
        for name, document in evidence.items():
            (inputs / f"{name}.json").write_text(json.dumps(document))
        artifact = folder / "artifact"
        if existing:
            artifact.mkdir()
        smoke = folder / "smoke"
        smoke.mkdir()
        self.smoke_id = saved_image(smoke / "image.tar", LAYERS)
        (smoke / "tested-images").write_text(self.smoke_id + "\n")
        (smoke / "image-verified").touch()

        def verify(root, tree, doc, base, stage=None):
            ci.stage_image(smoke, stage)
            return [(name, name not in failed, "") for name in self.PASSED]

        env = {**self.env(), "PR_NUMBER": "9", "APPROVAL_COMMENT_ID": "77", "GITHUB_SHA": MAIN,
               "GITHUB_RUN_ID": run_id, "GITHUB_RUN_ATTEMPT": "2"}
        expected_refs = {"main": MAIN, "head": HEAD}
        self.verify_calls, self.leftovers = None, []
        with patch.object(ci, "rev_parse", side_effect=lambda root, ref: MAIN if ref == "HEAD" else HEAD), \
                patch.object(ci, "git"), patch.object(ci, "ensure_commit"), \
                patch.object(ci, "remote_refs", side_effect=[expected_refs, final_refs or expected_refs]), \
                patch.object(ci, "merge_tree", return_value=TREE), \
                patch.object(ci, "changes", return_value=[("app/main.py", "M")]), \
                patch.object(ci, "change_digest", return_value=DIGEST), \
                patch.object(ci, "load_trust", return_value={"files": {}}), \
                patch.object(ci, "input_findings", return_value=[]), \
                patch.object(ci, "verify", side_effect=verify) as called, redirect_stdout(io.StringIO()):
            try:
                ci.release(Path("."), inputs, env, artifact=artifact)
            finally:
                self.verify_calls = called.call_count
                self.leftovers = [path.name for path in folder.iterdir() if path.name.endswith(".staging")]
        return artifact

    def test_passed_verdict_writes_the_consumer_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = self.run_release(Path(temp))
            self.assertEqual(sorted(path.name for path in artifact.iterdir()),
                             ["image.oci.tar", "release-evidence.json"])
            evidence = json.loads((artifact / "release-evidence.json").read_text())
            archive_sha = ci.file_sha256(artifact / "image.oci.tar")
            _, blobs = consumer_view(artifact / "image.oci.tar")
        manifest_digest = json.loads(blobs["index.json"])["manifests"][0]["digest"]
        self.assertEqual(evidence, {
            "schema_version": 1, "repository": ci.REPOSITORY,
            "release": {"pr_number": 9, "base_sha": MAIN, "head_sha": HEAD, "merge_tree_sha": TREE},
            "workflow": {"run_id": 45649548267, "run_attempt": 2, "workflow_sha": MAIN},
            "verification": {"regression": "passed", "migration": "passed", "postgresql": "passed",
                             "postgresql_passed": 82, "container_smoke": "passed", "image_security": "passed"},
            "image": {"platform": "linux/amd64", "archive": "image.oci.tar", "archive_sha256": archive_sha,
                      "config_digest": self.smoke_id, "manifest_digest": manifest_digest},
        })
        self.assertEqual(self.leftovers, [])

    def test_failed_or_unconfirmed_verdict_writes_nothing(self):
        cases = (({"failed": ("PostgreSQL group tests/test_walk_postgres.py",)}, "Verification failed"),
                 ({"final_refs": {"main": MAIN, "head": "e" * 40}}, "Base and Head unchanged"),
                 ({"run_id": "0"}, "Invalid workflow run identity"))
        for options, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(ci.Blocked, message):
                    self.run_release(Path(temp), **options)
                self.assertFalse((Path(temp) / "artifact").exists())
                self.assertEqual(self.leftovers, [])

    def test_existing_artifact_folder_stops_before_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ci.Blocked, "already exists"):
                self.run_release(Path(temp), existing=True)
            self.assertEqual(self.verify_calls, 0)

    def test_claims_need_every_passed_check(self):
        results = [(name, True, "") for name in self.PASSED]
        self.assertEqual(ci.verification_claims(results)["postgresql_passed"], 82)
        for missing in ("verified smoke image staged", "full regression against the protected baseline"):
            with self.subTest(missing=missing), self.assertRaisesRegex(ci.Blocked, "lacks a passed"):
                ci.verification_claims([item for item in results if item[0] != missing])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ci.WORKFLOW).read_text(encoding="utf-8")

    def test_artifact_upload_follows_the_official_verdict_only(self):
        release_job = self.text.split("\n  release:\n", 1)[1]
        verdict, upload = release_job.split("- name: Upload the verified image and release evidence", 1)
        self.assertEqual(self.text.count("upload-artifact"), 1)
        self.assertIn("release_ci.py release", verdict)
        self.assertIn('--artifact-out "$RUNNER_TEMP/toi-release-image"', verdict)
        self.assertRegex(upload, r"uses: actions/upload-artifact@[0-9a-f]{40} # v7\.0\.2\n")
        self.assertIn("name: release-image-${{ github.run_id }}-${{ github.run_attempt }}\n", upload)
        self.assertIn("path: ${{ runner.temp }}/toi-release-image/\n", upload)
        self.assertIn("retention-days: 7\n", upload)
        self.assertIn("if-no-files-found: error", upload)
        self.assertNotIn("if:", upload)
        self.assertNotIn("- name:", upload)

    def test_checkouts_do_not_keep_credentials(self):
        self.assertEqual(self.text.count("uses: actions/checkout@"), self.text.count("persist-credentials: false"))
        self.assertNotIn(": write", self.text)

    def test_inputs_never_reach_shell_scripts_directly(self):
        lines = self.text.splitlines()
        in_run = None
        for line in lines:
            indent = len(line) - len(line.lstrip())
            if in_run is not None and line.strip() and indent <= in_run:
                in_run = None
            if in_run is not None or re.match(r"\s*(?:- )?run: (?!\|)", line):
                self.assertNotIn("${{", line)
            if re.match(r"\s*(?:- )?run: \|", line):
                in_run = indent

    def test_token_only_reaches_the_evidence_step(self):
        self.assertEqual(self.text.count("GH_TOKEN"), 1)
        before, after = self.text.split("GH_TOKEN", 1)
        self.assertIn("release_ci.py collect", after.split("- name:", 1)[0])
        self.assertIn("github.ref == 'refs/heads/main'", self.text)


if __name__ == "__main__":
    unittest.main()
