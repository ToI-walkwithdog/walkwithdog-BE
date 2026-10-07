"""Deterministic chart data; unknown/estimated values are never presented as observation."""
import re
from collections import Counter

AXES = (
    ("energy_level", "활동량", "높을수록 활동량이 많음", "활발함", "차분함"),
    ("separation_anxiety_level", "분리불안", "높을수록 혼자 있을 때 불안할 수 있음", "혼자 있기 연습", "안정감"),
    ("sociability_level", "친화력", "높을수록 친화적인 경향", "친화적", "천천히 친해지기"),
    ("training_level", "훈련성", "높을수록 훈련에 잘 반응하는 경향", "훈련에 잘 반응", "차근차근 배우기"),
    ("independence_level", "독립성", "높을수록 독립적인 경향", "독립적", "함께하기 선호"),
)


def personality_data(dog):
    source = dog.trait_source or "unknown"
    estimated = source != "shelter_observed"
    axes = []
    words = Counter()
    for key, label, interpretation, high, low in AXES:
        value = getattr(dog, key, None)
        valid = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5
        axes.append({"code": key, "label": label, "value": value if valid else None,
                     "available": valid, "source": source if valid else "unknown",
                     "estimated": estimated if valid else False, "interpretation": interpretation})
        if valid and value >= 4:
            words[high] += 1
        elif valid and value <= 2:
            words[low] += 1
    keywords = dog.personality_keywords or ""
    for word in re.findall(r"[가-힣]{2,12}", keywords[:1000]):
        words[word] += 1
    return {"dog_id": dog.id, "dog_name": dog.name, "scale": {"min": 1, "max": 5},
            "axes": axes, "keywords": [{"text": word, "weight": count} for word, count in words.most_common(30)],
            "source": source, "estimated": estimated, "missing_fields": [a["code"] for a in axes if not a["available"]],
            "notice": "성향은 참고 정보이며 개별 행동을 보장하지 않습니다. 추정값과 관찰값을 구분해 확인하세요."}
