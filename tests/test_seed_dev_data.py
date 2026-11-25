"""Tests for scripts/seed_dev_data.py, mocked official data sync, and virtual smoke flow (Issue #116)."""
from datetime import date, datetime, timedelta, timezone
import json
import secrets
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import token_digest, verify_password
from app.database import Base, get_db
from app.main import app
from app.models import (
    AuthSession,
    Dog,
    DogWalkDateSlot,
    DogWalkSetting,
    Shelter,
    User,
    UserConsent,
    UserPledgeAcknowledgement,
)
from app.public_data import fetch_pages, synchronize
from app.region_master import read_snapshot, sync_regions
from app.routers import dogs as dogs_router
from scripts.seed_dev_data import (
    SEED_ACCOUNTS,
    VIRTUAL_DOGS,
    VIRTUAL_SHELTERS,
    main as seed_main,
    match_shelter_regions,
    seed_dev_data,
)

REGION_ZIP = "data/regions/legal_dong_code_full_20261123.zip"


def _sqlite_session(*, foreign_keys=False):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if foreign_keys:
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session


def _issue_session_token(db, user_id: int) -> str:
    raw_token = f"tok_{user_id}_{secrets.token_hex(16)}"
    db.add(
        AuthSession(
            token_hash=token_digest(raw_token),
            user_id=user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
        )
    )
    return raw_token


