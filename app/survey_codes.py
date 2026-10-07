"""Stable API codes; Korean labels remain accepted for existing clients/data."""

from enum import Enum
import re


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("-", "~")


class SurveyCode(str, Enum):
    def __new__(cls, code, label):
        obj = str.__new__(cls, code)
        obj._value_ = code
        obj.label = label
        return obj

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            value = compact(value)
            for member in cls:
                if value in (compact(member.value), compact(member.label)):
                    return member
        return None


class HousingType(SurveyCode):
    STUDIO = "studio", "원룸/오피스텔"
    APARTMENT = "apartment", "아파트/빌라"
    HOUSE_WITH_YARD = "house_with_yard", "마당 있는 주택"
    HOUSE_WITHOUT_YARD = "house_without_yard", "마당 없는 주택"


class FamilyType(SurveyCode):
    SINGLE = "single", "1인 가구"
    COUPLE = "couple_without_children", "부부/커플 (자녀 없음)"
    YOUNG_CHILDREN = "young_children", "영유아 자녀 가구(0~6세)"
    SCHOOL_CHILDREN = "school_children", "학령기 자녀 가구(7~18세)"
    ADULT_CHILDREN = "adult_children", "성인 자녀 동거 가구"
    SENIOR = "senior", "노년 가구(60세 이상)"
    MULTIGENERATIONAL = "multigenerational", "다세대 가구(조부모+부모+자녀)"


class OutingFrequency(SurveyCode):
    MOSTLY_HOME = "mostly_home", "거의 항상 집에 있어요"
    ONE_TO_TWO_DAYS = "1_to_2_days", "주 1~2일 외출해요"
    THREE_TO_FOUR_DAYS = "3_to_4_days", "주 3~4일 외출해요"
    DAILY = "daily", "거의 매일 집을 비워요"


class LifePattern(SurveyCode):
    REMOTE = "remote_worker", "재택/프리랜서"
    OFFICE = "office_worker", "출퇴근 직장인"
    IRREGULAR = "irregular_worker", "교대근무/출장 잦음"
    STUDENT = "student", "학생"


class WalkFrequency(SurveyCode):
    DAILY = "daily", "거의 매일 가능해요"
    THREE_TO_FOUR = "3_to_4_per_week", "주 3~4회 가능해요"
    ONE_TO_TWO = "1_to_2_per_week", "주 1~2회 가능해요"


class WalkDuration(SurveyCode):
    UNDER_TWENTY = "under_20_minutes", "20분 미만"
    TWENTY_TO_FORTY = "20_to_40_minutes", "20~40분"
    FORTY_TO_SIXTY = "40_to_60_minutes", "40~1시간"
    OVER_SIXTY = "60_minutes_or_more", "1시간 이상"


class DogExperience(SurveyCode):
    FIRST = "first_time", "처음 키워봐요"
    PREVIOUS = "previous_owner", "예전에 키워본 적 있어요"
    CURRENT = "current_owner", "현재 키우고 있어요"


MBTI = Enum("MBTI", {v: v for v in (
    "ISTJ", "ISFJ", "INFJ", "INTJ", "ISTP", "ISFP", "INFP", "INTP",
    "ESTP", "ESFP", "ENFP", "ENTP", "ESTJ", "ESFJ", "ENFJ", "ENTJ",
)}, type=str)

SURVEY_FIELDS = {
    "housing_type": HousingType, "family_type": FamilyType,
    "outing_frequency": OutingFrequency, "life_pattern": LifePattern,
    "walk_frequency": WalkFrequency, "walk_duration": WalkDuration,
    "dog_experience": DogExperience,
}


def parse_walk_hours(value):
    """daily_walk_time is hours, including legacy '30분' / '1.5시간'."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("산책 시간은 시간 단위 숫자여야 합니다.")
    text = re.sub(r"\s+", "", str(value))
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(시간|분)?", text)
    if not match:
        raise ValueError("산책 시간은 시간 단위 숫자여야 합니다.")
    hours = float(match[1]) / (60 if match[2] == "분" else 1)
    if not 0 <= hours <= 24:
        raise ValueError("산책 시간은 0~24시간이어야 합니다.")
    return hours


def normalize_survey_value(field, value):
    if value is None:
        return None
    try:
        return SURVEY_FIELDS[field](value)
    except ValueError:
        # Unknown legacy values must not crash matching or gain points.
        return None


def display_value(field, value):
    normalized = normalize_survey_value(field, value)
    return normalized.label if normalized else value
