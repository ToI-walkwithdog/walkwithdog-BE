"""SMS providers for phone verification (Issue #40).

Settings (names only; values never go in code, logs or the repository):
  SMS_PROVIDER         mock | solapi. Unset means no provider: sending fails with 503.
  APP_ENV              local | development | test | production. The mock provider is
                       refused unless APP_ENV is local, development or test, so a
                       production server can never report a mock "send" as delivered.
  SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER_NUMBER   for SMS_PROVIDER=solapi

Result meanings:
- ACCEPTED: the provider accepted the message. It does not mean the phone received it.
- FAILED: the provider reported a definite failure.
- SmsProviderTimeout or any other exception: the outcome is unknown (UNKNOWN). The
  caller must not resend automatically.

Since Issue #110 the provider is called by the delivery job of
app.phone_verification_service (a bounded in-process pool), after the send API has
already answered, and never while a database transaction is open. A provider result
only changes the request's internal status; the API answers never depend on it.
confirm() no longer calls lookup(): a matching code is itself proof of delivery.
"""
from dataclasses import dataclass, field
import os
import threading
from typing import Protocol

MOCK_ALLOWED_ENVIRONMENTS = {"local", "development", "test"}


class SmsProviderUnavailable(RuntimeError):
    """No usable provider is configured. Nothing was sent."""


class SmsProviderTimeout(RuntimeError):
    """The provider did not answer in time; the message may or may not have been accepted."""


@dataclass(frozen=True)
class SmsSendResult:
    accepted: bool
    message_id: str | None = None
    group_id: str | None = None


class SmsProvider(Protocol):
    name: str

    def send(self, *, to_e164: str, text: str, request_key: str) -> SmsSendResult:
        """Submit one message. Raise SmsProviderTimeout when the outcome is unknown."""

    def lookup(self, *, request_key: str, message_id: str | None, group_id: str | None) -> bool | None:
        """Settle an unknown outcome: True accepted, False failed, None still unknown."""


@dataclass
class MockSmsProvider:
    """In-memory provider for local runs and tests. It never contacts a carrier.

    `outbox` keeps sent messages only in this process so tests can read the code;
    nothing is logged. `next_outcome` lets tests script accepted, failed, timeout or
    error results; `lookup_results` scripts what a later lookup reports.
    """
    name: str = "mock"
    outbox: list = field(default_factory=list)
    next_outcome: list = field(default_factory=list)
    lookup_results: dict = field(default_factory=dict)
    lookups: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, *, to_e164: str, text: str, request_key: str) -> SmsSendResult:
        with self._lock:
            outcome = self.next_outcome.pop(0) if self.next_outcome else "accepted"
            if outcome == "timeout":
                raise SmsProviderTimeout("mock timeout")
            if outcome == "error":
                raise ConnectionError("mock connection reset")
            if outcome == "failed":
                return SmsSendResult(accepted=False)
            self.outbox.append({"to": to_e164, "text": text, "request_key": request_key})
            return SmsSendResult(accepted=True, message_id=f"mock-{request_key}", group_id="mock-group")

    def lookup(self, *, request_key: str, message_id: str | None, group_id: str | None) -> bool | None:
        with self._lock:
            self.lookups.append(request_key)
            return self.lookup_results.get(request_key)


class SolapiSmsProvider:
    """SOLAPI adapter placeholder.

    The account, API key and registered sender number are not issued yet, and the send
    and lookup calls (including whether messageId and groupId can be queried) must be
    verified against the real API. Until then this refuses to send, so it can never be
    mistaken for a working provider.
    """
    name = "solapi"

    def __init__(self, api_key: str, api_secret: str, sender: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._sender = sender

    def send(self, *, to_e164: str, text: str, request_key: str) -> SmsSendResult:
        raise SmsProviderUnavailable("SOLAPI integration is not implemented yet")

    def lookup(self, *, request_key: str, message_id: str | None, group_id: str | None) -> bool | None:
        return None


_mock_singleton = MockSmsProvider()


def configured_sms_provider() -> SmsProvider:
    """Build the provider from settings, refusing unsafe or incomplete configuration."""
    choice = (os.getenv("SMS_PROVIDER") or "").strip().lower()
    if choice == "mock":
        if (os.getenv("APP_ENV") or "").strip().lower() not in MOCK_ALLOWED_ENVIRONMENTS:
            raise SmsProviderUnavailable("mock SMS provider is only allowed in local, development or test")
        return _mock_singleton
    if choice == "solapi":
        values = [os.getenv(name) or "" for name in ("SOLAPI_API_KEY", "SOLAPI_API_SECRET", "SOLAPI_SENDER_NUMBER")]
        if not all(value.strip() for value in values):
            raise SmsProviderUnavailable("SOLAPI settings are incomplete")
        return SolapiSmsProvider(*values)
    raise SmsProviderUnavailable("SMS_PROVIDER is not set")


def get_sms_provider() -> SmsProvider | None:
    """Dependency. None when no usable provider is configured; the send route answers 503."""
    try:
        return configured_sms_provider()
    except SmsProviderUnavailable:
        return None
