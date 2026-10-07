"""One score/analysis implementation shared by all matching endpoints."""

from app.survey_codes import (
    HousingType as H, FamilyType as F, OutingFrequency as O,
    LifePattern as L, WalkFrequency as W, WalkDuration as D,
    DogExperience as E, SURVEY_FIELDS, normalize_survey_value,
    display_value, parse_walk_hours, MBTI,
)


def get_score_label(score):
    if score >= 85:
        return "매우 잘 맞아요"
    if score >= 70:
        return "잘 맞는 편이에요"
    if score >= 50:
        return "조금 주의가 필요해요"
    return "신중한 고민이 필요해요"


def make_score_breakdown(survey, dog):
    s = {field: normalize_survey_value(field, getattr(survey, field)) for field in SURVEY_FIELDS}
    b = dict.fromkeys(("base_score", "housing_match", "family_match", "outing_match",
                      "life_pattern_match", "walk_match", "experience_match", "personality_match"), 0)
    b["base_score"] = 50
    energy, social, independent = dog.energy_level, dog.sociability_level, dog.independence_level
    separation, training = dog.separation_anxiety_level, dog.training_level

    def high(value, minimum):
        return value is not None and value >= minimum

    def low(value, maximum):
        return value is not None and value <= maximum

    housing = s["housing_type"]
    if housing == H.STUDIO:
        b["housing_match"] = 10 if low(energy, 2) else -10 if high(energy, 4) else 0
    elif housing == H.APARTMENT:
        b["housing_match"] = 8 if low(energy, 3) else -8 if high(energy, 5) else 0
    elif housing == H.HOUSE_WITH_YARD:
        b["housing_match"] = (12 if high(energy, 4) else 0) + (5 if high(independent, 3) else 0)
    elif housing == H.HOUSE_WITHOUT_YARD:
        b["housing_match"] = 8 if low(energy, 3) else 0

    family = s["family_type"]
    if family in (F.YOUNG_CHILDREN, F.SCHOOL_CHILDREN):
        b["family_match"] = (12 if high(social, 4) else 0) - (5 if high(energy, 5) else 0)
    elif family == F.SENIOR:
        b["family_match"] = (12 if low(energy, 2) else 0) + (5 if high(training, 3) else 0)
    elif family in (F.SINGLE, F.COUPLE):
        b["family_match"] = 8 if high(independent, 3) else 0
    elif family == F.MULTIGENERATIONAL:
        b["family_match"] = (10 if high(social, 4) else 0) + (5 if high(training, 3) else 0)
    # Adult children/student options previously had no rule; no weight is inferred.

    outing = s["outing_frequency"]
    if outing == O.MOSTLY_HOME:
        b["outing_match"] = (6 if high(separation, 4) else 0) + (5 if high(social, 3) else 0)
    elif outing == O.ONE_TO_TWO_DAYS:
        b["outing_match"] = 6 if high(independent, 2) else 0
    elif outing == O.THREE_TO_FOUR_DAYS:
        b["outing_match"] = (8 if high(independent, 3) else 0) - (8 if high(separation, 4) else 0)
    elif outing == O.DAILY:
        b["outing_match"] = (12 if high(independent, 4) else 0) - (15 if high(separation, 4) else 0)

    try:
        hours = parse_walk_hours(survey.daily_walk_time)
    except ValueError:
        hours = None
    if hours is not None:
        if hours >= 2 and high(energy, 4):
            b["walk_match"] += 10
        elif 1 <= hours < 2 and high(energy, 3) and low(energy, 4):
            b["walk_match"] += 8
        elif 0.5 <= hours < 1 and low(energy, 3):
            b["walk_match"] += 6

    life = s["life_pattern"]
    if life == L.REMOTE:
        b["life_pattern_match"] = (6 if high(separation, 4) else 0) + (4 if high(social, 3) else 0)
    elif life == L.OFFICE:
        b["life_pattern_match"] = 8 if high(independent, 3) else 0
    elif life == L.IRREGULAR:
        b["life_pattern_match"] = (10 if high(independent, 4) else 0) - (12 if high(separation, 4) else 0)

    frequency = s["walk_frequency"]
    if frequency == W.DAILY:
        b["walk_match"] += 10 if high(energy, 4) else 5 if low(energy, 3) else 0
    elif frequency == W.THREE_TO_FOUR:
        b["walk_match"] += 8 if high(energy, 2) and low(energy, 4) else 0
    elif frequency == W.ONE_TO_TWO:
        b["walk_match"] += 10 if low(energy, 2) else -8 if high(energy, 4) else 0

    duration = s["walk_duration"]
    if duration == D.UNDER_TWENTY:
        b["walk_match"] += 10 if low(energy, 2) else -8 if high(energy, 4) else 0
    elif duration == D.TWENTY_TO_FORTY:
        b["walk_match"] += 8 if low(energy, 3) else 0
    elif duration == D.FORTY_TO_SIXTY:
        b["walk_match"] += 8 if high(energy, 3) and low(energy, 4) else 0
    elif duration == D.OVER_SIXTY:
        b["walk_match"] += 10 if high(energy, 4) else 0

    experience = s["dog_experience"]
    if experience == E.FIRST:
        b["experience_match"] = (8 if high(training, 3) else 0) + (6 if low(energy, 3) else -8 if high(energy, 5) else 0)
    elif experience == E.PREVIOUS:
        b["experience_match"] = 6 if high(training, 2) else 0
    elif experience == E.CURRENT:
        b["experience_match"] = (8 if high(training, 3) else 0) + (6 if high(social, 4) else 0)

    mbti = getattr(survey.mbti, "value", survey.mbti) or ""
    try:
        mbti = MBTI(mbti.upper()).value
    except ValueError:
        mbti = ""
    if mbti.startswith("E") and high(social, 4):
        b["personality_match"] += 4
    elif mbti.startswith("I") and low(energy, 3):
        b["personality_match"] += 4
    if mbti.endswith("J") and high(training, 3):
        b["personality_match"] += 3
    elif mbti.endswith("P") and high(social, 3):
        b["personality_match"] += 3
    return b


