"""Create marked demo fixtures without modifying existing records."""

import argparse
import json

from sqlalchemy import text

from app.models import User, Shelter, Dog

EMAIL = "api-demo@example.invalid"
NICKNAME = "[DEMO] API 시연 사용자"
SHELTER_KEY = "DEMO-API-SHELTER-V1"
SHELTER_NAME = "[DEMO] API 시연 보호소"
DESCRIPTION = "API 시연용 가상 강아지입니다. 실제 입양 대상이 아닙니다."
TRAIT_FIELDS = ("energy_level", "separation_anxiety_level", "sociability_level",
                "training_level", "independence_level")
FIXTURES = (
    ("차분이", (1, 1, 3, 5, 5)), ("애교이", (2, 5, 5, 3, 1)),
    ("활발이", (5, 2, 4, 4, 4)), ("독립이", (3, 1, 2, 3, 5)),
    ("친구이", (4, 3, 5, 2, 2)), ("균형이", (3, 3, 3, 3, 3)),
)


def seed(db, apply=False):
    if apply and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(724931001)"))
    user = db.query(User).filter(User.email == EMAIL).first()
    shelter = db.query(Shelter).filter(Shelter.desertion_no == SHELTER_KEY).first()
    if user and (user.nickname != NICKNAME or user.password is not None):
        raise ValueError("시연 사용자 이메일이 기존 다른 사용자와 충돌합니다.")
    if shelter and shelter.name != SHELTER_NAME:
        raise ValueError("시연 보호소 식별자가 기존 다른 보호소와 충돌합니다.")
    dogs = []
    for index, (name, traits) in enumerate(FIXTURES, 1):
        key = f"DEMO-API-{index:02d}"
        dog = db.query(Dog).filter(Dog.desertion_no == key).first()
        if dog and (dog.description != DESCRIPTION or dog.name != f"[DEMO] {name}"
                    or shelter is None or dog.shelter_id != shelter.id
                    or dog.trait_source != "manual_demo"):
            raise ValueError(f"시연 강아지 식별자 {key}가 기존 다른 데이터와 충돌합니다.")
        dogs.append((key, name, traits, dog))
    planned = {"users": int(user is None), "shelters": int(shelter is None),
               "dogs": sum(dog is None for _, _, _, dog in dogs)}
    if apply:
        if user is None:
            user = User(email=EMAIL, nickname=NICKNAME, password=None)
            db.add(user)
        if shelter is None:
            shelter = Shelter(desertion_no=SHELTER_KEY, name=SHELTER_NAME,
                              phone="000-0000-0000", address="시연용 가상 주소 (실제 위치 아님)")
            db.add(shelter)
        db.flush()
        for key, name, traits, dog in dogs:
            if dog is None:
                dog = Dog(desertion_no=key, shelter_id=shelter.id, name=f"[DEMO] {name}",
                          breed="시연용 믹스견", age=3, gender="unknown", description=DESCRIPTION,
                          special_note="가상 데이터", personality_keywords="시연용", trait_source="manual_demo",
                          barking_level=3, walk_need=3, **dict(zip(TRAIT_FIELDS, traits)))
                db.add(dog)
        db.flush()
    return {"applied": apply, "planned_creates": planned, "user_id": user.id if user else None,
            "shelter_id": shelter.id if shelter else None,
            "dog_ids": [row.id for row in db.query(Dog).filter(
                Dog.desertion_no.in_([key for key, _, _, _ in dogs])).order_by(Dog.desertion_no)],
            "email": EMAIL}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="시연 데이터 저장; 생략 시 조회만")
    args = parser.parse_args(argv)
    from app.database import SessionLocal, engine
    from scripts.prepare_demo_db import schema_state, validate_local_engine
    try:
        validate_local_engine(engine)
        with engine.connect() as connection:
            if not schema_state(connection)["ready"]:
                raise ValueError("먼저 prepare_demo_db --apply를 실행하세요.")
        with SessionLocal.begin() as db:
            result = seed(db, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        print(f"시연 데이터 준비 실패: {exc}")
    except Exception:
        print("시연 데이터 준비 실패: DB 연결·제약조건을 확인하세요. 저장 트랜잭션은 롤백됩니다.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
