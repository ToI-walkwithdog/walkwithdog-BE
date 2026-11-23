"""Test-only privacy policy settings for sign-up tests (Issue #31).

These values exist only so tests can exercise the mechanism. They are not the
service policy: the real document version and the active and required consent
items are not decided yet and come from the deployment settings.
"""
import os
from unittest.mock import patch

TEST_DOCUMENT_VERSION = "privacy-test-v1"
TEST_POLICY_ENV = {
    "PRIVACY_DOCUMENT_VERSION": TEST_DOCUMENT_VERSION,
    "PRIVACY_REQUIRED_CONSENTS": "PRIVACY_COLLECTION_USE",
    "PRIVACY_OPTIONAL_CONSENTS": "AI_OVERSEAS_TRANSFER",
}


def privacy(**changes):
    """A `privacy` object that satisfies TEST_POLICY_ENV; keyword arguments override fields."""
    return {"document_version": TEST_DOCUMENT_VERSION, "collection_use_agreed": True,
            "age_14_plus_confirmed": True, **changes}


def use_test_privacy_policy(test_case, **overrides):
    """Apply TEST_POLICY_ENV (plus overrides) for one test; restored by addCleanup."""
    patcher = patch.dict(os.environ, {**TEST_POLICY_ENV, **overrides})
    patcher.start()
    test_case.addCleanup(patcher.stop)
