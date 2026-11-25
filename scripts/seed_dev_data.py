"""Seed marked dev/test fixtures and report shelter-to-region candidates (Issue #116).

Preview by default: computes the plan against the database and rolls back.
--apply commits everything in one transaction and is idempotent on re-runs.
Refuses to run when APP_ENV=production. Passwords are never printed or logged.
"""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
import re
import secrets
import sys

from sqlalchemy import func, text

from app.account_status import ACCOUNT_ACTIVE, ACCOUNT_AGE_REVIEW_REQUIRED
from app.auth import hash_password
from app.dog_walk_settings import apply_walk_settings
from app.models import (
    Dog,
    DogWalkDateSlot,
    Shelter,
    User,
    UserConsent,
    UserPledgeAcknowledgement,
    UserRole,
    VolunteerExperienceChallenge,
    VolunteerOnboardingBasic,
    VolunteerOnboardingExperience,
    VolunteerOnboardingRoutine,
    VolunteerRoutineRegion,
    VolunteerRoutineTimeSlot,
)
from app.policy_documents import active_policy_document
from app.privacy_policy import CONSENT_ACTION_AGREE
from app.pledge_service import current_pledge_version
from app.region_models import Region, SELECTABLE_LEVELS
from app.walk_schemas import WalkSettingsPatch

KST = timezone(timedelta(hours=9))
PRODUCTION_ENVS = {"production", "prod"}
ADVISORY_LOCK_KEY = 724931116


@contextmanager
def _seed_policy_environment():
    current_versions = (
        "PLEDGE_CURRENT_VERSION",
        "TERMS_CURRENT_VERSION",
        "PRIVACY_CURRENT_VERSION",
    )
    document_versions = ("TERMS_DOCUMENT_VERSION", "PRIVACY_DOCUMENT_VERSION")
    environment_keys = (
        *current_versions,
        *document_versions,
        "PRIVACY_REQUIRED_CONSENTS",
    )
    original = {key: os.environ.get(key) for key in environment_keys}
    version_settings = (
        ("PLEDGE_CURRENT_VERSION", None, "pledge-demo-v1"),
        ("TERMS_CURRENT_VERSION", "TERMS_DOCUMENT_VERSION", "terms-demo-v1"),
        ("PRIVACY_CURRENT_VERSION", "PRIVACY_DOCUMENT_VERSION", "privacy-demo-v1"),
    )
    for current_key, document_key, fallback in version_settings:
        configured = (
            (os.getenv(document_key) or "").strip() if document_key else ""
        ) or (os.getenv(current_key) or "").strip() or fallback
        os.environ[current_key] = configured
        if document_key:
            os.environ[document_key] = configured
    if os.getenv("PRIVACY_REQUIRED_CONSENTS") is None:
        os.environ["PRIVACY_REQUIRED_CONSENTS"] = ""

    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


PROVINCE_ALIASES = {
    "서울": "서울특별시",
    "서울시": "서울특별시",
    "서울특별시": "서울특별시",
    "부산": "부산광역시",
    "부산시": "부산광역시",
    "부산광역시": "부산광역시",
    "대구": "대구광역시",
    "대구시": "대구광역시",
    "대구광역시": "대구광역시",
    "인천": "인천광역시",
    "인천시": "인천광역시",
    "인천광역시": "인천광역시",
    "광주": "광주광역시",
    "광주시": "광주광역시",
    "광주광역시": "광주광역시",
    "대전": "대전광역시",
    "대전시": "대전광역시",
    "대전광역시": "대전광역시",
    "울산": "울산광역시",
    "울산시": "울산광역시",
    "울산광역시": "울산광역시",
    "세종": "세종특별자치시",
    "세종시": "세종특별자치시",
    "세종특별자치시": "세종특별자치시",
    "경기": "경기도",
    "경기도": "경기도",
    "강원": "강원특별자치도",
    "강원도": "강원특별자치도",
    "강원특별자치도": "강원특별자치도",
    "충북": "충청북도",
    "충청북도": "충청북도",
    "충남": "충청남도",
    "충청남도": "충청남도",
    "전북": "전북특별자치도",
    "전라북도": "전북특별자치도",
    "전북특별자치도": "전북특별자치도",
    "전남": "전라남도",
    "전라남도": "전라남도",
    "경북": "경상북도",
    "경상북도": "경상북도",
    "경남": "경상남도",
    "경상남도": "경상남도",
    "제주": "제주특별자치도",
    "제주도": "제주특별자치도",
    "제주특별자치도": "제주특별자치도",
}

