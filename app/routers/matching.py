from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Survey, Dog, MatchingResult
from app.schemas import MatchingDetailResponse, MatchingScoreRequest, MatchingScoreResponse

from app.dependencies import require_login
from app.models import User

router = APIRouter(
    prefix="/matching",
    tags=["matching"]
)


def calculate_score(survey: Survey, dog: Dog) -> int:
    score = 50

    # 1. 주거 형태 ↔ 활동량
    if survey.housing_type == "원룸/오피스텔":
        if dog.energy_level is not None and dog.energy_level <= 2:
            score += 10
        elif dog.energy_level is not None and dog.energy_level >= 4:
            score -= 10

    elif survey.housing_type == "아파트/빌라":
        if dog.energy_level is not None and dog.energy_level <= 3:
            score += 8
        elif dog.energy_level is not None and dog.energy_level >= 5:
            score -= 8

    elif survey.housing_type == "마당 있는 주택":
        if dog.energy_level is not None and dog.energy_level >= 4:
            score += 12
        if dog.independence_level is not None and dog.independence_level >= 3:
            score += 5

    elif survey.housing_type == "마당 없는 주택":
        if dog.energy_level is not None and dog.energy_level <= 3:
            score += 8

    # 2. 가구 구성 ↔ 친화력/활동량
    if survey.family_type in ["영유아 자녀 가구(0-6세)", "학령기 자녀 가구(7-18세)"]:
        if dog.sociability_level is not None and dog.sociability_level >= 4:
            score += 12
        if dog.energy_level is not None and dog.energy_level >= 5:
            score -= 5

    elif survey.family_type == "노년 가구(60세 이상)":
        if dog.energy_level is not None and dog.energy_level <= 2:
            score += 12
        if dog.training_level is not None and dog.training_level >= 3:
            score += 5

    elif survey.family_type in ["1인 가구", "부부/커플 (자녀없음)"]:
        if dog.independence_level is not None and dog.independence_level >= 3:
            score += 8

    elif survey.family_type == "다세대 가구(조부모+부모+자녀)":
        if dog.sociability_level is not None and dog.sociability_level >= 4:
            score += 10
        if dog.training_level is not None and dog.training_level >= 3:
            score += 5

    # 3. 외출 빈도 ↔ 분리불안/독립성
    if survey.outing_frequency == "거의 항상 집에 있어요":
        if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
            score += 6
        if dog.sociability_level is not None and dog.sociability_level >= 3:
            score += 5

    elif survey.outing_frequency == "주 1~2일 외출해요":
        if dog.independence_level is not None and dog.independence_level >= 2:
            score += 6

    elif survey.outing_frequency == "주 3~4일 외출해요":
        if dog.independence_level is not None and dog.independence_level >= 3:
            score += 8
        if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
            score -= 8

    elif survey.outing_frequency == "거의 매일 집을 비워요":
        if dog.independence_level is not None and dog.independence_level >= 4:
            score += 12
        if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
            score -= 15

    # 4. 하루 평균 산책 가능 시간 ↔ 활동량
    if survey.daily_walk_time:
        if "2" in survey.daily_walk_time or "3" in survey.daily_walk_time:
            if dog.energy_level is not None and dog.energy_level >= 4:
                score += 10
        elif "1" in survey.daily_walk_time:
            if dog.energy_level is not None and 3 <= dog.energy_level <= 4:
                score += 8
        elif "30" in survey.daily_walk_time or "0.5" in survey.daily_walk_time:
            if dog.energy_level is not None and dog.energy_level <= 3:
                score += 6

    # 5. 생활 유형 ↔ 독립성/분리불안
    if survey.life_pattern == "재택/프리랜서":
        if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
            score += 6
        if dog.sociability_level is not None and dog.sociability_level >= 3:
            score += 4

    elif survey.life_pattern == "출퇴근 직장인":
        if dog.independence_level is not None and dog.independence_level >= 3:
            score += 8

    elif survey.life_pattern == "교대근무/출장 잦음":
        if dog.independence_level is not None and dog.independence_level >= 4:
            score += 10
        if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
            score -= 12

    # 6. 산책 빈도 ↔ 활동량
    if survey.walk_frequency == "거의 매일 가능해요":
        if dog.energy_level is not None and dog.energy_level >= 4:
            score += 10
        elif dog.energy_level is not None and dog.energy_level <= 3:
            score += 5

    elif survey.walk_frequency == "주 3~4회 가능해요":
        if dog.energy_level is not None and 2 <= dog.energy_level <= 4:
            score += 8

    elif survey.walk_frequency == "주 1~2회 가능해요":
        if dog.energy_level is not None and dog.energy_level <= 2:
            score += 10
        elif dog.energy_level is not None and dog.energy_level >= 4:
            score -= 8

    # 7. 1회 산책 시간 ↔ 활동량
    if survey.walk_duration == "20분 미만":
        if dog.energy_level is not None and dog.energy_level <= 2:
            score += 10
        elif dog.energy_level is not None and dog.energy_level >= 4:
            score -= 8

    elif survey.walk_duration == "20~40분":
        if dog.energy_level is not None and dog.energy_level <= 3:
            score += 8

    elif survey.walk_duration == "40~1시간":
        if dog.energy_level is not None and 3 <= dog.energy_level <= 4:
            score += 8

    elif survey.walk_duration == "1시간 이상":
        if dog.energy_level is not None and dog.energy_level >= 4:
            score += 10

    # 8. 반려견 경험 ↔ 훈련성/활동량
    if survey.dog_experience == "처음 키워봐요":
        if dog.training_level is not None and dog.training_level >= 3:
            score += 8
        if dog.energy_level is not None and dog.energy_level <= 3:
            score += 6
        elif dog.energy_level is not None and dog.energy_level >= 5:
            score -= 8

    elif survey.dog_experience == "예전에 키워본 적 있어요":
        if dog.training_level is not None and dog.training_level >= 2:
            score += 6

    elif survey.dog_experience == "현재 키우고 있어요":
        if dog.training_level is not None and dog.training_level >= 3:
            score += 8
        if dog.sociability_level is not None and dog.sociability_level >= 4:
            score += 6

    # 9. MBTI는 보조 점수
    if survey.mbti:
        mbti = survey.mbti.upper()

        if mbti.startswith("E"):
            if dog.sociability_level is not None and dog.sociability_level >= 4:
                score += 4
        elif mbti.startswith("I"):
            if dog.energy_level is not None and dog.energy_level <= 3:
                score += 4

        if "J" in mbti:
            if dog.training_level is not None and dog.training_level >= 3:
                score += 3

        if "P" in mbti:
            if dog.sociability_level is not None and dog.sociability_level >= 3:
                score += 3

    return max(0, min(score, 100))


