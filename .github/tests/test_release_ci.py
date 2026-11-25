"""Unit checks for the Release CI installation tools."""
import gzip
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[2] / "scripts" / "release_ci.py"
SPEC = importlib.util.spec_from_file_location("release_ci", MODULE)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def entries(items):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in items:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    return stream.getvalue()


class InstallationTests(unittest.TestCase):
    head = "b" * 40
    merge = "c" * 40

    def event(self, base=ci.BASE, head=None, branch="chore/release-ci"):
        repo = {"full_name": ci.REPOSITORY}
        return {"pull_request": {
            "base": {"sha": base, "ref": "main", "repo": repo},
            "head": {"sha": head or self.head, "ref": branch, "repo": repo},
            "body": "All changes approved",
        }}

    def git_evidence(self, paths, base=ci.BASE):
        parents = f"{self.merge} {base} {self.head}".encode()
        return [parents, b"\0".join(path.encode() for path in paths) + b"\0"]

    def test_exact_installation_paths(self):
        with patch.object(ci, "git", side_effect=self.git_evidence(ci.PATHS)):
            self.assertEqual(ci.claim(Path("."), self.event(), self.head, self.merge), ci.BASE)

    def test_pr_body_cannot_authorize_extra_or_missing_path(self):
        for paths in (ci.PATHS | {"requirements.txt"}, ci.PATHS - {"scripts/release_ci.py"}):
            with self.subTest(paths=paths), patch.object(ci, "git", side_effect=self.git_evidence(paths)):
                with self.assertRaisesRegex(ci.Blocked, "four approved paths"):
                    ci.claim(Path("."), self.event(), self.head, self.merge)

    def test_candidate_self_approval_does_not_enable_release(self):
        actual = self.event(branch="feature/unapproved")
        with patch.object(ci, "git", side_effect=self.git_evidence(ci.PATHS)):
            with self.assertRaisesRegex(ci.Blocked, "independent execution"):
                ci.claim(Path("."), actual, self.head, self.merge)

    def test_wrong_base_commit_or_head(self):
        with patch.object(ci, "git", side_effect=self.git_evidence(ci.PATHS)):
            with self.assertRaisesRegex(ci.Blocked, "exact base and head"):
                ci.claim(Path("."), self.event(base="d" * 40), self.head, self.merge)
        with self.assertRaisesRegex(ci.Blocked, "Wrong PR head"):
            ci.claim(Path("."), self.event(head="e" * 40), self.head, self.merge)

    def test_reference_inventory_and_checksum(self):
        files = {path: ci.sha(path.encode()) for path in ci.INPUTS}
        expected = {"issue": 125, "base": ci.BASE, "reference": ci.REFERENCE, "files": files}

        def git_fixture(_, verb, ref):
            self.assertEqual(verb, "show")
            commit, path = ref.split(":", 1)
            return json.dumps(expected).encode() if commit == self.merge else path.encode()

        with patch.object(ci, "git", side_effect=git_fixture):
            self.assertEqual(ci.pinned_inputs(Path("."), self.merge), expected)
            files["requirements.txt"] = "0" * 64
            with self.assertRaisesRegex(ci.Blocked, "checksum mismatch"):
                ci.pinned_inputs(Path("."), self.merge)

    def test_altered_sql_and_manifest_record_are_rejected(self):
        record = {"file": "015_review.sql", "sha256": "1" * 64, "depends_on": ["014_previous.sql"]}
        reference = {"migrations": [record]}
        for candidate in ({"migrations": []}, {"migrations": [{**record, "sha256": "2" * 64}]}):
            with self.assertRaisesRegex(ci.Blocked, "approved migration"):
                ci.protect_migrations(reference, candidate)

    def test_compose_version_check_uses_the_real_client(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            ci.os.environ, {"TOI_SMOKE_SCRATCH": temp}
        ), patch.object(ci, "docker") as docker:
            ci.smoke_docker(["compose", "version"])
            docker.assert_called_once_with("compose", "version")

    def test_missing_core_pass_and_new_skip_rejected(self):
        event = {"nodeid": "example", "phase": "call", "subtest": None, "outcome": "passed"}
        report = {"exit_code": 0, "collected": ["example"], "events": [event]}
        ci.require_passes(["example"], report)
        for rejected in ({**report, "events": []},
                         {**report, "events": [{**event, "outcome": "skipped"}]},
                         {**report, "collected": ["example", "unexpected"]}):
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

    def test_main_collection_does_not_silently_drop_test_ids(self):
        with self.assertRaisesRegex(ci.Blocked, "Main-only IDs"):
            ci.inventories({"exit_code": 0, "events": [], "collected": ["main-only"]},
                           {"collected": ["same-count-but-not-the-same-id"]})


class ImageTests(unittest.TestCase):
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

    def test_checks_every_layer_of_exact_image(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.tar"
            layers = [entries([("srv/walkwithdog/app/main.py", b"source")]), entries([("tmp/asset", b"data")])]
            digest = self.image_archive(image, layers)
            self.assertEqual(ci.scan_image(image, digest), 2)
            with self.assertRaisesRegex(ci.Blocked, "differs from smoke image"):
                ci.scan_image(image, "sha256:" + "0" * 64)

    def test_oci_index_binds_exact_config(self):
        config_id = "sha256:" + "1"*64
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
            self.assertFalse(ci.oci_contains_config(archive, index_id, "sha256:" + "2"*64))

    def test_deleted_secret_is_still_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.tar"
            layers = [entries([("tmp/blob", b"toi-release-ci-secret-canary-ABC")]),
                      entries([("tmp/.wh.blob", b"")])]
            digest = self.image_archive(image, layers)
            with self.assertRaisesRegex(ci.Blocked, "Credential-like"):
                ci.scan_image(image, digest)

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


if __name__ == "__main__":
    unittest.main()
