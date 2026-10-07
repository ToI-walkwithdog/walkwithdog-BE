import os


def recommendation_candidate(dog):
    if dog.trait_source == "manual_demo" and os.getenv("MATCHING_INCLUDE_DEMO", "false").lower() != "true":
        return False
    state = dog.process_state or (dog.external_data or {}).get("processState") or ""
    if "종료" in state:
        return False
    # Old imports only stored public status in the description.
    description = dog.description or ""
    if not state and ("상태: 종료" in description or "현재 상태: 종료" in description):
        return False
    mark = (dog.external_data or {}).get("specialMark") or description
    if any(word in mark for word in ("보호자와 연락됨", "보호자 인계", "소유자 반환")):
        return False
    return True
