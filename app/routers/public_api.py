# public_api.py

import os
import requests
from dotenv import load_dotenv

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Dog, Shelter, BreedTrait

from app.dependencies import require_admin
from app.models import User

load_dotenv()

router = APIRouter(
    prefix="/public-api",
    tags=["public-api"]
)

PUBLIC_API_KEY = os.getenv("PUBLIC_API_KEY")

BASE_URL = "https://api.odcloud.kr/api/15077756/v1/animals"

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def generate_dog_name(item: dict) -> str:
    breed = clean_breed(item)
    desertion_no = item.get("desertionNo", "")
    if desertion_no:
        return f"{breed}-{desertion_no[-3:]}"
    return breed


def clean_breed(item: dict) -> str:
    breed = item.get("kindNm") or item.get("kindFullNm") or "품종 미상"
    return breed.replace("[개] ", "").strip()


def convert_age_to_number(age_text: str) -> int | None:
    if not age_text:
        return None
    if "60일미만" in age_text:
        return 0

    year_text = age_text[:4]
    if year_text.isdigit():
        return 2026 - int(year_text)

    return None


def convert_gender(sex_cd: str) -> str:
    return {"M": "male", "F": "female"}.get(sex_cd, "unknown")


def make_description(item: dict) -> str:
    lines = []

    if notice_no := item.get("noticeNo"):
        lines.append(f"📋 공고번호: {notice_no}")
    if desertion_no := item.get("desertionNo"):
        lines.append(f"🔖 유기번호: {desertion_no}")
    if place := item.get("happenPlace"):
        lines.append(f"📍 발견장소: {place}")

    color = item.get("colorCd", "").strip()
    weight = item.get("weight", "").strip()

    if color:
        lines.append(f"🎨 색상: {color}")
    if weight:
        lines.append(f"⚖️ 몸무게: {weight}")

    if state := item.get("processState"):
        lines.append(f"📌 현재 상태: {state}")
    if mark := item.get("specialMark"):
        lines.append(f"💬 특이사항: {mark}")

    return "\n".join(lines) if lines else "정보 없음"


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


def get_breed_trait(db: Session, breed_name: str) -> BreedTrait | None:
    return (
        db.query(BreedTrait)
        .filter(BreedTrait.breed_name == breed_name)
        .first()
    )


@router.post("/fetch-dogs")
def fetch_dogs_from_public_api(
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)):
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

    saved_count = 0
    skipped_count = 0

    for item in items:
        desertion_no = item.get("desertionNo")

        if not desertion_no:
            skipped_count += 1
            continue

        # 중복 체크: desertion_no 컬럼 기준으로 정확하게 확인
        existing_dog = (
            db.query(Dog)
            .filter(Dog.desertion_no == desertion_no)
            .first()
        )

        if existing_dog:
            skipped_count += 1
            continue

        shelter = get_or_create_shelter(db, item)

        breed_name = clean_breed(item)
        breed_trait = get_breed_trait(db, breed_name)

        dog = Dog(
            desertion_no=desertion_no,
            shelter_id=shelter.id if shelter else None,
            name=generate_dog_name(item),
            breed=breed_name,
            age=convert_age_to_number(item.get("age")),
            gender=convert_gender(item.get("sexCd", "")),
            image_url=item.get("popfile1") or item.get("popfile"),
            description=make_description(item),
            personality_keywords=item.get("specialMark"),

            # 견종 기본 성향이 있으면 사용, 없으면 기본값 3
            energy_level=breed_trait.energy_level if breed_trait else 3,
            barking_level=breed_trait.barking_level if breed_trait else 3,
            independence_level=breed_trait.independence_level if breed_trait else 3,
            sociability_level=breed_trait.sociability_level if breed_trait else 3,
            walk_need=breed_trait.walk_need if breed_trait else 3,

            good_with_children=breed_trait.good_with_children if breed_trait else None,
            good_with_other_dogs=breed_trait.good_with_other_dogs if breed_trait else None,
            separation_anxiety=breed_trait.separation_anxiety if breed_trait else None,

            trait_source="breed_default",
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