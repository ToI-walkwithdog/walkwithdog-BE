# public_api.py

import os
import requests
from dotenv import load_dotenv

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Dog, Shelter

load_dotenv()

router = APIRouter(
    prefix="/public-api",
    tags=["public-api"]
)

PUBLIC_API_KEY = os.getenv("PUBLIC_API_KEY")
BASE_URL = "http://apis.data.go.kr/1543061/abandonmentPublicService_v2/abandonmentPublic_v2"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ──────────────────────────────────────────
# 1. 이름 자동 생성: 품종 + 유기번호 뒤 3자리
# ──────────────────────────────────────────
def generate_dog_name(item: dict) -> str:
    """
    예) kindNm='말티즈', desertionNo='448567202600718'  →  '말티즈-718'
    desertionNo 없으면 품종명만 반환
    """
    breed = clean_breed(item)
    desertion_no = item.get("desertionNo", "")
    if desertion_no:
        return f"{breed}-{desertion_no[-3:]}"
    return breed


# ──────────────────────────────────────────
# 2. 품종명 정리: '[개] ' 접두사 제거
# ──────────────────────────────────────────
def clean_breed(item: dict) -> str:
    """
    예) '[개] 말티즈'  →  '말티즈'
    kindNm, kindFullNm 순으로 시도 후 '품종 미상' 반환
    """
    breed = item.get("kindNm") or item.get("kindFullNm") or "품종 미상"
    return breed.replace("[개] ", "").strip()


# ──────────────────────────────────────────
# 3. 나이 변환: 텍스트  →  숫자(만 나이)
# ──────────────────────────────────────────
def convert_age_to_number(age_text: str) -> int | None:
    """
    예) '2022(년생)'  →  4
        '60일미만'    →  0
        None / 이상한 값  →  None
    """
    if not age_text:
        return None
    if "60일미만" in age_text:
        return 0
    # '2022(년생)' 형식에서 앞 4자리 추출
    year_text = age_text[:4]
    if year_text.isdigit():
        return 2026 - int(year_text)
    return None


# ──────────────────────────────────────────
# 4. 성별 변환: 코드  →  영문 소문자
# ──────────────────────────────────────────
def convert_gender(sex_cd: str) -> str:
    """
    'M'  →  'male'
    'F'  →  'female'
    그 외  →  'unknown'
    """
    return {"M": "male", "F": "female"}.get(sex_cd, "unknown")


# ──────────────────────────────────────────
# 5. description 문장 생성
# ──────────────────────────────────────────
def make_description(item: dict) -> str:
    """
    여러 API 필드를 읽기 좋은 한 문단으로 조합
    """
    lines = []

    # 공고·유기 번호
    if notice_no := item.get("noticeNo"):
        lines.append(f"📋 공고번호: {notice_no}")
    if desertion_no := item.get("desertionNo"):
        lines.append(f"🔖 유기번호: {desertion_no}")

    # 발견 정보
    if place := item.get("happenPlace"):
        lines.append(f"📍 발견장소: {place}")

    # 외형
    color  = item.get("colorCd",  "").strip()
    weight = item.get("weight",   "").strip()
    if color:
        lines.append(f"🎨 색상: {color}")
    if weight:
        lines.append(f"⚖️  몸무게: {weight}")

    # 상태 및 특이사항
    if state := item.get("processState"):
        lines.append(f"📌 현재 상태: {state}")
    if mark := item.get("specialMark"):
        lines.append(f"💬 특이사항: {mark}")

    return "\n".join(lines) if lines else "정보 없음"


# ──────────────────────────────────────────
# 보호소 조회 또는 생성 (기존 유지)
# ──────────────────────────────────────────
def get_or_create_shelter(db: Session, item: dict) -> Shelter | None:
    care_name = item.get("careNm")
    if not care_name:
        return None

    shelter = db.query(Shelter).filter(Shelter.name == care_name).first()
    if shelter:
        return shelter

    shelter = Shelter(
        name=care_name,
        phone=item.get("careTel"),
        address=item.get("careAddr"),
        website_url=None,
    )
    db.add(shelter)
    db.commit()
    db.refresh(shelter)
    return shelter


# ──────────────────────────────────────────
# 엔드포인트: 공공 API → DB 저장
# ──────────────────────────────────────────
@router.post("/fetch-dogs")
def fetch_dogs_from_public_api(db: Session = Depends(get_db)):
    if not PUBLIC_API_KEY:
        raise HTTPException(status_code=500, detail="PUBLIC_API_KEY가 .env에 없습니다.")

    params = {
        "serviceKey": PUBLIC_API_KEY,
        "pageNo": 1,
        "numOfRows": 20,
        "upkind": "417000",
        "_type": "json",
    }

    response = requests.get(BASE_URL, params=params, timeout=10)
    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "공공API 호출 실패",
                "status_code": response.status_code,
                "response_text": response.text[:1000],
            },
        )

    data = response.json()
    header = data["response"]["header"]
    if header["resultCode"] != "00":
        raise HTTPException(
            status_code=500,
            detail={
                "message": "공공API 응답 오류",
                "result_code": header["resultCode"],
                "result_msg": header["resultMsg"],
            },
        )

    items = data["response"]["body"]["items"].get("item", [])
    if isinstance(items, dict):
        items = [items]

    saved_count = skipped_count = 0

    for item in items:
        desertion_no = item.get("desertionNo")
        if not desertion_no:
            skipped_count += 1
            continue

        # 중복 체크
        if db.query(Dog).filter(Dog.description.contains(desertion_no)).first():
            skipped_count += 1
            continue

        shelter = get_or_create_shelter(db, item)

        dog = Dog(
            desertion_no=desertion_no,
            shelter_id=shelter.id if shelter else None,
            name=generate_dog_name(item),           # 예) 말티즈-718
            breed=clean_breed(item),                # 예) 말티즈
            age=convert_age_to_number(item.get("age")),   # 예) 4
            gender=convert_gender(item.get("sexCd", "")), # 예) female
            image_url=item.get("popfile1") or item.get("popfile"),
            description=make_description(item),     # 이쁜 문장
            personality_keywords=item.get("specialMark"),

            energy_level=3,
            barking_level=3,
            independence_level=3,
            sociability_level=3,
            walk_need=3,

            good_with_children=None,
            good_with_other_dogs=None,
            separation_anxiety=None,
        )

        db.add(dog)
        saved_count += 1

    db.commit()

    return {
        "message": "공공API 데이터 저장 완료",
        "saved_count": saved_count,
        "skipped_count": skipped_count,
    }


@router.get("/test")
def test_public_api():
    if not PUBLIC_API_KEY:
        raise HTTPException(status_code=500, detail="PUBLIC_API_KEY가 .env에 없습니다.")

    params = {
        "serviceKey": PUBLIC_API_KEY,
        "pageNo": 1,
        "numOfRows": 5,
        "upkind": "417000",
        "_type": "json",
    }

    response = requests.get(BASE_URL, params=params, timeout=10)
    return {
        "status_code": response.status_code,
        "url": response.url,
        "text": response.text[:2000],
    }