VIRTUAL_SHELTERS = (
    {
        "key": "TEST-SHELTER-01",
        "name": "[TEST] 서울 강남 시연 보호소",
        "phone": "02-0000-9001",
        "address": "서울특별시 강남구 테헤란로 100 (시연용 가상 보호소)",
        "preferred_region_code": "11680",
    },
    {
        "key": "TEST-SHELTER-02",
        "name": "[TEST] 경기 고양 시연 보호소",
        "phone": "031-0000-9002",
        "address": "경기도 고양시 일산동구 중앙로 1200 (시연용 가상 보호소)",
        "preferred_region_code": "41285",
    },
)

VIRTUAL_DOGS = (
    {
        "key": "TEST-DOG-01",
        "shelter_key": "TEST-SHELTER-01",
        "name": "[TEST] 별이",
        "breed": "푸들(시연용)",
        "age": 3,
        "age_months": 36,
        "gender": "F",
        "weight": "4.8(Kg)",
        "description": "시연용 가상 강아지입니다. 온순하고 초보자도 쉽게 산책할 수 있습니다.",
        "personality_keywords": "온순함,사람좋아함,산책매너좋음",
        "traits": (2, 2, 5, 4, 3),
        "walk_setting": {
            "walkable": True,
            "difficulty": "BEGINNER",
            "precautions": "하네스 착용 상태를 출발 전 확인해 주세요.",
            "recommended_duration_minutes": 30,
            "size_code": "SMALL",
            "difficulty_score": Decimal("1.5"),
            "max_walk_minutes": 60,
        },
        "slot_windows": (
            (1, time(10, 0), time(10, 30), 30),
            (2, time(14, 0), time(15, 0), 60),
        ),
    },
    {
        "key": "TEST-DOG-02",
        "shelter_key": "TEST-SHELTER-01",
        "name": "[TEST] 보리",
        "breed": "진도믹스(시연용)",
        "age": 4,
        "age_months": 48,
        "gender": "M",
        "weight": "14.5(Kg)",
        "description": "시연용 가상 강아지입니다. 호기심이 많고 적당한 활동량을 선호합니다.",
        "personality_keywords": "활발함,탐색좋아함,영리함",
        "traits": (4, 2, 4, 3, 4),
        "walk_setting": {
            "walkable": True,
            "difficulty": "INTERMEDIATE",
            "precautions": "다른 강아지를 보면 반가워서 당길 수 있습니다.",
            "recommended_duration_minutes": 60,
            "size_code": "MEDIUM",
            "difficulty_score": Decimal("3.0"),
            "max_walk_minutes": 90,
        },
        "slot_windows": (
            (1, time(10, 0), time(11, 0), 60),
            (3, time(15, 0), time(16, 30), 90),
        ),
    },
    {
        "key": "TEST-DOG-03",
        "shelter_key": "TEST-SHELTER-02",
        "name": "[TEST] 태풍",
        "breed": "리트리버믹스(시연용)",
        "age": 5,
        "age_months": 60,
        "gender": "M",
        "weight": "26.0(Kg)",
        "description": "시연용 가상 강아지입니다. 체력이 좋아 긴 코스 산책을 즐깁니다.",
        "personality_keywords": "에너지넘침,듬직함,훈련우수",
        "traits": (5, 1, 4, 5, 4),
        "walk_setting": {
            "walkable": True,
            "difficulty": "EXPERIENCED",
            "precautions": "대형견 산책 경험이 있는 봉사자에게 권장합니다.",
            "recommended_duration_minutes": 90,
            "size_code": "LARGE",
            "difficulty_score": Decimal("4.0"),
            "max_walk_minutes": 120,
        },
        "slot_windows": (
            (2, time(9, 30), time(11, 0), 90),
            (3, time(16, 0), time(16, 30), 30),
        ),
    },
)

