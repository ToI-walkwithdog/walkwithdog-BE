"""Reverse geocoding for Issue #46. Coordinates are used only for the current lookup."""

from http.client import HTTPException
import json
import os
import re
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

KAKAO_REGION_URL = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"
KAKAO_TIMEOUT_SECONDS = 3
MAX_RESPONSE_BYTES = 64 * 1024
LEGAL_DISTRICT_CODE = re.compile(r"[0-9]{10}")
MOCK_ENVIRONMENTS = {"local", "development", "test"}


class GeocodingUnavailable(Exception):
    """A provider failure with a safe category, independent of the request or response."""


class LocationGeocoder(Protocol):
    def reverse_geocode(self, latitude: float, longitude: float) -> str | None:
        """Return a legal district code or None when the point has no match."""
        ...


class MockLocationGeocoder:
    def reverse_geocode(self, latitude: float, longitude: float) -> str:
        # Deterministic fixture matching region master code 11680.
        return "1168010100"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KakaoLocationGeocoder:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def reverse_geocode(self, latitude: float, longitude: float) -> str | None:
        query = urlencode({"x": longitude, "y": latitude, "input_coord": "WGS84"})
        request = Request(f"{KAKAO_REGION_URL}?{query}",
                          headers={"Authorization": f"KakaoAK {self._api_key}"})
        # Send only to the configured Kakao hostname, even if a redirect or proxy is present.
        opener = build_opener(NoRedirect(), ProxyHandler({}))
        try:
            with opener.open(request, timeout=KAKAO_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            exc.close()
            if exc.code in (401, 403):
                raise GeocodingUnavailable("provider_auth") from None
            if exc.code == 429:
                raise GeocodingUnavailable("provider_quota") from None
            if 500 <= exc.code <= 599:
                raise GeocodingUnavailable("provider_server") from None
            raise GeocodingUnavailable("provider_http") from None
        except TimeoutError:
            raise GeocodingUnavailable("provider_timeout") from None
        except URLError as exc:
            reason = "provider_timeout" if isinstance(exc.reason, TimeoutError) else "provider_transport"
            raise GeocodingUnavailable(reason) from None
        except (OSError, HTTPException, ValueError):
            # Transport exceptions can contain the full URL or Authorization header.
            raise GeocodingUnavailable("provider_transport") from None

        try:
            if len(raw) <= MAX_RESPONSE_BYTES:
                return _legal_code(json.loads(raw))
        except (ValueError, RecursionError):
            pass
        raise GeocodingUnavailable("provider_response")


def _legal_code(payload: object) -> str | None:
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, list):
        raise GeocodingUnavailable("provider_response")
    codes: set[str] = set()
    for document in documents:
        kind = document.get("region_type") if isinstance(document, dict) else None
        if kind == "H":
            continue
        code = document.get("code") if kind == "B" else None
        if not isinstance(code, str) or LEGAL_DISTRICT_CODE.fullmatch(code) is None:
            raise GeocodingUnavailable("provider_response")
        codes.add(code)
    if len(codes) > 1:
        raise GeocodingUnavailable("provider_response")
    return next(iter(codes), None)


def get_location_geocoder() -> LocationGeocoder:
    provider = os.getenv("LOCATION_GEOCODING_PROVIDER", "").strip().lower()
    if provider == "mock" and os.getenv("APP_ENV", "").strip().lower() in MOCK_ENVIRONMENTS:
        return MockLocationGeocoder()
    if provider == "kakao":
        if os.getenv("LOCATION_EXTERNAL_TRANSFER_ENABLED", "").strip().lower() != "true":
            raise GeocodingUnavailable("transfer_disabled")
        key = os.getenv("KAKAO_REST_API_KEY", "").strip()
        if key:
            return KakaoLocationGeocoder(key)
    raise GeocodingUnavailable("provider_configuration")
