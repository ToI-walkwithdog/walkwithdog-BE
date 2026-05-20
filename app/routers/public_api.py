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


def convert_age_to_number(age_text):
    if not age_text:
        return None

    if "60일미만" in age_text:
        return 0

    year_text = age_text[:4]

    if year_text.isdigit():
        birth_year = int(year_text)
        return 2026 - birth_year

    return None


def convert_gender(sex_cd):
    if sex_cd == "M":
        return "male"
    if sex_cd == "F":
        return "female"
    return "unknown"


def get_or_create_shelter(db: Session, item: dict):
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
        website_url=None
    )

    db.add(shelter)
    db.commit()
    db.refresh(shelter)

    return shelter


@router.post("/fetch-dogs")
def fetch_dogs_from_public_api(db: Session = Depends(get_db)):
    if not PUBLIC_API_KEY:
        raise HTTPException(status_code=500, detail="PUBLIC_API_KEY가 .env에 없습니다.")

    params = {
        "serviceKey": PUBLIC_API_KEY,
        "pageNo": 1,
        "numOfRows": 20,
        "upkind": "417000",
        "_type": "json"
    }

    response = requests.get(BASE_URL, params=params, timeout=10)

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "공공API 호출 실패",
                "status_code": response.status_code,
                "response_text": response.text[:1000]
            }
        )

    data = response.json()

    result_code = data["response"]["header"]["resultCode"]
    result_msg = data["response"]["header"]["resultMsg"]

    if result_code != "00":
        raise HTTPException(
            status_code=500,
            detail={
                "message": "공공API 응답 오류",
                "result_code": result_code,
                "result_msg": result_msg
            }
        )

    items = data["response"]["body"]["items"].get("item", [])

    if isinstance(items, dict):
        items = [items]

    saved_count = 0
    skipped_count = 0

    for item in items:
        desertion_no = item.get("desertionNo")

        already_exists = db.query(Dog).filter(Dog.description.contains(desertion_no)).first()

        if already_exists:
            skipped_count += 1
            continue

        shelter = get_or_create_shelter(db, item)

        description = (
            f"공고번호: {item.get('noticeNo')}\n"
            f"유기번호: {item.get('desertionNo')}\n"
            f"발견장소: {item.get('happenPlace')}\n"
            f"색상: {item.get('colorCd')}\n"
            f"몸무게: {item.get('weight')}\n"
            f"상태: {item.get('processState')}\n"
            f"특징: {item.get('specialMark')}"
        )

        dog = Dog(
            shelter_id=shelter.id if shelter else None,
            name=item.get("kindNm", "이름 미상"),
            breed=item.get("kindNm"),
            age=convert_age_to_number(item.get("age")),
            gender=convert_gender(item.get("sexCd")),
            image_url=item.get("popfile1") or item.get("popfile"),
            description=description,
            personality_keywords=item.get("specialMark"),

            energy_level=3,
            barking_level=3,
            independence_level=3,
            sociability_level=3,
            walk_need=3,

            good_with_children=None,
            good_with_other_dogs=None,
            separation_anxiety=None
        )

        db.add(dog)
        saved_count += 1

    db.commit()

    return {
        "message": "공공API 데이터 저장 완료",
        "saved_count": saved_count,
        "skipped_count": skipped_count
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
        "_type": "json"
    }

    response = requests.get(BASE_URL, params=params, timeout=10)

    return {
        "status_code": response.status_code,
        "url": response.url,
        "text": response.text[:2000]
    }