def make_reason(survey: Survey, dog: Dog, score: int) -> str:
    reasons = []

    if dog.energy_level is not None:
        reasons.append(f"활동량 {dog.energy_level}점")
    if dog.separation_anxiety_level is not None:
        reasons.append(f"분리불안 {dog.separation_anxiety_level}점")
    if dog.sociability_level is not None:
        reasons.append(f"친화력 {dog.sociability_level}점")
    if dog.training_level is not None:
        reasons.append(f"훈련성 {dog.training_level}점")
    if dog.independence_level is not None:
        reasons.append(f"독립성 {dog.independence_level}점")

    trait_summary = ", ".join(reasons)

    return (
        f"{dog.name}는 {trait_summary}을 가진 반려견입니다. "
        f"사용자의 주거 형태({survey.housing_type}), 외출 빈도({survey.outing_frequency}), "
        f"산책 가능 시간({survey.walk_duration}), 반려견 경험({survey.dog_experience})을 기준으로 "
        f"매칭 점수 {score}%가 계산되었습니다."
    )


def make_detail_analysis(survey: Survey, dog: Dog, score: int) -> str:
    return (
        f"[사용자 생활 정보]\n"
        f"- 주거 형태: {survey.housing_type}\n"
        f"- 가구 구성: {survey.family_type}\n"
        f"- 외출 빈도: {survey.outing_frequency}\n"
        f"- 하루 평균 산책 가능 시간: {survey.daily_walk_time}\n"
        f"- 생활 유형: {survey.life_pattern}\n"
        f"- 산책 빈도: {survey.walk_frequency}\n"
        f"- 1회 산책 시간: {survey.walk_duration}\n"
        f"- 반려견 경험: {survey.dog_experience}\n"
        f"- MBTI: {survey.mbti}\n\n"
        f"[반려견 성향 정보]\n"
        f"- 활동량: {dog.energy_level}\n"
        f"- 분리불안: {dog.separation_anxiety_level}\n"
        f"- 친화력: {dog.sociability_level}\n"
        f"- 훈련성: {dog.training_level}\n"
        f"- 독립성: {dog.independence_level}\n"
        f"- 성향 출처: {dog.trait_source}\n\n"
        f"[매칭 결과]\n"
        f"최종 매칭 점수는 {score}%입니다."
    )

def get_score_label(score: int) -> str:
    if score >= 85:
        return "매우 잘 맞아요"
    elif score >= 70:
        return "잘 맞는 편이에요"
    elif score >= 50:
        return "조금 주의가 필요해요"
    else:
        return "신중한 고민이 필요해요"


