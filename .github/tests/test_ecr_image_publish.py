"""Synthetic OCI and isolated transport tests; no AWS or live GitHub access."""
import copy
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[2] / "scripts" / "ecr_image_publish.py"
SPEC = importlib.util.spec_from_file_location("ecr_image_publish", MODULE)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def make_tar(items):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, data in items:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return out.getvalue()


def fixture(architecture="amd64", wrong_diff=False, multi=False, gzip_layer=True,
            extra=None, tamper_blob=False):
    blobs = {}

    def add(data, media_type):
        d = digest(data)
        blobs["blobs/sha256/" + d[7:]] = data
        return {"digest": d, "mediaType": media_type, "size": len(data)}

    raw_layer = make_tar([("hello.txt", b"synthetic fixture")])
    layer = add(gzip.compress(raw_layer, mtime=0) if gzip_layer else raw_layer,
                ci.GZIP if gzip_layer else ci.LAYER)
    config = add(encode({"os": "linux", "architecture": architecture,
                         "rootfs": {"type": "layers", "diff_ids": [digest(b"wrong") if wrong_diff else digest(raw_layer)]}}), ci.CONFIG)
    manifest = add(encode({"schemaVersion": 2, "mediaType": ci.MANIFEST,
                           "config": config, "layers": [layer]}), ci.MANIFEST)
    items = [("oci-layout", encode({"imageLayoutVersion": "1.0.0"})),
             ("index.json", encode({"schemaVersion": 2, "manifests": [manifest] * (2 if multi else 1)}))]
    if tamper_blob:
        name = "blobs/sha256/" + layer["digest"][7:]
        raw = blobs[name]
        blobs[name] = bytes([raw[0] ^ 1]) + raw[1:]
    items.extend(blobs.items())
    if extra:
        items.append(extra)
    archive = make_tar(items)
    evidence = {"schema_version": 1, "repository": ci.REPOSITORY,
                "release": {"pr_number": 1, "base_sha": "a" * 40, "head_sha": "b" * 40,
                            "merge_tree_sha": "c" * 40},
                "workflow": {"run_id": 123, "run_attempt": 2, "workflow_sha": "a" * 40},
                "verification": {**dict.fromkeys(ci.CHECKS, "passed"), "postgresql_passed": 82},
                "image": {"platform": ci.PLATFORM, "archive": "image.oci.tar",
                          "archive_sha256": digest(archive)[7:], "config_digest": config["digest"],
                          "manifest_digest": manifest["digest"]}}
    return archive, evidence


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "image.oci.tar"

    def verify(self, archive, evidence):
        self.path.write_bytes(archive)
        return ci.verify_archive(self.path, evidence)

    def test_valid_archive_never_authorizes_publication(self):
        for gzip_layer in (True, False):
            with self.subTest(gzip=gzip_layer):
                archive, evidence = fixture(gzip_layer=gzip_layer)
                result = self.verify(archive, evidence)
                self.assertTrue(result["integrity_verified"])
                self.assertFalse(result["publication_authorized"])
                self.assertEqual(result["layer_count"], 1)

    def test_archive_hash_mismatch(self):
        archive, evidence = fixture()
        with self.assertRaisesRegex(ci.Blocked, "Archive SHA-256"):
            self.verify(archive + b"changed", evidence)

    def test_manifest_digest_mismatch(self):
        archive, evidence = fixture()
        evidence["image"]["manifest_digest"] = digest(b"wrong")
        with self.assertRaisesRegex(ci.Blocked, "Manifest digest"):
            self.verify(archive, evidence)

    def test_config_digest_mismatch(self):
        archive, evidence = fixture()
        evidence["image"]["config_digest"] = digest(b"wrong")
        with self.assertRaisesRegex(ci.Blocked, "Config digest"):
            self.verify(archive, evidence)

    def test_wrong_platform(self):
        with self.assertRaisesRegex(ci.Blocked, "config platform"):
            self.verify(*fixture(architecture="arm64"))

    def test_layer_diff_mismatch(self):
        with self.assertRaisesRegex(ci.Blocked, "DiffID mismatch"):
            self.verify(*fixture(wrong_diff=True))

    def test_layer_blob_tampering(self):
        with self.assertRaisesRegex(ci.Blocked, "blob digest"):
            self.verify(*fixture(tamper_blob=True))

    def test_multi_manifest(self):
        with self.assertRaisesRegex(ci.Blocked, "exactly one"):
            self.verify(*fixture(multi=True))

    def test_extra_and_unsafe_paths(self):
        for name in ("../escape", "/absolute", "unknown.txt", "blobs/sha256/" + "f" * 64):
            with self.subTest(name=name), self.assertRaises(ci.Blocked):
                self.verify(*fixture(extra=(name, b"extra")))

    def test_duplicate_archive_member(self):
        with self.assertRaisesRegex(ci.Blocked, "Duplicate archive member"):
            self.verify(*fixture(extra=("./index.json", b"{}")))

    def test_missing_or_failed_checks(self):
        archive, evidence = fixture()
        for key in ci.CHECKS:
            for value in (None, "failed", True):
                bad = copy.deepcopy(evidence)
                bad["verification"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ci.Blocked):
                    self.verify(archive, bad)

    def test_postgres_count_and_boolean(self):
        archive, evidence = fixture()
        for count in (81, 83, "82", True):
            evidence["verification"]["postgresql_passed"] = count
            with self.subTest(count=count), self.assertRaises(ci.Blocked):
                self.verify(archive, evidence)

    def test_short_sha_and_wrong_repository(self):
        archive, evidence = fixture()
        for group, key, value in (("release", "head_sha", "a123456"),
                                  ("workflow", "run_id", True),
                                  ("workflow", "run_attempt", 0)):
            bad = copy.deepcopy(evidence)
            bad[group][key] = value
            with self.subTest(key=key), self.assertRaises(ci.Blocked):
                self.verify(archive, bad)
        evidence["repository"] = "another/repo"
        with self.assertRaises(ci.Blocked):
            self.verify(archive, evidence)

    def test_duplicate_json_key(self):
        with self.assertRaisesRegex(ci.Blocked, "Duplicate JSON key"):
            ci.parse_json(b'{"schema_version":1,"schema_version":2}')

    def test_artifact_name_attempt_specific(self):
        self.assertEqual(ci.artifact_name(123, 2), "release-image-123-2")
        self.assertNotEqual(ci.artifact_name(123, 1), ci.artifact_name(123, 2))
        for args in ((True, 1), (123, 0), (123, "2")):
            with self.assertRaises(ci.Blocked):
                ci.artifact_name(*args)

    def test_invalid_tar(self):
        _, evidence = fixture()
        raw = b"invalid tar"
        evidence["image"]["archive_sha256"] = digest(raw)[7:]
        with self.assertRaises(ci.Blocked):
            self.verify(raw, evidence)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        _, self.evidence = fixture()
        repo = {"id": ci.REPOSITORY_ID, "full_name": ci.REPOSITORY}
        self.now = datetime(2026, 11, 25, 13, tzinfo=timezone.utc)
        self.source = "d" * 40
        self.run = {"id": 123, "run_attempt": 2, "event": "workflow_dispatch",
                    "head_branch": "main", "path": ci.RELEASE_WORKFLOW,
                    "status": "completed", "conclusion": "success", "repository": repo,
                    "head_repository": repo, "head_sha": "a" * 40,
                    "run_started_at": "2026-11-25T10:00:00Z", "updated_at": "2026-11-25T11:00:00Z"}
        self.artifact = {"id": 55, "name": ci.artifact_name(123, 2), "size_in_bytes": 100,
                         "digest": digest(b"zip"), "expired": False,
                         "created_at": "2026-11-25T10:30:00Z", "expires_at": "2026-12-02T10:30:00Z",
                         "workflow_run": {"id": 123, "repository_id": ci.REPOSITORY_ID,
                                          "head_repository_id": ci.REPOSITORY_ID,
                                          "head_branch": "main", "head_sha": "a" * 40}}
        self.pr = {"number": 1, "merged": True, "merge_commit_sha": self.source,
                   "merged_at": "2026-11-25T12:00:00Z", "base": {"ref": "main", "repo": repo},
                   "head": {"sha": "b" * 40, "repo": repo}}
        self.commit = {"sha": self.source, "commit": {"tree": {"sha": "c" * 40}},
                       "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}]}

    def verify(self, **changes):
        args = dict(run=self.run, artifacts=[self.artifact], pr=self.pr, commit=self.commit,
                    source_sha=self.source, trusted_workflow_shas={"a" * 40}, now=self.now)
        args.update(changes)
        return ci.verify_github_metadata(self.evidence, **args)

    def test_good_metadata_not_publication_authorization(self):
        result = self.verify()
        self.assertTrue(result["metadata_verified"])
        self.assertFalse(result["publication_authorized"])
        self.assertEqual(result["artifact_id"], 55)

    def test_untrusted_workflow(self):
        with self.assertRaisesRegex(ci.Blocked, "Untrusted"):
            self.verify(trusted_workflow_shas=set())

    def test_wrong_run_or_attempt(self):
        for changes in ({"id": 124}, {"run_attempt": 3}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(run={**self.run, **changes})

    def test_wrong_workflow_or_event_or_branch(self):
        for changes in ({"path": "fake.yml"}, {"event": "pull_request"}, {"head_branch": "dev"}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(run={**self.run, **changes})

    def test_failed_or_incomplete_run(self):
        for changes in ({"conclusion": "failure"}, {"status": "in_progress"}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(run={**self.run, **changes})

    def test_wrong_repository_or_sha(self):
        for changes in ({"repository": {"id": 1, "full_name": ci.REPOSITORY}},
                        {"head_repository": None}, {"head_sha": "e" * 40}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(run={**self.run, **changes})

    def test_missing_duplicate_or_previous_attempt_artifact(self):
        for artifacts in ([], [self.artifact, self.artifact], [{**self.artifact, "name": ci.artifact_name(123, 1)}]):
            with self.subTest(artifacts=artifacts), self.assertRaises(ci.Blocked):
                self.verify(artifacts=artifacts)

    def test_expired_or_previous_run_time_artifact(self):
        for changes in ({"expired": True}, {"expires_at": "2026-11-25T12:30:00Z"},
                        {"created_at": "2026-11-25T09:59:00Z"}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(artifacts=[{**self.artifact, **changes}])

    def test_artifact_origin_or_digest_mismatch(self):
        for changes in ({"workflow_run": {**self.artifact["workflow_run"], "id": 999}},
                        {"digest": None}, {"workflow_run": {**self.artifact["workflow_run"], "head_repository_id": 1}}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(artifacts=[{**self.artifact, **changes}])

    def test_wrong_or_early_pr_merge(self):
        for changes in ({"merged": False}, {"merge_commit_sha": "e" * 40},
                        {"merged_at": "2026-11-25T10:45:00Z"}, {"number": 2}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(pr={**self.pr, **changes})

    def test_wrong_head_or_base_branch(self):
        for changes in ({"head": {**self.pr["head"], "sha": "e" * 40}},
                        {"base": {**self.pr["base"], "ref": "dev"}}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(pr={**self.pr, **changes})

    def test_wrong_tree_or_parents(self):
        for changes in ({"commit": {"tree": {"sha": "e" * 40}}},
                        {"parents": [{"sha": "a" * 40}]}, {"parents": list(reversed(self.commit["parents"]))}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.verify(commit={**self.commit, **changes})

    def test_artifact_zip_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "artifact.zip"
            path.write_bytes(b"zip")
            ci.verify_artifact_zip_hash(path, self.artifact["digest"])
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ci.Blocked, "ZIP digest mismatch"):
                ci.verify_artifact_zip_hash(path, self.artifact["digest"])


class ReleaseContractTests(unittest.TestCase):
    def test_change_digest_matches_release_ci_name_status_encoding(self):
        from unittest.mock import patch
        raw = b"M\x00z.txt\x00A\x00a.txt\x00"
        expected = hashlib.sha256(
            json.dumps([["a.txt", "A"], ["z.txt", "M"]], separators=(",", ":"),
                       ensure_ascii=False).encode()).hexdigest()
        with patch.object(ci.subprocess, "check_output", return_value=raw) as command:
            self.assertEqual(ci.release_change_digest("a" * 40, "b" * 40), expected)
        self.assertEqual(command.call_args.args[0][-5:],
                         ["--name-status", "-z", "a" * 40, "b" * 40, "--"])

    def test_malformed_change_list_is_blocked(self):
        from unittest.mock import patch
        with patch.object(ci.subprocess, "check_output", return_value=b"M\x00missing-terminator"):
            with self.assertRaisesRegex(ci.Blocked, "change list"):
                ci.release_change_digest("a" * 40, "b" * 40)


class GitHubJobLogDownloadTests(unittest.TestCase):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b"trusted job log"

    class Opener:
        def __init__(self, location):
            from email.message import Message
            self.location = location
            self.api_request = None
            self.signed_request = None
            self.headers = Message()
            self.headers["Location"] = location

        def open(self, request, timeout):
            from urllib.error import HTTPError
            if request.full_url.startswith("https://api.github.com/"):
                self.api_request = request
                raise HTTPError(request.full_url, 302, "Redirect", self.headers, None)
            self.signed_request = request
            return GitHubJobLogDownloadTests.Response()

    def test_signed_log_download_does_not_forward_github_token(self):
        from unittest.mock import patch
        location = "https://productionresultssa31.blob.core.windows.net/log?sig=private"
        opener = self.Opener(location)
        with patch.object(ci.urllib.request, "build_opener", return_value=opener):
            api = ci.GitHubAPI("test-token")
            api._opener = opener
            self.assertEqual(api.get_job_log(90), b"trusted job log")
        self.assertEqual(opener.api_request.get_header("Authorization"), "Bearer test-token")
        self.assertIsNone(opener.signed_request.get_header("Authorization"))

    def test_job_log_redirect_rejects_untrusted_host(self):
        from unittest.mock import patch
        opener = self.Opener("https://attacker.example/log")
        with patch.object(ci.urllib.request, "build_opener", return_value=opener):
            api = ci.GitHubAPI("test-token")
            api._opener = opener
            with self.assertRaisesRegex(ci.Blocked, "Invalid signed"):
                api.get_job_log(90)
        self.assertIsNone(opener.signed_request)


class GitHubIntegrationTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        import zipfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "artifact.zip"
        raw, self.evidence = fixture()
        with zipfile.ZipFile(self.path, "w") as bundle:
            bundle.writestr("image.oci.tar", raw)
            bundle.writestr("release-evidence.json", encode(self.evidence))
        self.fixture = MetadataTests()
        self.fixture.setUp()
        f = self.fixture
        f.artifact["digest"] = digest(self.path.read_bytes())
        f.pr["user"] = {"id": 9}
        f.run["actor"] = {"id": 11, "login": "release-dispatcher"}
        self.change_digest = "f" * 64
        self.verifier_hashes = {path: "e" * 64 for path in ci.RELEASE_VERIFIER_FILES}
        approval = {"pr": 1, "base": "a" * 40, "head": "b" * 40, "merge_tree": "c" * 40,
                    "changes": self.change_digest,
                    "review": {"head": "b" * 40,
                               "record": f"https://github.com/{ci.REPOSITORY}/pull/1#pullrequestreview-1",
                               "blocking": 0}}
        self.comment = {"id": 77, "issue_url": f"https://api.github.com/repos/{ci.REPOSITORY}/issues/1",
                        "user": {"type": "User", "login": "release-admin"},
                        "created_at": "2026-11-25T09:30:00Z", "updated_at": "2026-11-25T09:30:00Z",
                        "body": "Approval\n```toi-release-approval\n" + json.dumps(approval) + "\n```"}
        self.review = {"id": 1, "user": {"id": 10}, "state": "APPROVED", "commit_id": "b" * 40,
                       "submitted_at": "2026-11-25T09:00:00Z", "author_association": "MEMBER",
                       "html_url": f"https://github.com/{ci.REPOSITORY}/pull/1#pullrequestreview-1"}
        self.records = {"/actions/runs/123": f.run, "/actions/runs/123/attempts/2": copy.deepcopy(f.run),
                        "/actions/runs/123/artifacts?per_page=100&page=1": {"total_count": 1, "artifacts": [f.artifact]},
                        "/actions/runs/123/attempts/2/jobs?per_page=100&page=1": {
                            "total_count": 1, "jobs": [{"id": 90, "name": "Official release verification",
                                                        "status": "completed", "conclusion": "success",
                                                        "run_id": 123, "run_attempt": 2,
                                                        "head_sha": f.run["head_sha"]}]},
                        "/issues/comments/77": self.comment,
                        "/pulls/1": f.pr, "/commits/" + f.source: f.commit,
                        "/branches/main": {"name": "main", "protected": True, "commit": {"sha": f.source}},
                        "/pulls/1/reviews?per_page=100&page=1": [self.review]}
        self.api = Mock()
        self.api.get.side_effect = lambda endpoint: self.records[endpoint]
        self.api.get_job_log.return_value = (
            b"## Release CI official verification\n"
            b"| Item | Value |\n| --- | --- |\n"
            b"| approval comment | `77` |\n"
            b"| approver | `release-admin` |\n")

    def verify(self, **changes):
        from unittest.mock import patch
        args = dict(run_id=123, attempt=2, pr_number=1, source_sha=self.fixture.source,
                    trusted_workflow_shas={"a" * 40}, now=self.fixture.now)
        args.update(changes)
        with patch.object(ci, "release_change_digest", return_value=self.change_digest), \
                patch.object(ci, "verifier_file_hashes", return_value=self.verifier_hashes):
            return ci.verify_github_zip(self.api, self.path, **args)

    def test_good_zip_authorizes_only_after_release_approval_and_integrity_checks(self):
        result = self.verify()
        self.assertTrue(result["metadata_verified"])
        self.assertTrue(result["integrity_verified"])
        self.assertEqual(result["independent_review_ids_observed"], [1])
        self.assertTrue(result["approval_contract_verified"])
        self.assertTrue(result["publication_authorized"])
        context = result["release_context"]
        self.assertEqual(context["release_workflow"]["official_job_id"], 90)
        self.assertEqual(context["approval"]["comment_id"], 77)
        self.assertEqual(context["release"]["change_digest"], self.change_digest)
        self.assertEqual(set(context["verifier"]["files"]), set(ci.RELEASE_VERIFIER_FILES))

    def test_release_approval_requires_unique_unchanged_admin_record(self):
        original = copy.deepcopy(self.comment)
        for update in (
            {"updated_at": "2026-11-25T09:31:00Z"},
            {"body": original["body"].replace(self.change_digest, "0" * 64)},
            {"body": original["body"].replace("pullrequestreview-1", "pullrequestreview-2")},
            {"user": {"type": "Bot", "login": "release-admin"}},
            {"id": 78},
        ):
            self.records["/issues/comments/77"] = {**original, **update}
            with self.subTest(update=update), self.assertRaises(ci.Blocked):
                self.verify()
        self.records["/issues/comments/77"] = original
        log = self.api.get_job_log.return_value
        self.api.get_job_log.return_value = log.replace(b"release-admin", b"other-admin")
        with self.assertRaisesRegex(ci.Blocked, "differs from the successful Release CI run"):
            self.verify()
        self.api.get_job_log.return_value = log

    def test_verified_bundle_contains_only_verified_release_files(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "verified"
            result = self.verify(output_directory=output)
            self.assertTrue(result["publication_authorized"])
            self.assertEqual({path.name for path in output.iterdir()},
                             {"image.oci.tar", "release-evidence.json", ci.VERIFIED_CONTEXT_FILE})
            context = json.loads((output / ci.VERIFIED_CONTEXT_FILE).read_text())
            self.assertTrue(context["publication_authorized"])
            self.assertEqual(digest((output / "image.oci.tar").read_bytes())[7:],
                             context["image"]["archive_sha256"])

    def test_old_or_inconsistent_attempt(self):
        with self.assertRaisesRegex(ci.Blocked, "latest attempt"):
            self.verify(attempt=1)
        self.records["/actions/runs/123/attempts/2"]["head_sha"] = "e" * 40
        with self.assertRaisesRegex(ci.Blocked, "attempt changed"):
            self.verify()

    def test_unprotected_or_advanced_main(self):
        branch = self.records["/branches/main"]
        branch["protected"] = False
        with self.assertRaisesRegex(ci.Blocked, "protected main"):
            self.verify()
        branch["protected"] = True
        branch["commit"]["sha"] = "e" * 40
        with self.assertRaisesRegex(ci.Blocked, "protected main"):
            self.verify()

    def test_publish_recheck_rejects_advanced_main_or_release_rerun(self):
        context = self.verify()["release_context"]
        ci.verify_context_current(self.api, context)
        branch = self.records["/branches/main"]
        branch["commit"]["sha"] = "e" * 40
        with self.assertRaisesRegex(ci.Blocked, "protected main"):
            ci.verify_context_current(self.api, context)
        branch["commit"]["sha"] = self.fixture.source
        latest = self.records["/actions/runs/123"]
        latest["run_attempt"] = 3
        with self.assertRaisesRegex(ci.Blocked, "latest successful attempt"):
            ci.verify_context_current(self.api, context)

    def test_publish_recheck_rejects_replaced_approval(self):
        context = self.verify()["release_context"]
        self.records["/issues/comments/77"]["body"] = "Reposted comment\n" + self.records["/issues/comments/77"]["body"]
        with self.assertRaisesRegex(ci.Blocked, "approval changed"):
            ci.verify_context_current(self.api, context)

    def test_zip_tampered_before_parse(self):
        self.path.write_bytes(b"altered")
        with self.assertRaisesRegex(ci.Blocked, "ZIP digest mismatch"):
            self.verify()

    def test_extra_zip_member_even_with_matching_digest(self):
        import zipfile
        with zipfile.ZipFile(self.path, "a") as bundle:
            bundle.writestr("../unexpected", b"no")
        self.fixture.artifact["digest"] = digest(self.path.read_bytes())
        with self.assertRaisesRegex(ci.Blocked, "ZIP members"):
            self.verify()

    def test_self_approval_stale_head_late_and_outsider(self):
        original = self.review.copy()
        for changes in ({"user": {"id": 9}}, {"commit_id": "e" * 40},
                        {"submitted_at": "2026-11-25T10:30:00Z"}, {"author_association": "NONE"}):
            self.review.clear()
            self.review.update(original, **changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ci.Blocked, "independent"):
                self.verify()

    def test_dismissed_or_changes_requested_review(self):
        for state in ("DISMISSED", "CHANGES_REQUESTED"):
            self.records["/pulls/1/reviews?per_page=100&page=1"] = [self.review, {**self.review, "id": 2,
                "state": state, "submitted_at": "2026-11-25T09:30:00Z"}]
            with self.subTest(state=state), self.assertRaisesRegex(ci.Blocked, "independent"):
                self.verify()

    def test_pagination_and_duplicate_ids(self):
        from unittest.mock import Mock
        api = Mock()
        page = [{"id": i} for i in range(1, 101)]
        api.get.side_effect = [{"total_count": 101, "artifacts": page},
                               {"total_count": 101, "artifacts": [{"id": 101}]}]
        self.assertEqual(len(ci.api_pages(api, "/artifacts", key="artifacts")), 101)
        api.get.side_effect = [page, [{"id": 100}]]
        with self.assertRaisesRegex(ci.Blocked, "Duplicate"):
            ci.api_pages(api, "/reviews")

    def test_incomplete_inventory(self):
        from unittest.mock import Mock
        api = Mock()
        api.get.return_value = {"total_count": 2, "artifacts": [{"id": 1}]}
        with self.assertRaisesRegex(ci.Blocked, "Incomplete"):
            ci.api_pages(api, "/artifacts", key="artifacts")

    def test_read_only_api_request(self):
        from unittest.mock import Mock
        api = ci.GitHubAPI("synthetic-token")
        api._opener = Mock()
        response = Mock()
        response.read.return_value = b'{"ok":true}'
        context = api._opener.open.return_value
        # Mock does not supply the context-manager protocol by default.
        from unittest.mock import MagicMock
        context = MagicMock()
        context.__enter__.return_value = response
        api._opener.open.return_value = context
        self.assertEqual(api.get("/branches/main"), {"ok": True})
        request = api._opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.github.com/repos/" + ci.REPOSITORY + "/branches/main")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        with self.assertRaises(ci.Blocked):
            api.get("/../other")

    def test_api_failure_redacted(self):
        from unittest.mock import Mock
        import urllib.error
        api = ci.GitHubAPI("synthetic-token")
        api._opener = Mock()
        api._opener.open.side_effect = urllib.error.HTTPError("https://api.github.com/", 403,
                                                               "synthetic-token", {}, None)
        with self.assertRaisesRegex(ci.Blocked, r"HTTP 403") as caught:
            api.get("/branches/main")
        self.assertNotIn("synthetic-token", str(caught.exception))


class MockECR:
    def __init__(self, expected):
        self.expected = expected
        self.calls = []
        self.lookups = [None, expected]
        self.identity = {"Account": ci.AWS_ACCOUNT,
                         "Arn": f"arn:aws:sts::{ci.AWS_ACCOUNT}:assumed-role/{ci.ECR_ROLE}/test-session"}
        self.repo = {"registryId": ci.AWS_ACCOUNT, "repositoryName": ci.ECR_NAME,
                     "repositoryUri": ci.ECR_URI, "repositoryArn": ci.ECR_ARN,
                     "imageTagMutability": "IMMUTABLE"}
        self.copy_error = False
        self.login_error = False
        self.token_error = False
        self.auth_path = None
        self.sha = "d" * 40

    def __call__(self, argv, *, input, timeout):
        import subprocess
        self.calls.append((argv, input, timeout))
        def result(data=None, code=0, stderr=b""):
            return subprocess.CompletedProcess(argv, code, encode(data) if data is not None else b"", stderr)
        if argv[:3] == ["aws", "sts", "get-caller-identity"]:
            return result(self.identity)
        if argv[:3] == ["aws", "ecr", "describe-repositories"]:
            return result({"repositories": [self.repo]})
        if argv[:3] == ["aws", "ecr", "describe-images"]:
            value = self.lookups.pop(0)
            if value is None:
                return result(code=254, stderr=b"An error occurred (ImageNotFoundException) when calling the DescribeImages operation: missing")
            if value == "denied":
                return result(code=254, stderr=b"AccessDeniedException")
            if isinstance(value, dict):
                return result(value)
            return result({"imageDetails": [{"registryId": ci.AWS_ACCOUNT, "repositoryName": ci.ECR_NAME,
                                              "imageTags": [self.sha], "imageDigest": value}]})
        if argv[:3] == ["aws", "ecr", "get-login-password"]:
            return subprocess.CompletedProcess(argv, int(self.token_error), b"synthetic-password\n", b"")
        if argv[:2] == ["skopeo", "login"]:
            self.auth_path = Path(argv[argv.index("--authfile") + 1])
            self.auth_path.write_text("synthetic auth")
            return result(code=int(self.login_error))
        if argv[:2] == ["skopeo", "copy"]:
            if self.copy_error == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            return result(code=int(bool(self.copy_error)))
        raise AssertionError("Unexpected command")

    def count(self, prefix):
        return sum(args[:len(prefix)] == prefix for args, _, _ in self.calls)


class ECRPublicationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "image.oci.tar"
        raw, self.evidence = fixture()
        self.path.write_bytes(raw)
        self.expected = self.evidence["image"]["manifest_digest"]
        self.aws = MockECR(self.expected)
        self.pauses = []

    def publish(self):
        return ci.simulate_publication(self.path, self.evidence, self.aws.sha,
                                       runner=self.aws, pause=self.pauses.append)

    def test_first_publication_command_contract(self):
        result = self.publish()
        self.assertEqual(result["outcome"], "published")
        self.assertEqual(result["ecr_digest"], self.expected)
        self.assertFalse(result["publication_authorized"])
        self.assertTrue(result["simulation"])
        copy = next(x for x in self.aws.calls if x[0][:2] == ["skopeo", "copy"])
        self.assertIn("--preserve-digests", copy[0])
        self.assertIn("--dest-tls-verify=true", copy[0])
        self.assertEqual(copy[0][-1], f"docker://{ci.ECR_URI}:{self.aws.sha}")
        self.assertFalse(self.aws.auth_path.exists())
        self.assertEqual(self.aws.count(["skopeo", "copy"]), 1)
        login = next(x for x in self.aws.calls if x[0][:2] == ["skopeo", "login"])
        self.assertEqual(login[1], b"synthetic-password\n")
        self.assertNotIn("synthetic-password", " ".join(login[0]))

    def test_existing_same_digest_is_noop(self):
        self.aws.lookups = [self.expected]
        self.assertEqual(self.publish()["outcome"], "already_present")
        self.assertEqual(self.aws.count(["skopeo"]), 0)
        self.assertEqual(self.aws.count(["aws", "ecr", "get-login-password"]), 0)

    def test_existing_different_digest_blocks(self):
        self.aws.lookups = [digest(b"other")]
        with self.assertRaisesRegex(ci.Blocked, "conflict"):
            self.publish()
        self.assertEqual(self.aws.count(["skopeo"]), 0)

    def test_wrong_account_blocks(self):
        self.aws.identity["Account"] = "000000000000"
        with self.assertRaisesRegex(ci.Blocked, "account"):
            self.publish()
        self.assertEqual(len(self.aws.calls), 1)

    def test_existing_broad_role_blocks(self):
        self.aws.identity["Arn"] = self.aws.identity["Arn"].replace(ci.ECR_ROLE, "toi-github-actions-role")
        with self.assertRaisesRegex(ci.Blocked, "assumed role"):
            self.publish()

    def test_mutable_repository_blocks(self):
        self.aws.repo["imageTagMutability"] = "MUTABLE"
        with self.assertRaisesRegex(ci.Blocked, "immutable"):
            self.publish()
        self.assertEqual(self.aws.count(["skopeo"]), 0)

    def test_wrong_repository_blocks(self):
        self.aws.repo["repositoryUri"] += "-other"
        with self.assertRaisesRegex(ci.Blocked, "Wrong ECR repository"):
            self.publish()

    def test_lookup_denied_does_not_mean_absent(self):
        self.aws.lookups = ["denied"]
        with self.assertRaisesRegex(ci.Blocked, "lookup failed"):
            self.publish()
        self.assertEqual(self.aws.count(["skopeo"]), 0)

    def test_empty_successful_lookup_is_not_absence(self):
        self.aws.lookups = [{"imageDetails": []}]
        with self.assertRaisesRegex(ci.Blocked, "inventory"):
            self.publish()
        self.assertEqual(self.aws.count(["skopeo"]), 0)

    def test_copy_timeout_reconciles(self):
        self.aws.copy_error = "timeout"
        self.assertEqual(self.publish()["outcome"], "reconciled")
        self.assertEqual(self.aws.count(["skopeo", "copy"]), 1)

    def test_delayed_visibility_after_copy(self):
        self.aws.lookups = [None, None, "denied", self.expected]
        self.assertEqual(self.publish()["outcome"], "published")
        self.assertEqual(self.pauses, [1, 2])
        self.assertEqual(self.aws.count(["skopeo", "copy"]), 1)

    def test_failed_copy_unconfirmed_no_retry(self):
        self.aws.copy_error = True
        self.aws.lookups = [None, None, None, None]
        with self.assertRaisesRegex(ci.Blocked, "unconfirmed"):
            self.publish()
        self.assertEqual(self.aws.count(["skopeo", "copy"]), 1)
        self.assertFalse(self.aws.auth_path.exists())

    def test_post_copy_conflict(self):
        self.aws.lookups = [None, digest(b"other")]
        with self.assertRaisesRegex(ci.Blocked, "conflict"):
            self.publish()
        self.assertEqual(self.aws.count(["skopeo", "copy"]), 1)

    def test_login_failed_no_copy(self):
        self.aws.login_error = True
        self.aws.lookups = [None, None, None, None]
        with self.assertRaisesRegex(ci.Blocked, "unconfirmed"):
            self.publish()
        self.assertEqual(self.aws.count(["skopeo", "copy"]), 0)
        self.assertFalse(self.aws.auth_path.exists())

    def test_token_failure_no_login(self):
        self.aws.token_error = True
        self.aws.lookups = [None, None, None, None]
        with self.assertRaises(ci.Blocked) as caught:
            self.publish()
        self.assertNotIn("synthetic-password", str(caught.exception))
        self.assertEqual(self.aws.count(["skopeo"]), 0)

    def test_tampered_archive_never_calls_transport(self):
        self.path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ci.Blocked, "Archive SHA-256"):
            self.publish()
        self.assertEqual(self.aws.calls, [])

    def test_invalid_source_sha_never_calls_transport(self):
        self.aws.sha = "latest"
        with self.assertRaisesRegex(ci.Blocked, "source SHA"):
            self.publish()
        self.assertEqual(self.aws.calls, [])



class PublicationEvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.archive = self.directory / "image.oci.tar"
        raw, self.evidence = fixture()
        self.archive.write_bytes(raw)
        self.aws = MockECR(self.evidence["image"]["manifest_digest"])
        self.output = self.directory / ci.SIMULATION_EVIDENCE
        self.context = {"publish_run_id": 456, "publish_run_attempt": 1,
                        "publish_workflow_sha": "d" * 40}
        self.now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)

    def run_simulation(self):
        return ci.simulate_with_evidence(self.archive, self.evidence, self.aws.sha, self.directory,
                                         runner=self.aws, clock=lambda: self.now, **self.context)

    def test_confirmed_record_has_required_identifiers(self):
        record = self.run_simulation()
        self.assertEqual(json.loads(self.output.read_text()), record)
        self.assertEqual(record["source"]["sha"], self.aws.sha)
        self.assertEqual(record["source"]["tree"], self.evidence["release"]["merge_tree_sha"])
        self.assertEqual(record["release_workflow"], self.evidence["workflow"])
        self.assertEqual(record["release"]["pr_number"], self.evidence["release"]["pr_number"])
        self.assertEqual(record["release"]["base_sha"], self.evidence["release"]["base_sha"])
        self.assertEqual(record["release"]["head_sha"], self.evidence["release"]["head_sha"])
        self.assertEqual(record["image"]["archive_sha256"], self.evidence["image"]["archive_sha256"])
        self.assertEqual(record["image"]["config_digest"], self.evidence["image"]["config_digest"])
        self.assertEqual(record["ecr"]["digest_uri"], f"{ci.ECR_URI}@{self.aws.expected}")
        self.assertEqual(record["ecr"]["tag"], self.aws.sha)
        self.assertEqual(record["publish_workflow"], {"run_id": 456, "run_attempt": 1, "workflow_sha": "d" * 40})
        self.assertEqual(record["confirmed_at"], "2026-11-25T12:00:00Z")

    def test_simulation_never_deployable(self):
        record = self.run_simulation()
        self.assertTrue(record["simulation"])
        self.assertFalse(record["deployable"])
        self.assertFalse(record["publication_authorized"])
        self.assertFalse(record["source"]["tree_verified"])
        self.assertFalse(record["ecr"]["remote_verified"])
        self.assertFalse(record["release"]["approval_contract_verified"])
        self.assertFalse((self.directory / "image-publish-evidence.json").exists())

    def test_existing_same_digest_record(self):
        self.aws.lookups = [self.aws.expected]
        self.assertEqual(self.run_simulation()["outcome"], "already_present")
        self.assertEqual(self.aws.count(["skopeo"]), 0)

    def test_response_loss_reconciled_record(self):
        self.aws.copy_error = "timeout"
        self.assertEqual(self.run_simulation()["outcome"], "reconciled")
        self.assertEqual(self.aws.count(["skopeo", "copy"]), 1)

    def test_unconfirmed_no_record(self):
        self.aws.lookups = [None, None, None, None]
        with self.assertRaises(ci.Blocked):
            self.run_simulation()
        self.assertFalse(self.output.exists())

    def test_digest_conflict_no_record(self):
        self.aws.lookups = [digest(b"different")]
        with self.assertRaises(ci.Blocked):
            self.run_simulation()
        self.assertFalse(self.output.exists())

    def test_existing_record_not_overwritten(self):
        self.output.write_text("keep me")
        with self.assertRaisesRegex(ci.Blocked, "already exists"):
            self.run_simulation()
        self.assertEqual(self.output.read_text(), "keep me")
        self.assertEqual(self.aws.calls, [])

    def test_invalid_publish_identifiers_before_transport(self):
        for key, value in (("publish_run_id", True), ("publish_run_attempt", 0),
                           ("publish_workflow_sha", "main")):
            with self.subTest(key=key):
                original = self.context[key]
                self.context[key] = value
                with self.assertRaises(ci.Blocked):
                    self.run_simulation()
                self.context[key] = original
        self.assertEqual(self.aws.calls, [])
        self.assertFalse(self.output.exists())

    def test_record_allowlist_excludes_unrelated_secrets(self):
        self.evidence["secret"] = "synthetic-secret-value"
        self.run_simulation()
        self.assertNotIn("synthetic-secret-value", self.output.read_text())
        self.assertNotIn("synthetic-password", self.output.read_text())

    def test_record_rejects_forged_or_mismatched_result(self):
        result = ci.simulate_publication(self.archive, self.evidence, self.aws.sha, runner=self.aws)
        for change in ({"simulation": False}, {"publication_authorized": True},
                       {"ecr_digest": digest(b"different")}, {"integrity_verified": False},
                       {"outcome": "failed"}, {"archive_sha256": "0" * 64}):
            with self.subTest(change=change), self.assertRaises(ci.Blocked):
                ci.simulation_evidence(self.evidence, {**result, **change}, confirmed_at=self.now, **self.context)

    def test_naive_timestamp_rejected(self):
        result = ci.simulate_publication(self.archive, self.evidence, self.aws.sha, runner=self.aws)
        with self.assertRaisesRegex(ci.Blocked, "timezone"):
            ci.simulation_evidence(self.evidence, result, confirmed_at=self.now.replace(tzinfo=None), **self.context)

    def test_persistence_failure_cleans_temp_and_produces_no_record(self):
        from unittest.mock import patch
        with patch.object(ci.os, "link", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(ci.Blocked, "persist"):
                self.run_simulation()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.directory.glob(".publish-evidence-*")), [])


class ProductionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.integration = GitHubIntegrationTests()
        self.integration.setUp()
        self.addCleanup(self.integration.doCleanups)
        self.bundle = self.directory / "verified"
        self.integration.verify(output_directory=self.bundle)
        self.output = self.directory / "published"
        self.output.mkdir()
        self.aws = MockECR(self.integration.fixture.evidence["image"]["manifest_digest"])
        self.now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)

    def publish(self):
        return ci.publish_with_evidence(
            self.bundle, self.output, runner=self.aws,
            publish_run_id=456, publish_run_attempt=1,
            publish_workflow_sha=self.integration.fixture.source, dispatcher="publish-admin",
            clock=lambda: self.now)

    def test_production_record_matches_backend_contract_v1(self):
        record = self.publish()
        evidence_file = self.output / ci.EVIDENCE_FILE
        self.assertEqual(json.loads(evidence_file.read_text()), record)
        self.assertEqual(set(record), {
            "schema_version", "record_type", "simulation", "publication_authorized", "deployable",
            "repository", "source", "release", "release_workflow", "approval", "verifier",
            "release_artifact", "verification", "image", "ecr", "publish_workflow", "outcome",
            "confirmed_at",
        })
        self.assertEqual(set(record["source"]), {"sha", "tree"})
        self.assertEqual(set(record["release"]), {
            "pr_number", "base_sha", "head_sha", "merge_tree_sha", "change_digest",
        })
        self.assertEqual(set(record["release_workflow"]), {
            "run_id", "run_attempt", "workflow_sha", "dispatcher", "official_job_id",
        })
        self.assertEqual(set(record["approval"]), {"comment_id", "approver", "record_sha256", "review"})
        self.assertEqual(set(record["verifier"]), {"files"})
        self.assertEqual(set(record["release_artifact"]), {"id", "digest", "name"})
        self.assertEqual(set(record["image"]), {
            "platform", "archive_sha256", "config_digest", "manifest_digest",
        })
        self.assertEqual(set(record["ecr"]), {
            "account_id", "region", "repository_uri", "tag", "digest", "digest_uri", "remote_verified",
        })
        self.assertEqual(set(record["publish_workflow"]), {
            "run_id", "run_attempt", "workflow_sha", "dispatcher",
        })
        self.assertEqual(record["record_type"], "production")
        self.assertTrue(record["publication_authorized"] and record["deployable"])
        self.assertFalse(record["simulation"])
        self.assertTrue(record["ecr"]["remote_verified"])
        self.assertEqual(record["ecr"]["digest"], record["image"]["manifest_digest"])
        self.assertEqual(record["confirmed_at"], "2026-11-25T12:00:00Z")

    def test_mutated_bundle_is_blocked_before_transport_or_evidence(self):
        archive = self.bundle / "image.oci.tar"
        archive.write_bytes(archive.read_bytes() + b"tampered")
        with self.assertRaises(ci.Blocked):
            self.publish()
        self.assertEqual(self.aws.calls, [])
        self.assertFalse((self.output / ci.EVIDENCE_FILE).exists())

    def test_unauthorized_context_is_blocked_before_transport(self):
        path = self.bundle / ci.VERIFIED_CONTEXT_FILE
        context = json.loads(path.read_text())
        context["publication_authorized"] = False
        path.write_text(json.dumps(context))
        with self.assertRaises(ci.Blocked):
            self.publish()
        self.assertEqual(self.aws.calls, [])


class WorkflowDownloadTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        self.integration = GitHubIntegrationTests()
        self.integration.setUp()
        self.addCleanup(self.integration.doCleanups)
        self.path = self.integration.path.with_name("downloaded.zip")
        self.payload = self.integration.path.read_bytes()
        self.api = self.integration.api
        self.artifact = self.integration.fixture.artifact
        self.options = dict(token="synthetic-token", run_id=123, attempt=2,
                            trusted_workflow_shas={"a" * 40}, now=self.integration.fixture.now)
        self.runner = Mock(side_effect=self.copy_payload)

    def copy_payload(self, argv, **kwargs):
        import subprocess
        Path(argv[argv.index("--output") + 1]).write_bytes(self.payload)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def download(self, **changes):
        return ci.download_release_zip(self.api, self.path, runner=self.runner, **{**self.options, **changes})

    def test_exact_artifact_and_token_not_in_arguments(self):
        self.assertEqual(self.download(), self.artifact["id"])
        self.assertEqual(self.path.read_bytes(), self.payload)
        argv = self.runner.call_args.args[0]
        self.assertEqual(argv[-1], f"https://api.github.com/repos/{ci.REPOSITORY}/actions/artifacts/{self.artifact['id']}/zip")
        self.assertNotIn("synthetic-token", " ".join(argv))
        self.assertIn(b"Authorization: Bearer synthetic-token", self.runner.call_args.kwargs["input"])
        self.assertNotIn("--location-trusted", argv)
        self.assertEqual(argv[argv.index("--proto-redir") + 1], "=https")

    def test_wrong_attempt_or_untrusted_run_never_downloads(self):
        for changes in ({"attempt": 1}, {"trusted_workflow_shas": {"f" * 40}}):
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.download(**changes)
        self.runner.assert_not_called()

    def test_missing_duplicate_or_expired_artifact_never_downloads(self):
        endpoint = "/actions/runs/123/artifacts?per_page=100&page=1"
        for rows in ([], [self.artifact, self.artifact], [{**self.artifact, "expired": True}]):
            self.integration.records[endpoint] = {"total_count": len(rows), "artifacts": rows}
            with self.subTest(rows=rows), self.assertRaises(ci.Blocked):
                self.download()
        self.runner.assert_not_called()

    def test_wrong_origin_or_oversized_artifact_never_downloads(self):
        endpoint = "/actions/runs/123/artifacts?per_page=100&page=1"
        for changes in ({"workflow_run": {**self.artifact["workflow_run"], "id": 999}},
                        {"size_in_bytes": ci.MAX_ARCHIVE + 1}):
            self.integration.records[endpoint] = {"total_count": 1, "artifacts": [{**self.artifact, **changes}]}
            with self.subTest(changes=changes), self.assertRaises(ci.Blocked):
                self.download()
        self.runner.assert_not_called()

    def test_tampered_download_is_removed(self):
        self.payload = b"tampered"
        with self.assertRaisesRegex(ci.Blocked, "download or digest"):
            self.download()
        self.assertFalse(self.path.exists())

    def test_download_failure_is_redacted_and_removed(self):
        import subprocess
        self.runner.side_effect = subprocess.TimeoutExpired(["curl"], 610, stderr=b"synthetic-token")
        with self.assertRaises(ci.Blocked) as caught:
            self.download()
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_existing_download_is_never_overwritten(self):
        self.path.write_bytes(b"keep")
        with self.assertRaises(ci.Blocked):
            self.download()
        self.assertEqual(self.path.read_bytes(), b"keep")
        self.runner.assert_not_called()


class WorkflowActivationTests(unittest.TestCase):
    def test_workflow_gates_oidc_and_publish_on_verified_release(self):
        path = MODULE.parents[1] / ".github/workflows/ecr-image-publish.yml"
        workflow = path.read_text(encoding="utf-8")
        self.assertIn("id-token: write", workflow)
        self.assertEqual(workflow.count("actions: read"), 2)
        self.assertEqual(workflow.count("issues: read"), 2)
        self.assertIn("configure-aws-credentials@", workflow)
        self.assertIn("--preserve-digests", MODULE.read_text(encoding="utf-8"))
        self.assertNotIn("if: ${{ false }}", workflow)
        self.assertIn("publication_authorized == 'true'", workflow)
        self.assertIn("image-publish-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("needs: validate", workflow)
        self.assertIn("github.ref_protected", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("cancel-in-progress: false", workflow)


class PublicationPolicyTests(unittest.TestCase):
    def setUp(self):
        path = MODULE.parents[1] / "deploy/iam/ecr-publish-policy.json"
        self.policy = json.loads(path.read_text(encoding="utf-8"))
        self.login, self.repository = self.policy["Statement"]

    def test_policy_version_and_exact_statement_inventory(self):
        self.assertEqual(set(self.policy), {"Version", "Statement"})
        self.assertEqual(self.policy["Version"], "2012-10-17")
        self.assertEqual(len(self.policy["Statement"]), 2)
        for statement in self.policy["Statement"]:
            self.assertEqual(set(statement), {"Sid", "Effect", "Action", "Resource", "Condition"})
            self.assertEqual(statement["Effect"], "Allow")

    def test_login_is_only_global_action(self):
        self.assertEqual(self.login["Action"], "ecr:GetAuthorizationToken")
        self.assertEqual(self.login["Resource"], "*")
        self.assertEqual(self.repository["Resource"], ci.ECR_ARN)

    def test_every_statement_is_region_limited(self):
        for statement in self.policy["Statement"]:
            self.assertEqual(statement["Condition"], {"StringEquals": {"aws:RequestedRegion": ci.AWS_REGION}})

    def test_repository_action_allowlist(self):
        expected = {"ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
                    "ecr:DescribeImages", "ecr:DescribeRepositories"}
        actions = self.repository["Action"]
        self.assertEqual(len(actions), len(expected))
        self.assertEqual(set(actions), expected)

    def test_no_delete_deploy_or_administration_grants(self):
        actions = [self.login["Action"], *self.repository["Action"]]
        self.assertTrue(all(a.startswith("ecr:") and "*" not in a for a in actions))
        self.assertTrue(set(actions).isdisjoint({"ecr:BatchDeleteImage", "ecr:DeleteRepository",
                          "ecr:PutLifecyclePolicy", "ecr:PutImageTagMutability",
                          "ecr:PutRegistryScanningConfiguration", "ecr:SetRepositoryPolicy",
                          "ecr:CreateRepository", "ecr:StartImageScan", "ssm:SendCommand"}))

    def test_runbook_documents_active_publish_contract(self):
        path = MODULE.parents[1] / "docs/deployment/ECR_PUBLISH.md"
        text = path.read_text(encoding="utf-8")
        for expected in ("image-publish-evidence.json", "TRUSTED_PUBLISHERS",
                         "TRUSTED_RELEASE_WORKFLOW_SHA", "ECR_PUBLISH_ROLE_ARN",
                         "repo:ToI-walkwithdog@339425868/walkwithdog-BE@1409697696:ref:refs/heads/main"):
            self.assertIn(expected, text)
        self.assertNotIn("aws iam create-role", text)
        self.assertNotIn("aws iam put-role-policy", text)


class LoopbackRegistry:
    """Tiny test double backed by actual HTTP requests and immutable tag state.

    This is NOT an OCI Distribution implementation: blob PUT is deliberately
    simplified. Only the opt-in Skopeo test exercises a real registry protocol.
    """
    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from urllib.parse import urlsplit
        self.blobs = {}
        self.tags = {}
        self.writes = 0
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, payload=b""):
                self.send_response(code)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path.startswith("/blobs/"):
                    data = registry.blobs.get(path.removeprefix("/blobs/"))
                elif path.startswith("/manifests/"):
                    data = registry.tags.get(path.removeprefix("/manifests/"))
                else:
                    data = None
                self.reply(404 if data is None else 200, data or b"")

            def do_PUT(self):
                path = urlsplit(self.path).path
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024 * 1024:
                    return self.reply(400)
                data = self.rfile.read(size)
                if path.startswith("/blobs/"):
                    key = path.removeprefix("/blobs/")
                    if digest(data) != key:
                        return self.reply(400)
                    registry.blobs[key] = data
                elif path.startswith("/manifests/"):
                    key = path.removeprefix("/manifests/")
                    if key in registry.tags and registry.tags[key] != data:
                        return self.reply(409)
                    manifest = json.loads(data)
                    for descriptor in [manifest["config"], *manifest["layers"]]:
                        blob = registry.blobs.get(descriptor["digest"])
                        if blob is None or len(blob) != descriptor["size"]:
                            return self.reply(400)
                    registry.tags[key] = data
                    registry.writes += 1
                else:
                    return self.reply(404)
                self.reply(201)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, payload=None):
        from urllib.request import build_opener, ProxyHandler, Request
        from urllib.error import HTTPError
        opener = build_opener(ProxyHandler({}))
        req = Request(self.url + path, data=payload, method="PUT" if payload is not None else "GET")
        try:
            with opener.open(req, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()


class RegistryRunner(MockECR):
    """Test adapter: mock AWS responses, use loopback HTTP for actual image bytes."""
    def __init__(self, expected, registry):
        super().__init__(expected)
        self.registry = registry
        self.lose_response = False

    def __call__(self, argv, *, input, timeout):
        import subprocess
        if argv[:3] == ["aws", "ecr", "describe-images"]:
            status, data = self.registry.request("/manifests/" + self.sha)
            if status not in (200, 404):
                raise AssertionError("Unexpected loopback status")
            self.lookups = [digest(data) if status == 200 else None]
        if argv[:2] != ["skopeo", "copy"]:
            return super().__call__(argv, input=input, timeout=timeout)
        self.calls.append((argv, input, timeout))
        assert argv[-1] == f"docker://{ci.ECR_URI}:{self.sha}"
        assert argv[-2].startswith("oci-archive:") and "--preserve-digests" in argv
        with tarfile.open(argv[-2].removeprefix("oci-archive:"), "r:") as archive:
            manifest_digest = json.load(archive.extractfile("index.json"))["manifests"][0]["digest"]
            manifest_raw = archive.extractfile("blobs/sha256/" + manifest_digest[7:]).read()
            manifest = json.loads(manifest_raw)
            for descriptor in [manifest["config"], *manifest["layers"]]:
                blob_digest = descriptor["digest"]
                data = archive.extractfile("blobs/sha256/" + blob_digest[7:]).read()
                assert self.registry.request("/blobs/" + blob_digest, data)[0] == 201
            status, _ = self.registry.request("/manifests/" + self.sha, manifest_raw)
        if self.lose_response:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, int(status != 201), b"", b"")


class IsolatedFlowTests(unittest.TestCase):
    def setUp(self):
        import zipfile
        self.integration = GitHubIntegrationTests()
        self.integration.setUp()
        self.addCleanup(self.integration.doCleanups)
        self.directory = self.integration.path.parent
        self.archive = self.directory / "image.oci.tar"
        with zipfile.ZipFile(self.integration.path) as bundle:
            self.archive.write_bytes(bundle.read("image.oci.tar"))
        self.evidence = self.integration.evidence
        self.registry = LoopbackRegistry()
        self.addCleanup(self.registry.close)
        self.runner = RegistryRunner(self.evidence["image"]["manifest_digest"], self.registry)
        self.runner.sha = self.integration.fixture.source
        self.output = self.directory / ci.SIMULATION_EVIDENCE

    def simulate(self, directory=None):
        return ci.simulate_with_evidence(self.archive, self.evidence, self.runner.sha,
                                         directory or self.directory, runner=self.runner,
                                         publish_run_id=456, publish_run_attempt=1,
                                         publish_workflow_sha=self.runner.sha)

    def test_verified_artifact_and_simulated_registry_evidence(self):
        # Verification is still not authorization. The subsequent operation is
        # explicitly a simulation with a loopback-only transport.
        verified = self.integration.verify()
        self.assertTrue(verified["metadata_verified"] and verified["integrity_verified"])
        self.assertTrue(verified["approval_contract_verified"] and verified["publication_authorized"])
        record = self.simulate()
        status, raw = self.registry.request("/manifests/" + self.runner.sha)
        self.assertEqual(status, 200)
        self.assertEqual(digest(raw), verified["manifest_digest"])
        manifest = json.loads(raw)
        for descriptor in [manifest["config"], *manifest["layers"]]:
            status, blob = self.registry.request("/blobs/" + descriptor["digest"])
            self.assertEqual((status, digest(blob), len(blob)), (200, descriptor["digest"], descriptor["size"]))
        self.assertEqual(json.loads(self.output.read_text()), record)
        self.assertEqual(record["ecr"]["digest"], digest(raw))
        self.assertTrue(record["simulation"])
        self.assertFalse(record["publication_authorized"] or record["deployable"])
        self.assertFalse((self.directory / "image-publish-evidence.json").exists())

    def test_idempotency_reads_persisted_registry_state(self):
        self.assertEqual(self.simulate()["outcome"], "published")
        other = self.directory / "rerun"
        other.mkdir()
        self.assertEqual(self.simulate(other)["outcome"], "already_present")
        self.assertEqual(self.registry.writes, 1)
        self.assertEqual(self.runner.count(["skopeo", "copy"]), 1)

    def test_conflict_preserves_original_registry_bytes(self):
        self.simulate()
        old = self.registry.request("/manifests/" + self.runner.sha)[1]
        raw, self.evidence = fixture(gzip_layer=False)
        self.archive.write_bytes(raw)
        other = self.directory / "conflict"
        other.mkdir()
        with self.assertRaisesRegex(ci.Blocked, "digest conflict"):
            self.simulate(other)
        self.assertEqual(self.registry.request("/manifests/" + self.runner.sha)[1], old)
        self.assertEqual(self.registry.writes, 1)
        self.assertFalse((other / ci.SIMULATION_EVIDENCE).exists())

    def test_response_loss_reconciles_registry_state(self):
        self.runner.lose_response = True
        self.assertEqual(self.simulate()["outcome"], "reconciled")
        self.assertEqual(self.registry.writes, 1)
        self.assertEqual(self.runner.count(["skopeo", "copy"]), 1)

    def test_mutated_archive_never_reaches_transport(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"changed")
        with self.assertRaisesRegex(ci.Blocked, "SHA-256 mismatch"):
            self.simulate()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.registry.writes, 0)
        self.assertFalse(self.output.exists())


class RealRegistryTests(unittest.TestCase):
    """Optional real Skopeo + disposable Registry; never connects to AWS."""
    @unittest.skipUnless(__import__("os").environ.get("TOI_TEST_LOCAL_REGISTRY") == "1",
                         "Requires explicit opt-in, Skopeo and disposable 127.0.0.1:5000 registry")
    def test_real_skopeo_preserves_oci_manifest_and_blobs(self):
        import os
        import shutil
        import subprocess
        import uuid
        from urllib.request import build_opener, ProxyHandler, Request
        skopeo = shutil.which("skopeo")
        self.assertIsNotNone(skopeo, "Skopeo is required for opt-in test")
        # A fixed loopback target cannot be redirected to an ECR endpoint by input.
        host = "127.0.0.1:5000"
        name = "toi-ecr-test-" + uuid.uuid4().hex
        tag = "d" * 40
        raw, evidence = fixture()
        opener = build_opener(ProxyHandler({}), ci.NoRedirect())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.oci.tar"
            path.write_bytes(raw)
            verified = ci.verify_archive(path, evidence)
            auth = Path(tmp) / "auth.json"
            auth.write_text('{"auths":{}}', encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "WINDIR", "LANG")}
            env.update({"HOME": tmp, "XDG_RUNTIME_DIR": tmp, "NO_PROXY": "127.0.0.1"})
            subprocess.run([skopeo, "copy", "--preserve-digests", "--dest-tls-verify=false",
                            "--authfile", str(auth), f"oci-archive:{path}", f"docker://{host}/{name}:{tag}"],
                           check=True, timeout=120, env=env)
            with opener.open(Request(f"http://{host}/v2/{name}/manifests/{tag}",
                                     headers={"Accept": ci.MANIFEST}), timeout=10) as response:
                manifest_raw = response.read()
                self.assertEqual(response.headers.get("Docker-Content-Digest"), verified["manifest_digest"])
            self.assertEqual(digest(manifest_raw), verified["manifest_digest"])
            manifest = json.loads(manifest_raw)
            for descriptor in [manifest["config"], *manifest["layers"]]:
                with opener.open(f"http://{host}/v2/{name}/blobs/{descriptor['digest']}", timeout=10) as response:
                    blob = response.read()
                self.assertEqual((digest(blob), len(blob)), (descriptor["digest"], descriptor["size"]))


class ReviewHardeningTests(unittest.TestCase):
    def test_nonstandard_json_constants_rejected(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant), self.assertRaisesRegex(ci.Blocked, "Non-standard"):
                ci.parse_json(b'{"number":' + constant + b'}')

    def test_github_api_nonstandard_json_rejected(self):
        from unittest.mock import MagicMock
        api = ci.GitHubAPI("synthetic-token")
        api._opener = MagicMock()
        api._opener.open.return_value.__enter__.return_value.read.return_value = b'{"n":NaN}'
        with self.assertRaisesRegex(ci.Blocked, "Non-standard"):
            api.get("/branches/main")

    def test_stream_size_boundary(self):
        self.assertEqual(ci.measure_stream(io.BytesIO(b"abc"), 3), (digest(b"abc")[7:], 3))
        with self.assertRaisesRegex(ci.Blocked, "size limit"):
            ci.measure_stream(io.BytesIO(b"abc"), 2)

    def test_layer_expansion_limit(self):
        from unittest.mock import patch
        raw, evidence = fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.oci.tar"
            path.write_bytes(raw)
            with patch.object(ci, "MAX_UNCOMPRESSED_LAYERS", 1024):
                with self.assertRaisesRegex(ci.Blocked, "size limit"):
                    ci.verify_archive(path, evidence)

    def test_main_change_during_archive_verification(self):
        from unittest.mock import patch
        integration = GitHubIntegrationTests()
        integration.setUp()
        self.addCleanup(integration.doCleanups)
        real = ci.verify_archive

        def changed(*args, **kwargs):
            result = real(*args, **kwargs)
            integration.records["/branches/main"] = {
                "name": "main", "protected": True, "commit": {"sha": "e" * 40}}
            return result

        with patch.object(ci, "verify_archive", side_effect=changed):
            with self.assertRaisesRegex(ci.Blocked, "protected main"):
                integration.verify()

    def test_run_change_during_archive_verification(self):
        from unittest.mock import patch
        integration = GitHubIntegrationTests()
        integration.setUp()
        self.addCleanup(integration.doCleanups)
        real = ci.verify_archive

        def changed(*args, **kwargs):
            result = real(*args, **kwargs)
            run = integration.records["/actions/runs/123"]
            integration.records["/actions/runs/123"] = {**run, "run_attempt": 3}
            return result

        with patch.object(ci, "verify_archive", side_effect=changed):
            with self.assertRaisesRegex(ci.Blocked, "Run changed"):
                integration.verify()

    def test_download_reservation_race_preserves_other_file(self):
        from unittest.mock import patch
        fixture_case = WorkflowDownloadTests()
        fixture_case.setUp()
        self.addCleanup(fixture_case.doCleanups)
        original = Path.open

        def concurrent_create(path, mode="r", *args, **kwargs):
            if path == fixture_case.path and mode == "xb":
                with original(path, "wb") as other:
                    other.write(b"concurrent owner")
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", concurrent_create):
            with self.assertRaisesRegex(ci.Blocked, "reserve download target"):
                fixture_case.download()
        self.assertEqual(fixture_case.path.read_bytes(), b"concurrent owner")
        fixture_case.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
