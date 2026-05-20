from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User, Dog, Survey, MatchingResult
from schemas import MatchingCreate, MatchingResponse, MatchingDetailResponse

router = APIRouter(
    prefix="/matching",
    tags=["matching"]
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def calculate_score(survey: Survey, dog: Dog):
    score = 50
    reasons = []

    # 산책 빈도와 강아지 산책 필요도
    if survey.walk_frequency in ["매일", "자주"]:
        if dog.walk_need and dog.walk_need >= 4:
            score += 15
            reasons.append("사용자의 산책 빈도와 강아지의 산책 필요도가 잘 맞아요.")
        elif dog.walk_need and dog.walk_need <= 2:
            score += 8
            reasons.append("산책 부담이 크지 않아 생활 패턴에 잘 맞을 수 있어요.")

    # 반려견 경험
    if survey.dog_experience == "있음":
        score += 10
        reasons.append("반려견 경험이 있어 적응 가능성이 높아요.")
    else:
        if dog.energy_level and dog.energy_level <= 3:
            score += 8
            reasons.append("초보 보호자도 비교적 돌보기 쉬운 성향이에요.")

    # 주거 형태
    if survey.housing_type in ["아파트", "빌라"]:
        if dog.barking_level and dog.barking_level <= 2:
            score += 10
            reasons.append("짖음 정도가 낮아 공동주택 환경에 적합해요.")
        if dog.energy_level and dog.energy_level <= 3:
            score += 5
            reasons.append("실내 생활에 무리가 적은 에너지 수준이에요.")

    # 가구 구성
    if survey.family_type and "아이" in survey.family_type:
        if dog.good_with_children == "yes":
            score += 10
            reasons.append("아이와 함께 지내기 좋은 성향이에요.")

    # 사회성
    if dog.sociability_level and dog.sociability_level >= 4:
        score += 5
        reasons.append("사회성이 좋아 가족과 친해지기 쉬워요.")

    # 분리불안
    if survey.outing_frequency in ["많음", "자주"]:
        if dog.separation_anxiety == "no":
            score += 10
            reasons.append("외출이 잦은 생활에도 비교적 잘 적응할 수 있어요.")
        elif dog.separation_anxiety == "yes":
            score -= 10
            reasons.append("외출이 잦다면 분리불안 관리가 필요할 수 있어요.")

    score = max(0, min(score, 100))

    if not reasons:
        reasons.append("기본 생활 패턴과 강아지 성향 정보를 기준으로 추천되었어요.")

    detail_analysis = (
        f"사용자의 주거 형태는 {survey.housing_type}, "
        f"가구 구성은 {survey.family_type}, "
        f"외출 빈도는 {survey.outing_frequency}, "
        f"산책 빈도는 {survey.walk_frequency}, "
        f"반려견 경험은 {survey.dog_experience}입니다. "
        f"{dog.name}의 에너지 수준은 {dog.energy_level}, "
        f"짖음 정도는 {dog.barking_level}, "
        f"산책 필요도는 {dog.walk_need}, "
        f"사회성은 {dog.sociability_level}입니다."
    )

    return score, " ".join(reasons), detail_analysis

@router.post("/", response_model=MatchingResponse)
def create_matching(
    matching: MatchingCreate,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == matching.user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    dog = db.query(Dog).filter(Dog.id == matching.dog_id).first()
    if dog is None:
        raise HTTPException(status_code=404, detail="Dog not found")

    survey = (
        db.query(Survey)
        .filter(Survey.user_id == matching.user_id)
        .order_by(Survey.id.desc())
        .first()
    )

    if survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")

    score, reason, detail_analysis = calculate_score(survey, dog)

    new_matching = MatchingResult(
        user_id=matching.user_id,
        dog_id=matching.dog_id,
        score=score,
        reason=reason,
        detail_analysis=detail_analysis
    )

    db.add(new_matching)
    db.commit()
    db.refresh(new_matching)

    return new_matching


@router.get("/{user_id}", response_model=list[MatchingDetailResponse])
def get_matching_results(
    user_id: int,
    db: Session = Depends(get_db)
):
    return (
        db.query(MatchingResult)
        .filter(MatchingResult.user_id == user_id)
        .all()
    )