def make_warnings(survey: Survey, dog: Dog) -> list[str]:
    warnings = []

    if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
        if survey.outing_frequency in ["주 3~4일 외출해요", "거의 매일 집을 비워요"]:
            warnings.append("혼자 있는 시간이 길어지면 분리불안이 나타날 수 있어요.")

    if dog.energy_level is not None and dog.energy_level >= 4:
        if survey.walk_duration in ["20분 미만", "20~40분"]:
            warnings.append("강아지의 활동량에 비해 산책 시간이 부족할 수 있어요.")

    if dog.barking_level is not None and dog.barking_level >= 4:
        if survey.housing_type in ["원룸/오피스텔", "아파트/빌라"]:
            warnings.append("짖음 수준이 높아 공동주택에서는 적응 훈련이 필요할 수 있어요.")

    return warnings


def make_care_points(survey: Survey, dog: Dog) -> list[str]:
    care_points = []

    if dog.energy_level is not None and dog.energy_level >= 4:
        care_points.append("하루 1회 이상 충분한 산책과 놀이 시간이 필요해요.")

    if dog.separation_anxiety_level is not None and dog.separation_anxiety_level >= 4:
        care_points.append("입양 초기에는 짧은 외출부터 시작하는 분리불안 완화 훈련이 필요해요.")

    if dog.sociability_level is not None and dog.sociability_level >= 4:
        care_points.append("사람과의 상호작용을 좋아하므로 꾸준한 관심과 교감이 중요해요.")

    if dog.training_level is not None and dog.training_level <= 2:
        care_points.append("기본 배변 훈련과 생활 규칙 훈련이 필요할 수 있어요.")

    if not care_points:
        care_points.append("기본적인 산책, 식사, 휴식 루틴을 일정하게 유지해 주세요.")

    return care_points


def make_score_breakdown(survey: Survey, dog: Dog) -> dict[str, int]:
    breakdown = {
        "housing_match": 0,
        "family_match": 0,
        "outing_match": 0,
        "walk_match": 0,
        "experience_match": 0,
        "personality_match": 0
    }

    if survey.housing_type and dog.energy_level is not None:
        breakdown["housing_match"] = 15

    if survey.family_type and dog.sociability_level is not None:
        breakdown["family_match"] = 15

    if survey.outing_frequency and dog.independence_level is not None:
        breakdown["outing_match"] = 15

    if survey.walk_duration and dog.energy_level is not None:
        breakdown["walk_match"] = 20

    if survey.dog_experience and dog.training_level is not None:
        breakdown["experience_match"] = 15

    if dog.energy_level is not None and dog.sociability_level is not None:
        breakdown["personality_match"] = 20

    return breakdown


@router.post("/score", response_model=MatchingScoreResponse)
def get_matching_score(
    request: MatchingScoreRequest,
    db: Session = Depends(get_db)
):
    survey = (
        db.query(Survey)
        .filter(Survey.user_id == request.user_id)
        .order_by(Survey.created_at.desc())
        .first()
    )

    if survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")

    dog = db.query(Dog).filter(Dog.id == request.dog_id).first()

    if dog is None:
        raise HTTPException(status_code=404, detail="Dog not found")

    score = calculate_score(survey, dog)

    return {
        "score": score,
        "label": get_score_label(score),
        "reason": make_reason(survey, dog, score),
        "warnings": make_warnings(survey, dog),
        "care_points": make_care_points(survey, dog),
        "score_breakdown": make_score_breakdown(survey, dog)
    }

@router.post("/{survey_id}", response_model=list[MatchingDetailResponse])
def create_matching_results(
    survey_id: int, 
    current_user: User = Depends(require_login),
    db: Session = Depends(get_db)):
    survey = db.query(Survey).filter(Survey.id == survey_id).first()

    if survey is None:
        raise HTTPException(status_code=404, detail="Survey not found")

    dogs = db.query(Dog).all()

    if not dogs:
        raise HTTPException(status_code=404, detail="Dog data not found")

    calculated_results = []

    for dog in dogs:
        score = calculate_score(survey, dog)
        reason = make_reason(survey, dog, score)
        detail_analysis = make_detail_analysis(survey, dog, score)

        calculated_results.append({
            "dog": dog,
            "score": score,
            "reason": reason,
            "detail_analysis": detail_analysis
        })

    top_results = sorted(
        calculated_results,
        key=lambda x: x["score"],
        reverse=True
    )[:5]

    saved_results = []

    for result in top_results:
        matching_result = MatchingResult(
            user_id=survey.user_id,
            dog_id=result["dog"].id,
            score=result["score"],
            reason=result["reason"],
            detail_analysis=result["detail_analysis"]
        )

        db.add(matching_result)
        saved_results.append(matching_result)

    db.commit()

    for result in saved_results:
        db.refresh(result)

    return saved_results


@router.get("/history/{user_id}", response_model=list[MatchingDetailResponse])
def get_matching_history(user_id: int, db: Session = Depends(get_db)):
    return (
        db.query(MatchingResult)
        .filter(MatchingResult.user_id == user_id)
        .order_by(MatchingResult.created_at.desc())
        .all()
    )