SEED_ACCOUNTS = (
    {
        "login_id": "test_admin_seed",
        "nickname": "[TEST] 운영관리자",
        "role": UserRole.ADMIN,
        "account_status": ACCOUNT_ACTIVE,
        "phone": "010-0000-9001",
        "shelter_key": None,
        "profile_state": "ADMIN",
    },
    {
        "login_id": "test_shelter_seed",
        "nickname": "[TEST] 강남보호소담당자",
        "role": UserRole.SHELTER,
        "account_status": ACCOUNT_ACTIVE,
        "phone": "010-0000-9002",
        "shelter_key": "TEST-SHELTER-01",
        "profile_state": "SHELTER",
    },
    {
        "login_id": "test_user_completed",
        "nickname": "[TEST] 완료봉사자",
        "role": UserRole.USER,
        "account_status": ACCOUNT_ACTIVE,
        "phone": "010-0000-9003",
        "shelter_key": None,
        "profile_state": "ONBOARDING_COMPLETED",
    },
    {
        "login_id": "test_user_onboarding",
        "nickname": "[TEST] 미완료봉사자",
        "role": UserRole.USER,
        "account_status": ACCOUNT_ACTIVE,
        "phone": "010-0000-9004",
        "shelter_key": None,
        "profile_state": "ONBOARDING_INCOMPLETE",
    },
    {
        "login_id": "test_user_age_review",
        "nickname": "[TEST] 연령확인대기",
        "role": UserRole.USER,
        "account_status": ACCOUNT_AGE_REVIEW_REQUIRED,
        "phone": "010-0000-9005",
        "shelter_key": None,
        "profile_state": "AGE_REVIEW_REQUIRED",
    },
)


def ensure_non_production_env() -> str:
    app_env = (os.getenv("APP_ENV") or "local").strip().lower()
    if app_env in PRODUCTION_ENVS:
        raise RuntimeError("APP_ENV=production 환경에서는 가상 시드 데이터를 생성할 수 없습니다.")
    return app_env


def resolve_seed_password(password_env: str | None = None) -> str:
    if password_env:
        value = (os.getenv(password_env) or "").strip()
        if len(value) < 8:
            raise ValueError(f"{password_env} 환경변수에 8자 이상의 시드 비밀번호를 설정해 주세요.")
        return value
    return secrets.token_urlsafe(24)


def _normalize_province_token(token: str) -> str | None:
    cleaned = re.sub(r"[^가-힣]", "", token or "")
    return PROVINCE_ALIASES.get(cleaned)


def match_shelter_regions(db) -> dict:
    """Match shelters with missing region_code against active region master rows (read-only report)."""
    active_regions = db.query(Region).filter(Region.is_active.is_(True)).all()
    provinces = {r.region_code: r.region_name for r in active_regions if r.region_level == 1}
    selectable = [
        r for r in active_regions
        if r.region_level in SELECTABLE_LEVELS
    ]

    shelters = (
        db.query(Shelter)
        .filter(Shelter.region_code.is_(None))
        .order_by(Shelter.id)
        .all()
    )

    matched = []
    ambiguous = []
    unmatched = []

    for shelter in shelters:
        addr = (shelter.address or "").strip()
        tokens = [t for t in re.split(r"[\s,()]+", addr) if t]
        province_name = _normalize_province_token(tokens[0]) if tokens else None

        candidates = []
        for region in selectable:
            parent_name = provinces.get(region.parent_region_code or "")
            if province_name and parent_name and parent_name != province_name:
                continue
            if region.region_code == "36110":
                if province_name == "세종특별자치시" or "세종" in addr:
                    candidates.append(region)
                continue
            # Match sigungu name as a whitespace-delimited token or prefix in address
            if region.region_name in tokens or any(tok.startswith(region.region_name) for tok in tokens[1:4]):
                candidates.append(region)

        entry = {
            "shelter_id": shelter.id,
            "shelter_name": shelter.name,
            "address": shelter.address,
            "patch_endpoint": f"/shelters/{shelter.id}/region",
        }
        if len(candidates) == 1:
            c = candidates[0]
            matched.append({
                **entry,
                "candidate_region_code": c.region_code,
                "candidate_region_name": c.region_name,
                "province_name": provinces.get(c.parent_region_code or ""),
            })
        elif len(candidates) > 1:
            ambiguous.append({
                **entry,
                "candidates": [
                    {
                        "region_code": c.region_code,
                        "region_name": c.region_name,
                        "province_name": provinces.get(c.parent_region_code or ""),
                    }
                    for c in sorted(candidates, key=lambda r: r.region_code)
                ],
            })
        else:
            unmatched.append(entry)

    return {
        "evaluated_count": len(shelters),
        "matched_candidates": matched,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
    }