def test_seed_refused_in_production_and_never_leaks_password(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TEST_SEED_PW", "SuperSecretSeed!23")
    rc = seed_main(["--password-env", "TEST_SEED_PW"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "production" in err
    assert "SuperSecretSeed!23" not in err


def test_preview_apply_idempotency_and_real_data_untouched(monkeypatch):
    _, Session = _sqlite_session(foreign_keys=True)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TEST_SEED_PW", "SeedPass!2026")
    for key in (
        "PLEDGE_CURRENT_VERSION",
        "TERMS_CURRENT_VERSION",
        "PRIVACY_CURRENT_VERSION",
        "TERMS_DOCUMENT_VERSION",
        "PRIVACY_DOCUMENT_VERSION",
        "PRIVACY_REQUIRED_CONSENTS",
    ):
        monkeypatch.delenv(key, raising=False)
    base_day = date(2026, 12, 1)

    with Session.begin() as db:
        sync_regions(db, read_snapshot(REGION_ZIP), apply=True)
        official_shelter = Shelter(
            name="공공실제보호소",
            external_code="311000020260001",
            phone="02-123-4567",
            address="서울특별시 종로구 세종대로 1",
        )
        db.add(official_shelter)
        db.flush()
        official_dog = Dog(
            shelter_id=official_shelter.id,
            desertion_no="411311202600123",
            name="공공유기견",
            breed="믹스견",
            age=2,
            gender="M",
            trait_source="unknown",
        )
        db.add(official_dog)
        db.flush()
        official_shelter_id = official_shelter.id
        official_dog_id = official_dog.id

    with Session() as db:
        preview = seed_dev_data(db, apply=False, base_date_kst=base_day, password_env="TEST_SEED_PW")
        db.rollback()
    assert preview["applied"] is False
    assert preview["planned_creates"]["shelters"] == len(VIRTUAL_SHELTERS)
    assert preview["planned_creates"]["dogs"] == len(VIRTUAL_DOGS)
    assert preview["planned_creates"]["users"] == len(SEED_ACCOUNTS)
    assert "SeedPass!2026" not in json.dumps(preview)

    with Session() as db:
        assert db.query(Shelter).filter(Shelter.desertion_no.like("TEST-%")).count() == 0

    with Session.begin() as db:
        applied = seed_dev_data(db, apply=True, base_date_kst=base_day, password_env="TEST_SEED_PW")
    assert applied["applied"] is True
    assert applied["planned_creates"]["shelters"] == len(VIRTUAL_SHELTERS)
    assert applied["planned_creates"]["dogs"] == len(VIRTUAL_DOGS)

    with Session() as db:
        completed_user = db.query(User).filter_by(login_id="test_user_completed").one()
        consents = db.query(UserConsent).filter_by(user_id=completed_user.id).all()
        assert {(row.consent_type, row.document_version) for row in consents} == {
            ("TERMS_OF_SERVICE", "terms-demo-v1"),
            ("PRIVACY_COLLECTION_USE", "privacy-demo-v1"),
        }
        onboarding_user = db.query(User).filter_by(login_id="test_user_onboarding").one()
        pledge = db.query(UserPledgeAcknowledgement).filter_by(user_id=onboarding_user.id).one()
        assert pledge.pledge_version == "pledge-demo-v1"

    with Session.begin() as db:
        rerun = seed_dev_data(db, apply=True, base_date_kst=base_day, password_env="TEST_SEED_PW")
    assert all(count == 0 for count in rerun["planned_creates"].values())

    with Session() as db:
        refreshed_shelter = db.get(Shelter, official_shelter_id)
        refreshed_dog = db.get(Dog, official_dog_id)
        assert refreshed_shelter.name == "공공실제보호소"
        assert refreshed_shelter.region_code is None
        assert refreshed_dog.trait_source == "unknown"
        assert db.query(DogWalkSetting).filter(DogWalkSetting.dog_id == official_dog_id).count() == 0
        assert db.query(DogWalkDateSlot).filter(DogWalkDateSlot.dog_id == official_dog_id).count() == 0

        virtual_dogs = db.query(Dog).filter(Dog.desertion_no.like("TEST-%")).all()
        assert len(virtual_dogs) == len(VIRTUAL_DOGS)
        for vdog in virtual_dogs:
            assert vdog.walk_setting is not None and vdog.walk_setting.walkable is True
            slots = db.query(DogWalkDateSlot).filter(DogWalkDateSlot.dog_id == vdog.id).all()
            assert len(slots) == 2
            assert all(s.slot_date > base_day and s.duration_minutes in (30, 60, 90) for s in slots)

        admin = db.query(User).filter(User.login_id == "test_admin_seed").first()
        assert admin is not None and verify_password("SeedPass!2026", admin.password)


def test_shelter_region_matching_exact_ambiguous_and_unmatched():
    _, Session = _sqlite_session()
    with Session.begin() as db:
        sync_regions(db, read_snapshot(REGION_ZIP), apply=True)
        s_exact = Shelter(name="단일일치보호소", address="서울특별시 강남구 역삼로 10")
        s_ambiguous = Shelter(name="모호보호소", address="중구 중앙대로 55")
        s_unmatched = Shelter(name="불일치보호소", address="알수없는시 존재하지않는구 99")
        db.add_all([s_exact, s_ambiguous, s_unmatched])

    with Session() as db:
        report = match_shelter_regions(db)

    assert report["evaluated_count"] == 3
    assert len(report["matched_candidates"]) == 1
    assert report["matched_candidates"][0]["shelter_name"] == "단일일치보호소"
    assert report["matched_candidates"][0]["candidate_region_code"] == "11680"
    assert len(report["ambiguous"]) == 1
    assert report["ambiguous"][0]["shelter_name"] == "모호보호소"
    assert len(report["ambiguous"][0]["candidates"]) >= 2
    assert len(report["unmatched"]) == 1
    assert report["unmatched"][0]["shelter_name"] == "불일치보호소"


def test_mocked_public_data_sync_and_conflict(monkeypatch):
    _, Session = _sqlite_session()
    monkeypatch.setenv("PUBLIC_API_KEY", "dummy-key-never-called")

    mock_payload = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": {
                "totalCount": 1,
                "items": {
                    "item": [
                        {
                            "desertionNo": "441394202600501",
                            "kindNm": "[개] 진도견",
                            "age": "2024(년생)",
                            "sexCd": "F",
                            "weight": "16(Kg)",
                            "processState": "보호중",
                            "neuterYn": "Y",
                            "careRegNo": "341394202600001",
                            "careNm": "고양시동물보호센터",
                            "careTel": "031-962-3232",
                            "careAddr": "경기도 고양시 덕양구 원흥동 1",
                        }
                    ]
                },
            },
        }
    }

    class FakeResp:
        status_code = 200

        def json(self):
            return mock_payload

    with patch("app.public_data.requests.get", return_value=FakeResp()):
        items, meta = fetch_pages(max_pages=1, rows=10)

    assert meta["total_count"] == 1
    with Session.begin() as db:
        summary = synchronize(db, items, dry_run=False)
    assert summary["saved_count"] == 1

    with Session() as db:
        dog = db.query(Dog).filter(Dog.desertion_no == "441394202600501").first()
        assert dog is not None
        assert dog.trait_source == "unknown"
        assert dog.walk_setting is None


