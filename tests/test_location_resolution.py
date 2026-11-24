from datetime import datetime, timedelta, timezone
import io
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from app.auth import UNUSABLE_PASSWORD, token_digest
    from app.database import Base, get_db
    from app.location_geocoder import KAKAO_TIMEOUT_SECONDS, KAKAO_REGION_URL, NoRedirect
    from app.models import AuthSession, User, UserRole
    from app.region_models import LEVEL_SIDO, LEVEL_SIGUNGU, LEVEL_SPECIAL_SELECTABLE, Region
    from app.routers.regions import router

URL = "/regions/resolve-location"
POINT = {"latitude": 37.123456, "longitude": 127.654321}
KEY = "synthetic-kakao-key"
SESSIONS = {"user": {}, "minor": {"account_status": "AGE_REVIEW_REQUIRED"},
            "shelter": {"role": UserRole.SHELTER}, "admin": {"role": UserRole.ADMIN}, "expired": {}}


@pytest.fixture
def location_app(monkeypatch):
    for key, value in {"APP_ENV": "test", "LOCATION_GEOCODING_PROVIDER": "mock",
                       "LOCATION_EXTERNAL_TRANSFER_ENABLED": "false", "KAKAO_REST_API_KEY": ""}.items():
        monkeypatch.setenv(key, value)
    opener = Mock()
    opener.open.side_effect = AssertionError("Unexpected network call")
    build_opener = Mock(return_value=opener)
    monkeypatch.setattr("app.location_geocoder.build_opener", build_opener)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    with session.begin() as db:
        for purpose, options in SESSIONS.items():
            user = User(login_id=purpose, nickname=purpose, password=UNUSABLE_PASSWORD, **options)
            db.add(user)
            db.flush()
            db.add(AuthSession(user_id=user.id, token_hash=token_digest(purpose),
                               expires_at=expires_at - timedelta(hours=2 if purpose == "expired" else 0)))
        db.add_all([
            Region(region_code="11", region_name="테스트 시도", region_level=LEVEL_SIDO, is_active=True),
            Region(region_code="11680", region_name="테스트 시군구", region_level=LEVEL_SIGUNGU,
                   parent_region_code="11", is_active=True),
            Region(region_code="11650", region_name="비활성 시군구", region_level=LEVEL_SIGUNGU,
                   parent_region_code="11", is_active=False),
            Region(region_code="36", region_name="테스트 세종", region_level=LEVEL_SIDO, is_active=True),
            Region(region_code="36_SPECIAL_ALL", region_name="테스트 세종 전체", region_level=LEVEL_SPECIAL_SELECTABLE,
                   parent_region_code="36", is_active=True),
            Region(region_code="41", region_name="비활성 시도", region_level=LEVEL_SIDO, is_active=False),
            Region(region_code="41117", region_name="테스트 하위 지역", region_level=LEVEL_SIGUNGU,
                   parent_region_code="41", is_active=True),
        ])

    @event.listens_for(engine, "before_cursor_execute")
    def assert_read_only(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT")
        assert str(POINT["latitude"]) not in repr(parameters)
        assert str(POINT["longitude"]) not in repr(parameters)

    def database():
        with session() as db:
            yield db

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = database
    try:
        with TestClient(application) as client:
            yield client, build_opener, opener.open
    finally:
        engine.dispose()


def post(client, payload=POINT, token="user"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(URL, headers=headers, json=payload)


def assert_error(response, status, code, message):
    assert response.status_code == status
    assert response.json() == {"code": code, "data": {}, "message": message}
    assert response.headers["Cache-Control"] == "no-store"


def enable_stubbed_kakao(monkeypatch):
    monkeypatch.setenv("LOCATION_GEOCODING_PROVIDER", "kakao")
    monkeypatch.setenv("LOCATION_EXTERNAL_TRANSFER_ENABLED", "true")
    monkeypatch.setenv("KAKAO_REST_API_KEY", KEY)


def test_explicit_mock_works_before_onboarding_and_only_reads_master(location_app):
    client, build_opener, _ = location_app
    response = post(client)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"code": "OK", "data": {"region": {
        "sido_code": "11", "sido_name": "테스트 시도", "region_code": "11680", "region_name": "테스트 시군구",
    }}, "message": "현재 위치에 해당하는 지역을 확인했습니다."}
    build_opener.assert_not_called()


@pytest.mark.parametrize("token", [None, "unknown", "expired"])
def test_authentication_rejects_missing_invalid_or_expired_session(location_app, token, monkeypatch):
    client, build_opener, _ = location_app
    enable_stubbed_kakao(monkeypatch)
    response = post(client, token=token)
    assert_error(response, 401, "AUTHENTICATION_REQUIRED", "로그인이 필요합니다.")
    assert response.headers["WWW-Authenticate"] == "Bearer"
    build_opener.assert_not_called()


@pytest.mark.parametrize("token", ["minor", "shelter", "admin"])
def test_active_general_user_required_before_provider_call(location_app, token, monkeypatch):
    client, build_opener, _ = location_app
    enable_stubbed_kakao(monkeypatch)
    assert_error(post(client, token=token), 403, "ONBOARDING_NOT_ALLOWED", "위치 정보를 사용할 수 없습니다.")
    build_opener.assert_not_called()


@pytest.mark.parametrize("raw_body", [
    "{}", "{", "[]", '{"latitude":37}', '{"latitude":null,"longitude":127}',
    '{"latitude":"37","longitude":127}', '{"latitude":37,"longitude":true}',
    '{"latitude":91,"longitude":127}', '{"latitude":-91,"longitude":127}',
    '{"latitude":37,"longitude":181}', '{"latitude":37,"longitude":-181}',
    '{"latitude":NaN,"longitude":127}', '{"latitude":37,"longitude":1e309}',
    pytest.param(b'{"latitude":37,"longitude":"\xff"}', id="invalid-utf8"),
    pytest.param('{"latitude":' + "9" * 5000 + ',"longitude":127}', id="oversized-json-number"),
])
def test_invalid_json_coordinates_are_rejected_without_provider_or_echo(location_app, raw_body, monkeypatch):
    client, build_opener, _ = location_app
    enable_stubbed_kakao(monkeypatch)
    response = client.post(URL, content=raw_body,
                           headers={"Authorization": "Bearer user", "Content-Type": "application/json"})
    assert_error(response, 422, "INVALID_COORDINATES", "위치 정보를 확인해 주세요.")
    build_opener.assert_not_called()


@pytest.mark.parametrize("latitude,longitude", [(-90, -180), (90, 180), (0, 0)])
def test_numeric_boundaries_are_valid(location_app, latitude, longitude):
    client, _, _ = location_app
    assert post(client, {"latitude": latitude, "longitude": longitude}).status_code == 200


@pytest.mark.parametrize("code,expected", [("1168010100", "11680"), ("3611010100", "36_SPECIAL_ALL")])
def test_kakao_uses_wgs84_and_maps_legal_code_ignoring_administrative_record(
    location_app, monkeypatch, caplog, code, expected,
):
    client, build_opener, open_request = location_app
    enable_stubbed_kakao(monkeypatch)
    open_request.side_effect = None
    open_request.return_value = io.BytesIO(json.dumps({"documents": [
        {"region_type": "H", "code": "1165010100"}, {"region_type": "B", "code": code, "region_2depth_name": "ignored"},
    ]}).encode())
    response = post(client)
    assert response.status_code == 200
    assert response.json()["data"]["region"]["region_code"] == expected
    assert response.json()["data"]["region"]["sido_code"] == expected[:2]
    request = open_request.call_args.args[0]
    parts = urlsplit(request.full_url)
    assert parts._replace(query="").geturl() == KAKAO_REGION_URL
    assert parse_qs(parts.query) == {"input_coord": ["WGS84"], "x": [str(POINT["longitude"])],
                                    "y": [str(POINT["latitude"])]}
    assert request.get_header("Authorization") == f"KakaoAK {KEY}"
    assert request.get_method() == "GET"
    assert open_request.call_args.kwargs == {"timeout": KAKAO_TIMEOUT_SECONDS}
    redirect_handler, proxy_handler = build_opener.call_args.args
    assert isinstance(redirect_handler, NoRedirect)
    assert redirect_handler.redirect_request(request, None, 302, "", {}, "https://example.invalid") is None
    assert proxy_handler.proxies == {}
    for value in (KEY, str(POINT["latitude"]), str(POINT["longitude"]), "ignored"):
        assert value not in response.text + caplog.text


@pytest.mark.parametrize("code", [None, "9999910100", "1165010100", "4111710100"])
def test_unmapped_or_inactive_region_leads_to_manual_selection(location_app, monkeypatch, code):
    client, _, open_request = location_app
    enable_stubbed_kakao(monkeypatch)
    documents = [{"region_type": "B", "code": code}] if code else []
    open_request.side_effect = None
    open_request.return_value = io.BytesIO(json.dumps({"documents": documents}).encode())
    assert_error(post(client), 422, "REGION_NOT_SUPPORTED", "현재 위치에서 지역을 선택할 수 없습니다.")


@pytest.mark.parametrize("environment,provider,enabled,key,reason", [
    ("test", "", "false", "", "provider_configuration"),
    ("production", "mock", "false", "", "provider_configuration"),
    ("", "mock", "false", "", "provider_configuration"),
    ("test", "kakao", "false", KEY, "transfer_disabled"),
    ("test", "kakao", "true", "", "provider_configuration"),
])
def test_unavailable_configuration_never_falls_back_to_mock(location_app, monkeypatch, caplog,
                                                             environment, provider, enabled, key, reason):
    client, build_opener, _ = location_app
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("LOCATION_GEOCODING_PROVIDER", provider)
    monkeypatch.setenv("LOCATION_EXTERNAL_TRANSFER_ENABLED", enabled)
    monkeypatch.setenv("KAKAO_REST_API_KEY", key)
    assert_error(post(client), 503, "GEOCODING_UNAVAILABLE", "현재 위치를 확인할 수 없습니다.")
    build_opener.assert_not_called()
    assert [record.message for record in caplog.records] == [f"Location lookup unavailable: {reason}"]


@pytest.mark.parametrize("failure,reason", [(401, "provider_auth"), (429, "provider_quota"), (502, "provider_server"),
                                            (302, "provider_http"), ("timeout", "provider_timeout"),
                                            ("wrapped_timeout", "provider_timeout"), ("dns", "provider_transport")])
def test_kakao_transport_errors_return_safe_503(location_app, monkeypatch, caplog, failure, reason):
    client, _, open_request = location_app
    enable_stubbed_kakao(monkeypatch)
    private_details = f"{POINT} {KEY} upstream body"
    if isinstance(failure, int):
        open_request.side_effect = HTTPError(f"{KAKAO_REGION_URL}?{private_details}", failure, private_details,
                                             {}, io.BytesIO(private_details.encode()))
    elif failure == "timeout":
        open_request.side_effect = TimeoutError(private_details)
    elif failure == "wrapped_timeout":
        open_request.side_effect = URLError(TimeoutError(private_details))
    else:
        open_request.side_effect = URLError(private_details)
    response = post(client)
    assert_error(response, 503, "GEOCODING_UNAVAILABLE", "현재 위치를 확인할 수 없습니다.")
    assert [record.message for record in caplog.records] == [f"Location lookup unavailable: {reason}"]


@pytest.mark.parametrize("body", [
    b'{"private": "synthetic upstream body"', b'{"documents":{}}',
    b'{"documents":[{"region_type":"B","code":"private-detail"}]}',
    b'{"documents":[{"region_type":"B","code":"1168010100"},{"region_type":"B","code":"3611010100"}]}',
])
def test_invalid_provider_payload_is_unavailable(location_app, monkeypatch, caplog, body):
    client, _, open_request = location_app
    enable_stubbed_kakao(monkeypatch)
    open_request.side_effect = None
    open_request.return_value = io.BytesIO(body)
    assert_error(post(client), 503, "GEOCODING_UNAVAILABLE", "현재 위치를 확인할 수 없습니다.")
    assert [record.message for record in caplog.records] == ["Location lookup unavailable: provider_response"]


def test_unexpected_region_error_uses_safe_contract_500(location_app, monkeypatch, caplog):
    client, _, _ = location_app
    monkeypatch.setattr("app.region_service.require_selectable_region",
                        Mock(side_effect=RuntimeError(f"{POINT} {KEY}")))
    assert_error(post(client), 500, "INTERNAL_SERVER_ERROR", "일시적인 오류가 발생했습니다.")
    assert [record.message for record in caplog.records] == ["POST /regions/resolve-location failed: RuntimeError"]
