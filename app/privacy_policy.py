"""Sign-up privacy confirmation (Issue #31): request parsing and the server policy.

Confirmed contract (Notion sign-up spec, Issue #31):
- `privacy` object is required on POST /users/.
- `privacy.document_version` must equal the server's current document version.
- `privacy.age_14_plus_confirmed` must be true. It is a self-declaration, stored
  as `users.age_14_plus_confirmed_at`; it is not an age verification.
- Consent fields map to `user_consents.consent_type`. Only items the server has
  activated count, and only items the user actually agreed to are recorded.

Not decided yet, so read from server settings instead of being hard-coded:
- the current document version string,
- which consent items are active, and which of them are required.
There is no default. If the settings are missing or invalid, sign-up fails closed
(500, nothing stored) instead of guessing a policy or treating a missing answer as
consent.

Settings:
  PRIVACY_DOCUMENT_VERSION   e.g. privacy-v2 (at most 32 characters)
  PRIVACY_REQUIRED_CONSENTS  comma-separated consent types; must be set, may be empty
  PRIVACY_OPTIONAL_CONSENTS  comma-separated consent types; unset means none
"""
from dataclasses import dataclass
import os

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api_contract import ContractError

# consent_type stored in user_consents -> request field inside `privacy`.
CONSENT_FIELDS = {
    "PRIVACY_COLLECTION_USE": "collection_use_agreed",
    "AI_OVERSEAS_TRANSFER": "ai_overseas_transfer_agreed",
}
CONSENT_ACTION_AGREE = "AGREE"
DOCUMENT_VERSION_MAX_LENGTH = 32

INVALID_PRIVACY_PAYLOAD_MESSAGE = "개인정보 확인 정보를 다시 확인해 주세요."
PRIVACY_CONFIRMATION_REQUIRED_MESSAGE = "개인정보 처리 관련 필수 항목을 확인해 주세요."
AGE_CONFIRMATION_REQUIRED_MESSAGE = "연령 확인 항목을 확인해 주세요."
POLICY_VERSION_MISMATCH_MESSAGE = "개인정보 처리방침을 다시 확인해 주세요."


class PrivacyPolicyNotConfigured(RuntimeError):
    """The server has no usable privacy policy settings; sign-up must not proceed."""


class PrivacyDocumentVersionNotConfigured(PrivacyPolicyNotConfigured):
    """The server cannot select an active privacy document version."""


@dataclass(frozen=True)
class PrivacyPolicy:
    document_version: str
    required_consents: frozenset[str]
    optional_consents: frozenset[str]

    @property
    def active_consents(self) -> frozenset[str]:
        return self.required_consents | self.optional_consents


class PrivacyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_version: str = Field(min_length=1, max_length=DOCUMENT_VERSION_MAX_LENGTH)
    age_14_plus_confirmed: bool
    collection_use_agreed: bool = False
    ai_overseas_transfer_agreed: bool = False

    def agreed(self, consent_type: str) -> bool:
        return getattr(self, CONSENT_FIELDS[consent_type]) is True


def _consent_list(name: str, raw: str) -> frozenset[str]:
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())
    unknown = values - CONSENT_FIELDS.keys()
    if unknown:
        raise PrivacyPolicyNotConfigured(f"{name} has unknown consent types")
    return values


def current_privacy_policy() -> PrivacyPolicy:
    """Read the active policy from settings on every call; raise if it is not usable."""
    version = (os.getenv("PRIVACY_DOCUMENT_VERSION") or "").strip()
    required_raw = os.getenv("PRIVACY_REQUIRED_CONSENTS")
    if not version or len(version) > DOCUMENT_VERSION_MAX_LENGTH:
        raise PrivacyDocumentVersionNotConfigured("The privacy document version is missing or invalid")
    if required_raw is None:
        raise PrivacyPolicyNotConfigured("PRIVACY_REQUIRED_CONSENTS must be set")
    required = _consent_list("PRIVACY_REQUIRED_CONSENTS", required_raw)
    optional = _consent_list("PRIVACY_OPTIONAL_CONSENTS", os.getenv("PRIVACY_OPTIONAL_CONSENTS") or "")
    if required & optional:
        raise PrivacyPolicyNotConfigured("a consent type cannot be both required and optional")
    return PrivacyPolicy(version, required, optional)


def invalid_payload() -> ContractError:
    return ContractError(422, "INVALID_PRIVACY_PAYLOAD", INVALID_PRIVACY_PAYLOAD_MESSAGE, {"field": "privacy"})


def parse_privacy(value: object) -> PrivacyInput:
    """Validate with the same schema used by OpenAPI, then check the age declaration."""
    if not isinstance(value, dict):
        raise invalid_payload()
    try:
        # Omitted and false age declarations use the same contract error.
        privacy = PrivacyInput.model_validate({"age_14_plus_confirmed": False, **value})
    except ValidationError:
        raise invalid_payload() from None
    if not privacy.document_version.strip():
        raise invalid_payload()
    if not privacy.age_14_plus_confirmed:
        raise ContractError(422, "AGE_CONFIRMATION_REQUIRED", AGE_CONFIRMATION_REQUIRED_MESSAGE,
                            {"field": "privacy.age_14_plus_confirmed"})
    return privacy


def check_document_version(submitted: str, active: str, field: str) -> None:
    if submitted != active:
        raise ContractError(409, "POLICY_VERSION_MISMATCH", POLICY_VERSION_MISMATCH_MESSAGE, {"field": field})


def check_against_policy(privacy: PrivacyInput, policy: PrivacyPolicy) -> list[str]:
    """Return the consent types to record. 422 for a missing required consent, then 409 for the version."""
    if any(not privacy.agreed(consent_type) for consent_type in policy.required_consents):
        raise ContractError(422, "PRIVACY_CONFIRMATION_REQUIRED", PRIVACY_CONFIRMATION_REQUIRED_MESSAGE,
                            {"field": "privacy"})
    check_document_version(privacy.document_version, policy.document_version, "privacy.document_version")
    # Active items the user explicitly agreed to. Inactive items are never recorded,
    # even if the client sent true for them.
    return sorted(consent_type for consent_type in policy.active_consents if privacy.agreed(consent_type))