def test_virtual_seed_end_to_end_walk_smoke(monkeypatch):
    _, Session = _sqlite_session()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SHELTER_CODE_HMAC_KEY", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("PLEDGE_CURRENT_VERSION", "pledge-demo-v1")
    monkeypatch.setenv("TERMS_CURRENT_VERSION", "terms-demo-v1")
    monkeypatch.setenv("PRIVACY_CURRENT_VERSION", "privacy-demo-v1")

    with Session.begin() as db:
        sync_regions(db, read_snapshot(REGION_ZIP), apply=True)
        seed_dev_data(db, apply=True, base_date_kst=date(2026, 12, 1))
        volunteer = db.query(User).filter(User.login_id == "test_user_completed").first()
        shelter_staff = db.query(User).filter(User.login_id == "test_shelter_seed").first()
        dog = db.query(Dog).filter(Dog.desertion_no == "TEST-DOG-01").first()
        slot = (
            db.query(DogWalkDateSlot)
            .filter(DogWalkDateSlot.dog_id == dog.id)
            .order_by(DogWalkDateSlot.slot_date, DogWalkDateSlot.start_time)
            .first()
        )
        vol_token = _issue_session_token(db, volunteer.id)
        staff_token = _issue_session_token(db, shelter_staff.id)
        dog_id = dog.id
        slot_id = slot.id
        slot_date = slot.slot_date
        slot_start = slot.start_time
        slot_end = slot.end_time

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[dogs_router.get_db] = override_get_db
    try:
        client = TestClient(app)
        vol_headers = {"Authorization": f"Bearer {vol_token}"}
        staff_headers = {"Authorization": f"Bearer {staff_token}"}

        # 1. List available dogs (only virtual dogs appear)
        avail = client.get(
            "/dogs/walk-availability",
            params={"region_codes": ["11680", "41285"]},
            headers=vol_headers,
        )
        assert avail.status_code == 200, avail.text
        items = avail.json()["data"]["items"]
        assert len(items) == len(VIRTUAL_DOGS)
        assert any(item["dog_id"] == dog_id for item in items)

        # 2. Request a walk on the seeded date slot
        kst = timezone(timedelta(hours=9))
        start_iso = datetime.combine(slot_date, slot_start, tzinfo=kst).isoformat()
        end_iso = datetime.combine(slot_date, slot_end, tzinfo=kst).isoformat()
        req_res = client.post(
            "/walk-requests",
            headers={**vol_headers, "Idempotency-Key": "11111111-1111-4111-8111-111111111111"},
            json={
                "dog_id": dog_id,
                "date_slot_id": slot_id,
                "requested_start_at": start_iso,
                "requested_end_at": end_iso,
            },
        )
        assert req_res.status_code == 201, req_res.text
        req_body = req_res.json()
        request_id = req_body["id"] if "id" in req_body else req_body["data"]["id"]

        # 3. Shelter approves the walk request
        approve_res = client.patch(
            f"/walk-requests/{request_id}",
            headers=staff_headers,
            json={"action": "APPROVE"},
        )
        assert approve_res.status_code == 200, approve_res.text

        # 4. Issue and verify shelter code within eligible window
        active_now = datetime.combine(slot_date, slot_start, tzinfo=kst).astimezone(timezone.utc)
        with patch("app.walk_request_service.utc_now", return_value=active_now), \
             patch("app.walk_service.utc_now", return_value=active_now):
            issue_res = client.post(
                f"/walk-requests/{request_id}/shelter-code",
                headers=staff_headers,
            )
            assert issue_res.status_code == 201, issue_res.text
            code_body = issue_res.json()
            raw_code = (
                code_body.get("shelter_code")
                or (code_body.get("data") or {}).get("shelter_code")
                or code_body.get("code")
            )
            assert raw_code

            verify_res = client.post(
                f"/walk-requests/{request_id}/shelter-code/verify",
                headers=vol_headers,
                json={"shelter_code": raw_code},
            )
            assert verify_res.status_code == 200, verify_res.text

            # 5. Start walk
            start_res = client.post(
                "/walks",
                headers=vol_headers,
                json={"walk_request_id": request_id},
            )
            assert start_res.status_code == 201, start_res.text
            walk_id = start_res.json()["id"]

        # 6. End walk
        with patch("app.walk_service.utc_now", return_value=active_now + timedelta(minutes=5)):
            end_res = client.patch(
                f"/walks/{walk_id}/end",
                headers=vol_headers,
                json={"status": "COMPLETED"},
            )
        assert end_res.status_code == 200, end_res.text

        # 7. Handover walk
        handover_res = client.post(
            f"/walks/{walk_id}/handover",
            headers=vol_headers,
            json={
                "handover_result": "COMPLETE",
                "dog_returned": True,
                "equipment_returned": True,
                "condition_shared": True,
            },
        )
        assert handover_res.status_code == 201, handover_res.text
    finally:
        app.dependency_overrides.clear()
