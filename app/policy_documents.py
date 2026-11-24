"""Read approved active policy documents from the server's document directory."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path, PurePosixPath

from app.api_contract import ContractError
from app.privacy_policy import (
    DOCUMENT_VERSION_MAX_LENGTH,
    PrivacyDocumentVersionNotConfigured,
    PrivacyPolicy,
    check_document_version as check_document_version,
    current_privacy_policy,
)


DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / "policies"
POLICY_KINDS = ("terms", "privacy")
logger = logging.getLogger(__name__)


class PolicyDocumentUnavailable(ContractError):
    def __init__(self, kind: str, reason: str):
        logger.warning("Policy document unavailable: kind=%s reason=%s", kind, reason)
        super().__init__(503, "POLICY_DOCUMENT_UNAVAILABLE", "정책 문서를 불러올 수 없습니다.")


@dataclass(frozen=True)
class PolicyDocument:
    kind: str
    document_version: str
    title: str
    effective_at: datetime
    content: str
    sha256: str


def _configured_privacy_policy() -> PrivacyPolicy:
    try:
        return current_privacy_policy()
    except PrivacyDocumentVersionNotConfigured:
        raise PolicyDocumentUnavailable("privacy", "invalid active version") from None


def _manifest_entry(directory: Path, kind: str, version: str) -> dict:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raise PolicyDocumentUnavailable(kind, "unreadable manifest") from None
    entries = manifest.get("documents") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise PolicyDocumentUnavailable(kind, "invalid manifest")
    matching = [entry for entry in entries if entry.get("kind") == kind and entry.get("document_version") == version]
    if len(matching) != 1:
        raise PolicyDocumentUnavailable(kind, "missing or duplicate active entry")
    return matching[0]


def _read_markdown(directory: Path, kind: str, file: object) -> bytes:
    if not isinstance(file, str) or "\\" in file or ":" in file:
        raise PolicyDocumentUnavailable(kind, "invalid document path")
    relative = PurePosixPath(file)
    if len(relative.parts) != 2 or relative.parts[0] != kind or relative.suffix != ".md":
        raise PolicyDocumentUnavailable(kind, "invalid document path")
    try:
        path = (directory / relative).resolve(strict=True)
        if not path.is_relative_to(directory / kind) or path.suffix != ".md":
            raise PolicyDocumentUnavailable(kind, "document path outside kind directory")
        return path.read_bytes()
    except (OSError, ValueError, RuntimeError):
        raise PolicyDocumentUnavailable(kind, "unreadable document file") from None


def _read_document(kind: str, version: str) -> PolicyDocument:
    if not version or len(version) > DOCUMENT_VERSION_MAX_LENGTH:
        raise PolicyDocumentUnavailable(kind, "invalid active version")
    try:
        directory = Path(os.getenv("POLICY_DOCUMENTS_DIR") or DEFAULT_DIRECTORY).resolve()
    except (OSError, ValueError, RuntimeError):
        raise PolicyDocumentUnavailable(kind, "invalid document directory") from None
    entry = _manifest_entry(directory, kind, version)
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PolicyDocumentUnavailable(kind, "invalid title")
    try:
        effective_at = datetime.fromisoformat(entry.get("effective_at"))
        if effective_at.utcoffset() is None:
            raise ValueError("Missing UTC offset")
        effective_at = effective_at.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise PolicyDocumentUnavailable(kind, "invalid effective timestamp") from None
    body = _read_markdown(directory, kind, entry.get("file"))
    checksum = sha256(body).hexdigest()
    if checksum != entry.get("sha256"):
        raise PolicyDocumentUnavailable(kind, "checksum mismatch")
    try:
        content = body.decode("utf-8")
    except UnicodeDecodeError:
        raise PolicyDocumentUnavailable(kind, "invalid document encoding") from None
    if not content.strip():
        raise PolicyDocumentUnavailable(kind, "empty document")
    return PolicyDocument(kind, version, title, effective_at, content, checksum)


def active_policy_document(kind: str) -> PolicyDocument:
    """Resolve one public policy document from the active server configuration."""
    if kind not in POLICY_KINDS:
        raise ContractError(422, "INVALID_POLICY_KIND", "정책 문서 종류를 확인해 주세요.", {"field": "kind"})
    version = (_configured_privacy_policy().document_version if kind == "privacy"
               else (os.getenv("TERMS_DOCUMENT_VERSION") or "").strip())
    return _read_document(kind, version)


def active_signup_privacy_policy() -> PrivacyPolicy:
    """Use the same privacy version and validate its document before signup writes."""
    policy = _configured_privacy_policy()
    _read_document("privacy", policy.document_version)
    return policy
