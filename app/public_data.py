"""Official APMS v2 client and transactional synchronization, without guessed traits."""
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote

import requests
from fastapi import HTTPException
from sqlalchemy import text

from app.models import Dog, Shelter

BASE_URL = "https://apis.data.go.kr/1543061/abandonmentPublicService_v2/abandonmentPublic_v2"
LEGACY_ID = re.compile(r"유기번호\s*:\s*(\d{10,20})")


def fetch_page(page=1, rows=20):
    key = os.getenv("PUBLIC_API_KEY")
    if not key:
        raise HTTPException(503, "PUBLIC_API_KEY 설정이 필요합니다.")
    try:
        response = requests.get(BASE_URL, params={"serviceKey": unquote(key), "pageNo": page,
                               "numOfRows": rows, "upkind": "417000", "_type": "json"}, timeout=(5, 15))
        if response.status_code != 200:
            raise HTTPException(502, "공공데이터 서버가 정상 응답하지 않았습니다.")
        data = response.json()["response"]
        if str(data["header"]["resultCode"]) not in ("00", "0"):
            raise HTTPException(502, "공공데이터 서비스의 승인·호출 결과를 확인하세요.")
        body = data["body"]
        total = int(body["totalCount"])
        items = (body.get("items") or {}).get("item", [])
        if isinstance(items, dict):
            items = [items]
        if total < 0 or not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            raise ValueError("invalid items")
        return items, total
    except requests.Timeout:
        raise HTTPException(504, "공공데이터 요청 시간 초과") from None
    except (requests.RequestException, ValueError, KeyError, TypeError):
        # Never expose exception URLs, remote bodies or the service key.
        raise HTTPException(502, "공공데이터 응답 형식 또는 연결 오류") from None


def fetch_pages(max_pages=2, rows=20):
    result = []
    total = 0
    pages = 0
    for page in range(1, max_pages + 1):
        items, total = fetch_page(page, rows)
        result.extend(items)
        pages += 1
        if not items or page * rows >= total:
            break
    return result, {"pages": pages, "total_count": total, "truncated": pages * rows < total}


def age_number(value):
    if not isinstance(value, str):
        return None
    if "60일미만" in value:
        return 0
    match = re.match(r"^(\d{4})", value)
    age = datetime.now(timezone.utc).year - int(match[1]) if match else -1
    return age if 0 <= age <= 40 else None


def clean_breed(item):
    return str(item.get("kindNm") or item.get("kindFullNm") or "품종 미상").replace("[개]", "").strip()[:50]


def legacy_id(description):
    identifiers = set(LEGACY_ID.findall(description or ""))
    return next(iter(identifiers)) if len(identifiers) == 1 else None


def resolve_shelter(db, item):
    name = item.get("careNm")
    if not name:
        return None
    code = str(item["careRegNo"]) if item.get("careRegNo") else None
    shelter = db.query(Shelter).filter(Shelter.external_code == code).first() if code else None
    if shelter is None:
        candidates = db.query(Shelter).filter(Shelter.name == name, Shelter.address == item.get("careAddr")).all()
        if len(candidates) > 1:
            raise HTTPException(409, "보호소 식별 충돌: 기존 데이터를 확인하세요.")
        shelter = candidates[0] if candidates else None
        if shelter and code and shelter.external_code not in (None, code):
            raise HTTPException(409, "보호소 코드 충돌")
    if shelter is None:
        shelter = Shelter(name=str(name)[:100], external_code=code, phone=item.get("careTel"), address=item.get("careAddr"))
        db.add(shelter)
        db.flush()
    elif code and shelter.external_code is None:
        shelter.external_code = code
    return shelter


def synchronize(db, items, dry_run=True):
    # Serialize imports across processes; database uniqueness remains the final guard.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(782452)"))
    dogs = db.query(Dog).all()
    indexed = {dog.desertion_no: dog for dog in dogs if dog.desertion_no}
    legacy = {}
    for dog in dogs:
        if not dog.desertion_no and dog.trait_source != "manual_demo":
            identifier = legacy_id(dog.description)
            if identifier:
                legacy.setdefault(identifier, []).append(dog)
    unique = {}
    skipped = 0
    for item in items:
        identifier = str(item.get("desertionNo") or "")
        if not re.fullmatch(r"\d{10,20}", identifier):
            skipped += 1
            continue
        unique[identifier] = item
    counts = {"saved_count": 0, "updated_count": 0, "reused_legacy_count": 0, "skipped_count": skipped,
              "dry_run": dry_run, "duplicate_payload_count": len(items) - skipped - len(unique)}
    # Validate identities before changing anything; no guess-based backfill.
    for identifier in unique:
        matches = legacy.get(identifier, [])
        if len(matches) > 1 or (identifier in indexed and matches):
            raise HTTPException(409, "유기번호 충돌: 자동 병합하지 않습니다.")
    for identifier, item in unique.items():
        dog = indexed.get(identifier)
        if dog is None and legacy.get(identifier):
            dog = legacy[identifier][0]
            counts["reused_legacy_count"] += 1
        if dog is None:
            counts["saved_count"] += 1
        else:
            if dog.trait_source == "manual_demo":
                raise HTTPException(409, "시연 데이터 식별자 충돌")
            counts["updated_count"] += 1
        if dry_run:
            continue
        shelter = resolve_shelter(db, item)
        breed = clean_breed(item)
        if dog is None:
            # Unknown traits remain NULL, not invented neutral values.
            dog = Dog(name=f"{breed[:45]}-{identifier[-3:]}", desertion_no=identifier, trait_source="unknown")
            db.add(dog)
        dog.desertion_no = identifier
        dog.breed = breed
        dog.age = age_number(item.get("age"))
        dog.gender = {"M": "male", "F": "female"}.get(item.get("sexCd"), "unknown")
        dog.image_url = item.get("popfile1") or item.get("popfile")
        dog.weight = item.get("weight")
        if shelter:
            dog.shelter_id = shelter.id
        dog.external_data = item
        dog.process_state = item.get("processState")
        dog.neuter_status = item.get("neuterYn") if item.get("neuterYn") in ("Y", "N", "U") else None
        dog.synced_at = datetime.now(timezone.utc)
        # Preserve existing shelter descriptions, notes and all observed trait values.
        if not dog.description:
            dog.description = str(item.get("specialMark") or "정보 없음")
    db.flush()
    return counts