def _select_active_region(db, preferred_code: str) -> str | None:
    preferred = (
        db.query(Region)
        .filter(Region.region_code == preferred_code, Region.is_active.is_(True))
        .first()
    )
    if preferred is not None:
        return preferred.region_code
    fallback = (
        db.query(Region)
        .filter(Region.is_active.is_(True), Region.region_level == 2)
        .order_by(Region.region_code)
        .first()
    )
    return fallback.region_code if fallback is not None else None


def _phone_to_e164(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0"):
        return "+82" + digits[1:]
    return "+" + digits


def _ensure_completed_user_state(db, user: User, region_code: str | None, now: datetime) -> int:
    created = 0
    with _seed_policy_environment():
        terms_doc = active_policy_document("terms")
        privacy_doc = active_policy_document("privacy")
        pledge_ver = current_pledge_version()
    for consent_type, doc_ver in (
        ("TERMS_OF_SERVICE", terms_doc.document_version),
        ("PRIVACY_COLLECTION_USE", privacy_doc.document_version),
    ):
        exists = (
            db.query(UserConsent)
            .filter(
                UserConsent.user_id == user.id,
                UserConsent.consent_type == consent_type,
                UserConsent.document_version == doc_ver,
            )
            .first()
        )
        if exists is None:
            db.add(
                UserConsent(
                    user_id=user.id,
                    consent_type=consent_type,
                    document_version=doc_ver,
                    action=CONSENT_ACTION_AGREE,
                    recorded_at=now,
                )
            )
            created += 1

    pledge = (
        db.query(UserPledgeAcknowledgement)
        .filter(
            UserPledgeAcknowledgement.user_id == user.id,
            UserPledgeAcknowledgement.pledge_version == pledge_ver,
        )
        .first()
    )
    if pledge is None:
        db.add(
            UserPledgeAcknowledgement(
                user_id=user.id,
                pledge_version=pledge_ver,
                acknowledged_at=now,
            )
        )
        created += 1

    if db.get(VolunteerOnboardingBasic, user.id) is None:
        db.add(
            VolunteerOnboardingBasic(
                user_id=user.id,
                full_name="시연완료봉사자",
                birth_date=date(1995, 5, 15),
                phone_e164=_phone_to_e164(user.phone),
                phone_verified_at=now,
                completed_at=now,
                updated_at=now,
            )
        )
        created += 1

    if db.get(VolunteerOnboardingExperience, user.id) is None:
        experience = VolunteerOnboardingExperience(
            user_id=user.id,
            has_experience=True,
            experienced_breeds_text="푸들, 진도믹스",
            dog_size_code="MEDIUM",
            walking_frequency="WEEKLY_1_2",
            perceived_difficulty="EASY",
            completed_at=now,
            updated_at=now,
        )
        db.add(experience)
        db.flush()  # The challenge row references this record directly.
        db.add(
            VolunteerExperienceChallenge(
                user_id=user.id,
                challenge_code="NONE",
            )
        )
        created += 1

    if db.get(VolunteerOnboardingRoutine, user.id) is None:
        db.add(
            VolunteerOnboardingRoutine(
                user_id=user.id,
                walking_duration_code="BETWEEN_30_AND_60_MIN",
                comfortable_dog_size_code="LARGE",
                completed_at=now,
                updated_at=now,
                terms_document_version=terms_doc.document_version,
                privacy_document_version=privacy_doc.document_version,
            )
        )
        db.add(
            VolunteerRoutineTimeSlot(
                user_id=user.id,
                time_slot_code="WEEKEND_MORNING",
            )
        )
        if region_code:
            db.add(
                VolunteerRoutineRegion(
                    user_id=user.id,
                    region_code=region_code,
                )
            )
        created += 1

    return created


def seed_dev_data(
    db,
    apply: bool = False,
    base_date_kst: date | None = None,
    password_env: str | None = None,
) -> dict:
    """Create marked [TEST] shelters, dogs, walk settings, future KST slots, and role accounts."""
    app_env = ensure_non_production_env()
    if apply and db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text(f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY})"))

    now_utc = datetime.now(timezone.utc)
    today_kst = base_date_kst or datetime.now(KST).date()
    password_hash = hash_password(resolve_seed_password(password_env)) if apply else None

    planned = {
        "shelters": 0,
        "dogs": 0,
        "walk_settings": 0,
        "date_slots": 0,
        "users": 0,
        "onboarding_records": 0,
    }

    # 1. Virtual Shelters ([TEST] / TEST-*)
    shelters_by_key: dict[str, Shelter] = {}
    for spec in VIRTUAL_SHELTERS:
        shelter = db.query(Shelter).filter(Shelter.desertion_no == spec["key"]).first()
        if shelter is not None:
            if not shelter.name.startswith("[TEST]") or shelter.name != spec["name"]:
                raise ValueError(f"가상 보호소 키 {spec['key']}가 기존 다른 데이터와 충돌합니다.")
        else:
            planned["shelters"] += 1
            if apply:
                region_code = _select_active_region(db, spec["preferred_region_code"])
                shelter = Shelter(
                    desertion_no=spec["key"],
                    external_code=spec["key"],
                    name=spec["name"],
                    phone=spec["phone"],
                    address=spec["address"],
                    region_code=region_code,
                )
                db.add(shelter)
                db.flush()
        if shelter is not None:
            shelters_by_key[spec["key"]] = shelter

    # 2. Virtual Dogs, Walk Settings, and Future KST Date Slots
    dogs_by_key: dict[str, Dog] = {}
    for spec in VIRTUAL_DOGS:
        parent_shelter = shelters_by_key.get(spec["shelter_key"])
        dog = db.query(Dog).filter(Dog.desertion_no == spec["key"]).first()
        if dog is not None:
            if not dog.name.startswith("[TEST]") or dog.name != spec["name"] or dog.trait_source != "manual_demo":
                raise ValueError(f"가상 강아지 키 {spec['key']}가 기존 다른 데이터와 충돌합니다.")
        else:
            planned["dogs"] += 1
            if apply:
                assert parent_shelter is not None
                dog = Dog(
                    shelter_id=parent_shelter.id,
                    desertion_no=spec["key"],
                    name=spec["name"],
                    breed=spec["breed"],
                    age=spec["age"],
                    age_months=spec["age_months"],
                    gender=spec["gender"],
                    weight=spec["weight"],
                    description=spec["description"],
                    personality_keywords=spec["personality_keywords"],
                    trait_source="manual_demo",
                    barking_level=2,
                    walk_need=3,
                    energy_level=spec["traits"][0],
                    separation_anxiety_level=spec["traits"][1],
                    sociability_level=spec["traits"][2],
                    training_level=spec["traits"][3],
                    independence_level=spec["traits"][4],
                )
                db.add(dog)
                db.flush()
        if dog is not None:
            dogs_by_key[spec["key"]] = dog

        # Walk setting (virtual dogs only)
        if dog is None or dog.walk_setting is None:
            planned["walk_settings"] += 1
            if apply and dog is not None:
                patch = WalkSettingsPatch(**spec["walk_setting"])
                apply_walk_settings(db, dog, patch)

        # Future KST date slots (30 / 60 / 90 min)
        for day_offset, start_t, end_t, duration_min in spec["slot_windows"]:
            slot_day = today_kst + timedelta(days=day_offset)
            existing_slot = None
            if dog is not None:
                existing_slot = (
                    db.query(DogWalkDateSlot)
                    .filter(
                        DogWalkDateSlot.dog_id == dog.id,
                        DogWalkDateSlot.slot_date == slot_day,
                        DogWalkDateSlot.start_time == start_t,
                        DogWalkDateSlot.end_time == end_t,
                    )
                    .first()
                )
            if existing_slot is None:
                planned["date_slots"] += 1
                if apply and dog is not None:
                    db.add(
                        DogWalkDateSlot(
                            dog_id=dog.id,
                            slot_date=slot_day,
                            start_time=start_t,
                            end_time=end_t,
                            duration_minutes=duration_min,
                            is_active=True,
                        )
                    )
    if apply:
        db.flush()

    # 3. Role & Onboarding-State Accounts (synthetic 010-0000-900x numbers)
    users_by_login: dict[str, User] = {}
    default_region_code = _select_active_region(db, "11680")

    for spec in SEED_ACCOUNTS:
        user = db.query(User).filter(func.lower(User.login_id) == spec["login_id"]).first()
        target_shelter = shelters_by_key.get(spec["shelter_key"]) if spec["shelter_key"] else None
        if user is not None:
            if user.role != spec["role"] or not user.nickname.startswith("[TEST]"):
                raise ValueError(f"시드 계정 {spec['login_id']}가 기존 다른 계정과 충돌합니다.")
        else:
            if db.query(User).filter(func.lower(User.nickname) == func.lower(spec["nickname"])).first():
                raise ValueError(f"시드 계정 닉네임 {spec['nickname']}가 기존 계정과 충돌합니다.")
            planned["users"] += 1
            if apply:
                user = User(
                    login_id=spec["login_id"],
                    nickname=spec["nickname"],
                    password=password_hash,
                    role=spec["role"],
                    shelter_id=target_shelter.id if target_shelter is not None else None,
                    phone=spec["phone"],
                    account_status=spec["account_status"],
                    age_14_plus_confirmed_at=(
                        now_utc if spec["profile_state"] != "AGE_REVIEW_REQUIRED" else None
                    ),
                    age_review_required_at=(
                        now_utc if spec["profile_state"] == "AGE_REVIEW_REQUIRED" else None
                    ),
                    first_walk_guide_completed_at=(
                        now_utc if spec["profile_state"] == "ONBOARDING_COMPLETED" else None
                    ),
                )
                db.add(user)
                db.flush()

        if user is not None:
            users_by_login[spec["login_id"]] = user

        if spec["profile_state"] == "ONBOARDING_COMPLETED":
            if user is None:
                planned["onboarding_records"] += 6
            elif apply:
                planned["onboarding_records"] += _ensure_completed_user_state(
                    db, user, default_region_code, now_utc
                )
        elif spec["profile_state"] == "ONBOARDING_INCOMPLETE":
            with _seed_policy_environment():
                pledge_ver = current_pledge_version()
            if user is None:
                planned["onboarding_records"] += 1
            elif apply:
                pledge = (
                    db.query(UserPledgeAcknowledgement)
                    .filter(
                        UserPledgeAcknowledgement.user_id == user.id,
                        UserPledgeAcknowledgement.pledge_version == pledge_ver,
                    )
                    .first()
                )
                if pledge is None:
                    db.add(
                        UserPledgeAcknowledgement(
                            user_id=user.id,
                            pledge_version=pledge_ver,
                            acknowledged_at=now_utc,
                        )
                    )
                    planned["onboarding_records"] += 1

    if apply:
        db.flush()

    region_matching = match_shelter_regions(db)

    return {
        "applied": apply,
        "app_env": app_env,
        "base_date_kst": today_kst.isoformat(),
        "planned_creates": planned,
        "virtual_shelters": [
            {
                "key": key,
                "id": shelters_by_key[key].id if key in shelters_by_key else None,
                "region_code": shelters_by_key[key].region_code if key in shelters_by_key else None,
            }
            for key in (s["key"] for s in VIRTUAL_SHELTERS)
        ],
        "virtual_dogs": [
            {
                "key": key,
                "id": dogs_by_key[key].id if key in dogs_by_key else None,
            }
            for key in (d["key"] for d in VIRTUAL_DOGS)
        ],
        "accounts": [
            {
                "login_id": acc["login_id"],
                "role": acc["role"].value,
                "account_status": acc["account_status"],
                "profile_state": acc["profile_state"],
                "user_id": users_by_login[acc["login_id"]].id if acc["login_id"] in users_by_login else None,
            }
            for acc in SEED_ACCOUNTS
        ],
        "shelter_region_matching": region_matching,
    }


def _parse_base_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="시드 데이터를 단일 트랜잭션으로 저장 (기본: preview)")
    parser.add_argument("--base-date", type=_parse_base_date, default=None, help="미래 KST 일정 기준일 (YYYY-MM-DD)")
    parser.add_argument(
        "--password-env",
        default=None,
        help="시드 계정 초기 비밀번호를 읽을 환경변수 이름 (미지정 시 내부 난수 사용, 출력 안 함)",
    )
    args = parser.parse_args(argv)

    from app.database import SessionLocal

    try:
        ensure_non_production_env()
        with SessionLocal() as db:
            try:
                result = seed_dev_data(
                    db,
                    apply=args.apply,
                    base_date_kst=args.base_date,
                    password_env=args.password_env,
                )
                if args.apply:
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                raise
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"시드 중단: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"시드 중단: DB 연결 또는 제약조건 오류({type(exc).__name__}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
