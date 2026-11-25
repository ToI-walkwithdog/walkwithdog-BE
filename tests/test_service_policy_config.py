import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import policies
from tests.test_shelter_code import ShelterCodeFixture
from app.volunteer_level_service import get_level_policy


class ServicePolicyConfigTests(ShelterCodeFixture, unittest.TestCase):
    # Policy document retrieval
    @patch.dict(os.environ, {
        "TERMS_DOCUMENT_VERSION": "terms-demo-v1",
        "PRIVACY_DOCUMENT_VERSION": "privacy-demo-v1",
        "PRIVACY_REQUIRED_CONSENTS": "PRIVACY_COLLECTION_USE",
        "PRIVACY_OPTIONAL_CONSENTS": "",
    })
    def test_policy_documents_available(self):
        app = FastAPI()
        app.include_router(policies.router)
        client = TestClient(app)
        for kind in ("terms", "privacy"):
            resp = client.get(f"/policies/{kind}")
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()["data"]
            self.assertEqual(data["document_version"], f"{kind}-demo-v1")
            self.assertTrue(data["effective_at"].endswith("+00:00"))
            self.assertIn("[시연용]", data["content"])

    def test_shelter_code_issue_and_verify(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.setup_code_api(engine)
        req_id = self.seed_request(status="APPROVED")
        issue = self.issue(req_id)
        self.assertEqual(issue.status_code, 201, issue.text)
        code = issue.json()["data"]["shelter_code"]
        verify = self.verify(req_id, code)
        self.assertEqual(verify.status_code, 200, verify.text)
        start = self.start(req_id, code=False)
        self.assertEqual(start.status_code, 201, start.text)
        self.assertEqual((start.json()["walk_request_id"], start.json()["status"]), (req_id, "IN_PROGRESS"))

    # Volunteer level policy load
    @patch.dict(os.environ, {
        "VOLUNTEER_LEVEL_POLICY_VERSION": "level-policy-v1",
        "VOLUNTEER_LEVEL_POLICY_START_AT": "2026-11-26T00:00:00Z",
    })
    def test_volunteer_level_policy(self):
        policy = get_level_policy()
        self.assertIsNotNone(policy)
        self.assertEqual(policy.version, "level-policy-v1")
        self.assertTrue(policy.starts_at.isoformat().endswith("+00:00"))

    def test_volunteer_level_policy_is_disabled_when_unconfigured(self):
        with patch.dict(os.environ, {
            "VOLUNTEER_LEVEL_POLICY_VERSION": "",
            "VOLUNTEER_LEVEL_POLICY_START_AT": "",
        }):
            self.assertIsNone(get_level_policy())
