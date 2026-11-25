"""Public policy document contract tests with synthetic files only."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_contract import ContractError
from app.policy_documents import check_document_version
from app.routers import policies
from privacy_test_support import TEST_POLICY_DIRECTORY, use_test_privacy_policy


class PolicyDocumentTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(temporary) / "policies"
        shutil.copytree(TEST_POLICY_DIRECTORY, self.directory)
        self.manifest_file = self.directory / "manifest.json"
        self.entries = json.loads(self.manifest_file.read_text(encoding="utf-8"))["documents"]
        use_test_privacy_policy(self, POLICY_DOCUMENTS_DIR=str(self.directory))
        app = FastAPI()
        app.include_router(policies.router)
        self.client = self.enterContext(TestClient(app))

    def write_manifest(self, entries):
        self.manifest_file.write_text(json.dumps({"documents": entries}), encoding="utf-8")

    def assert_unavailable(self, kind):
        response = self.client.get(f"/policies/{kind}")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {
            "code": "POLICY_DOCUMENT_UNAVAILABLE", "data": {}, "message": "정책 문서를 불러올 수 없습니다.",
        })

    def test_public_document_contract_is_identical_with_or_without_bearer(self):
        for entry in self.entries:
            for headers in ({}, {"Authorization": "Bearer synthetic-invalid-token"}):
                with self.subTest(kind=entry["kind"], headers=headers):
                    response = self.client.get(f"/policies/{entry['kind']}", headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.json(), {
                        "code": "OK", "message": "정책 문서를 조회했습니다.",
                        "data": {
                            "kind": entry["kind"], "title": entry["title"], "document_version": entry["document_version"],
                            "effective_at": "2026-11-30T15:00:00+00:00", "content_format": "markdown",
                            "content": (self.directory / entry["file"]).read_text(encoding="utf-8"),
                        },
                    })

    def test_invalid_kind_is_not_normalized_or_replaced(self):
        for kind in ("Terms", "TERMS", "%20terms", "pledge"):
            with self.subTest(kind=kind):
                response = self.client.get(f"/policies/{kind}")
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.json(), {
                    "code": "INVALID_POLICY_KIND", "data": {"field": "kind"},
                    "message": "정책 문서 종류를 확인해 주세요.",
                })
        self.assertEqual(self.client.get("/policies").status_code, 404)

    def test_configured_version_switch_keeps_the_previous_file(self):
        old = self.entries[0]
        version = "terms-test-v2"
        relative = f"terms/{version}.md"
        content = b"# Synthetic replacement"
        (self.directory / relative).write_bytes(content)
        self.write_manifest([*self.entries, {**old, "document_version": version,
                                             "file": relative, "sha256": sha256(content).hexdigest()}])
        self.assertEqual(self.client.get("/policies/terms").json()["data"]["document_version"], old["document_version"])
        with patch.dict(os.environ, {"TERMS_DOCUMENT_VERSION": version}):
            result = self.client.get("/policies/terms").json()["data"]
        self.assertEqual((result["document_version"], result["content"]), (version, content.decode()))
        self.assertTrue((self.directory / old["file"]).is_file())

    def test_missing_or_invalid_version_does_not_fall_back(self):
        for version in ("", "a" * 33, "unregistered-version"):
            with self.subTest(version=version), patch.dict(os.environ, {"TERMS_DOCUMENT_VERSION": version}):
                self.assert_unavailable("terms")
                self.assertEqual(self.client.get("/policies/privacy").status_code, 200)
        with patch.dict(os.environ):
            del os.environ["PRIVACY_DOCUMENT_VERSION"]
            self.assert_unavailable("privacy")

    def test_invalid_consent_settings_still_return_generic_500(self):
        with patch.dict(os.environ, {"PRIVACY_REQUIRED_CONSENTS": "SYNTHETIC_UNKNOWN_CONSENT"}):
            response = self.client.get("/policies/privacy")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {
            "code": "INTERNAL_SERVER_ERROR", "data": {}, "message": "일시적인 오류가 발생했습니다.",
        })

    def test_manifest_and_metadata_fail_closed(self):
        terms, privacy = self.entries
        self.manifest_file.unlink()
        self.assert_unavailable("terms")
        self.manifest_file.write_text("{", encoding="utf-8")
        self.assert_unavailable("terms")
        cases = (
            [],
            [terms, terms, privacy],
            [{**terms, "title": " "}, privacy],
            [{**terms, "effective_at": None}, privacy],
            [{**terms, "effective_at": "not-a-timestamp"}, privacy],
            [{**terms, "effective_at": "2026-12-01T00:00:00"}, privacy],
            [{**terms, "file": "privacy/privacy-test-v1.md"}, privacy],
            [{**terms, "file": "terms/../outside.md"}, privacy],
            [{**terms, "file": "terms/missing.md"}, privacy],
            [{**terms, "sha256": "0" * 64}, privacy],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                self.write_manifest(entries)
                self.assert_unavailable("terms")

    def test_bad_document_bytes_are_rejected_even_with_a_matching_hash(self):
        terms, privacy = self.entries
        for body in (b"   ", b"\xff"):
            with self.subTest(body=body):
                (self.directory / terms["file"]).write_bytes(body)
                self.write_manifest([{**terms, "sha256": sha256(body).hexdigest()}, privacy])
                self.assert_unavailable("terms")

    def test_missing_file_log_does_not_disclose_path_or_settings(self):
        terms, privacy = self.entries
        self.write_manifest([{**terms, "file": "terms/synthetic-private-name.md"}, privacy])
        with self.assertLogs("app.policy_documents", "WARNING") as logs:
            self.assert_unavailable("terms")
        logged = " ".join(logs.output)
        self.assertIn("kind=terms", logged)
        for detail in (str(self.directory), "synthetic-private-name", terms["sha256"], terms["document_version"]):
            self.assertNotIn(detail, logged)

    def test_openapi_and_shared_version_error_for_future_consent_submitters(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/policies/{kind}"]["get"]
        self.assertEqual(set(operation["responses"]), {"200", "422", "500", "503"})
        self.assertNotIn("security", operation)
        check_document_version("v1", "v1", "agreements.terms.document_version")
        with self.assertRaises(ContractError) as error:
            check_document_version("v0", "v1", "agreements.terms.document_version")
        self.assertEqual((error.exception.status_code, error.exception.code), (409, "POLICY_VERSION_MISMATCH"))
        self.assertEqual(error.exception.data, {"field": "agreements.terms.document_version"})

    def test_repository_manifest_entries_match_document_hashes(self):
        directory = Path(__file__).resolve().parents[1] / "policies"
        entries = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["documents"]
        for entry in entries:
            with self.subTest(file=entry["file"]):
                content = (directory / entry["file"]).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(sha256(content).hexdigest(), entry["sha256"])