def calculate_score(survey, dog):
    return max(0, min(sum(make_score_breakdown(survey, dog).values()), 100))


def make_warnings(survey, dog):
    warnings = []
    outing = normalize_survey_value("outing_frequency", survey.outing_frequency)
    duration = normalize_survey_value("walk_duration", survey.walk_duration)
    housing = normalize_survey_value("housing_type", survey.housing_type)
    if (dog.separation_anxiety_level or 0) >= 4 and outing in (O.THREE_TO_FOUR_DAYS, O.DAILY):
        warnings.append("혼자 있는 시간이 길어지면 분리불안이 나타날 수 있어요.")
    if (dog.energy_level or 0) >= 4 and duration in (D.UNDER_TWENTY, D.TWENTY_TO_FORTY):
        warnings.append("강아지의 활동량에 비해 산책 시간이 부족할 수 있어요.")
    if (dog.barking_level or 0) >= 4 and housing in (H.STUDIO, H.APARTMENT):
        warnings.append("짖음 수준이 높아 공동주택에서는 적응 훈련이 필요할 수 있어요.")
    return warnings


def make_care_points(survey, dog):
    points = []
    if (dog.energy_level or 0) >= 4:
        points.append("하루 1회 이상 충분한 산책과 놀이 시간이 필요해요.")
    if (dog.separation_anxiety_level or 0) >= 4:
        points.append("입양 초기에는 짧은 외출부터 시작하는 분리불안 완화 훈련이 필요해요.")
    if (dog.sociability_level or 0) >= 4:
        points.append("사람과의 상호작용을 좋아하므로 꾸준한 관심과 교감이 중요해요.")
    if dog.training_level is not None and dog.training_level <= 2:
        points.append("기본 배변 훈련과 생활 규칙 훈련이 필요할 수 있어요.")
    return points or ["기본적인 산책, 식사, 휴식 루틴을 일정하게 유지해 주세요."]


def build_analysis(survey, dog):
    breakdown = make_score_breakdown(survey, dog)
    score = max(0, min(sum(breakdown.values()), 100))
    labels = {field: display_value(field, getattr(survey, field)) for field in SURVEY_FIELDS}
    traits = {"활동량": dog.energy_level, "분리불안": dog.separation_anxiety_level,
              "친화력": dog.sociability_level, "훈련성": dog.training_level, "독립성": dog.independence_level}
    summary = ", ".join(f"{name} {value}점" for name, value in traits.items() if value is not None)
    reason = (
        f"{dog.name}는 {summary or '아직 확인되지 않은 성향'}을 가진 반려견입니다. "
        f"사용자의 주거 형태({labels['housing_type']}), 외출 빈도({labels['outing_frequency']}), "
        f"산책 가능 시간({labels['walk_duration']}), 반려견 경험({labels['dog_experience']})을 기준으로 "
        f"매칭 점수 {score}%가 계산되었습니다."
    )
    field_labels = {"housing_type": "주거 형태", "family_type": "가구 구성",
                    "outing_frequency": "외출 빈도", "life_pattern": "생활 유형",
                    "walk_frequency": "산책 빈도", "walk_duration": "1회 산책 시간",
                    "dog_experience": "반려견 경험"}
    detail = "[사용자 생활 정보]\n" + "\n".join(f"- {field_labels[field]}: {value}" for field, value in labels.items())
    detail += f"\n- 하루 평균 산책 가능 시간(시간): {survey.daily_walk_time}\n- MBTI: {getattr(survey.mbti, 'value', survey.mbti)}"
    detail += "\n\n[반려견 성향 정보]\n" + "\n".join(f"- {name}: {value}" for name, value in traits.items())
    detail += f"\n- 성향 출처: {dog.trait_source}\n\n[매칭 결과]\n최종 매칭 점수는 {score}%입니다."
    return {"score": score, "label": get_score_label(score), "reason": reason,
            "detail_analysis": detail, "warnings": make_warnings(survey, dog),
            "care_points": make_care_points(survey, dog), "score_breakdown": breakdown}
