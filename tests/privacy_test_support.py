"""Test-only privacy policy settings for sign-up tests (Issue #31).

These values exist only so tests can exercise the mechanism. They are not the
service policy: the real document version and the active and required consent
items are not decided yet and come from the deployment settings.
"""
import os
from pathlib import Path
from unittest.mock import patch

TEST_DOCUMENT_VERSION = "privacy-test-v1"
TEST_TERMS_VERSION = "terms-test-v1"
TEST_POLICY_DIRECTORY = Path(__file__).resolve().parent / "fixtures" / "policies"
TEST_POLICY_ENV = {
    "PRIVACY_DOCUMENT_VERSION": TEST_DOCUMENT_VERSION,
    "PRIVACY_REQUIRED_CONSENTS": "PRIVACY_COLLECTION_USE",
    "PRIVACY_OPTIONAL_CONSENTS": "AI_OVERSEAS_TRANSFER",
    "TERMS_DOCUMENT_VERSION": TEST_TERMS_VERSION,
    "POLICY_DOCUMENTS_DIR": str(TEST_POLICY_DIRECTORY),
}


def privacy(**changes):
    """A `privacy` object that satisfies TEST_POLICY_ENV; keyword arguments override fields."""
    return {"document_version": TEST_DOCUMENT_VERSION, "collection_use_agreed": True,
            "age_14_plus_confirmed": True, **changes}


def onboarding_agreements(*, terms_version=TEST_TERMS_VERSION, privacy_version=TEST_DOCUMENT_VERSION):
    """The two explicitly accepted synthetic documents in a walking routine request."""
    return {"terms": {"document_version": terms_version, "agreed": True},
            "privacy": {"document_version": privacy_version, "agreed": True}}


def use_test_privacy_policy(test_case, **overrides):
    """Apply TEST_POLICY_ENV (plus overrides) for one test; restored by addCleanup."""
    patcher = patch.dict(os.environ, {**TEST_POLICY_ENV, **overrides})
    patcher.start()
    test_case.addCleanup(patcher.stop)
