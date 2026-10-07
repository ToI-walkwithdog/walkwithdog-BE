"""Run the complete matching demo against a local API server."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.schemas import MatchingDetailResponse
from app.survey_codes import SURVEY_FIELDS, MBTI

FIRST_SURVEY = {
    "housing_type": "studio", "family_type": "single", "outing_frequency": "daily",
    "daily_walk_time": 0.5, "life_pattern": "office_worker", "walk_frequency": "3_to_4_per_week",
    "walk_duration": "20_to_40_minutes", "dog_experience": "first_time", "mbti": "INFJ",
}
SECOND_SURVEY = {
    **FIRST_SURVEY, "housing_type": "house_with_yard", "outing_frequency": "mostly_home",
    "daily_walk_time": 2, "life_pattern": "remote_worker", "walk_frequency": "daily",
    "walk_duration": "60_minutes_or_more",
}
ANALYSIS_FIELDS = ("score", "label", "reason", "detail_analysis", "warnings", "care_points", "score_breakdown")


class DemoError(RuntimeError):
    pass


def require(condition, stage, message):
    if not condition:
        raise DemoError(f"{stage}: {message}")


def local_base_url(value):
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1")
            or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")):
        raise argparse.ArgumentTypeError("API 시연은 자격증명 없는 로컬 서버 주소만 허용합니다.")
    try:
        url.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("유효한 포트가 필요합니다.") from exc
    return value.rstrip("/")


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("양의 정수가 필요합니다.")
    return number


def positive_timeout(value):
    number = float(value)
    if not 0 < number <= 120:
        raise argparse.ArgumentTypeError("timeout은 0보다 크고 120초 이하여야 합니다.")
    return number


def request_json(client, method, path, stage, timeout, payload=None):
    try:
        response = client.request(method, path, json=payload, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise DemoError(f"{stage}: 요청 시간 초과") from exc
    except httpx.RequestError as exc:
        raise DemoError(f"{stage}: 서버 연결 실패") from exc
    require(response.status_code == 200, stage, f"HTTP {response.status_code} (기대값 200)")
    try:
        return response.json()
    except ValueError as exc:
        raise DemoError(f"{stage}: JSON이 아닌 응답") from exc


def validate_results(results, user_id, survey_id, stage):
    require(isinstance(results, list) and 1 <= len(results) <= 5, stage, "결과는 1~5개 배열이어야 합니다.")
    try:
        for result in results:
            MatchingDetailResponse.model_validate(result)
            require(all(type(result.get(key)) is int and result[key] > 0 for key in ("id", "dog_id", "user_id", "survey_id")),
                    stage, "잘못된 결과 ID")
            require(result["user_id"] == user_id and result["survey_id"] == survey_id,
                    stage, "사용자/설문 ID 불일치")
            require(type(result["score"]) is int and 0 <= result["score"] <= 100, stage, "잘못된 점수")
            require(result["dog"]["id"] == result["dog_id"], stage, "강아지 ID 불일치")
            require(bool(result["reason"]) and bool(result["detail_analysis"]), stage, "분석 설명 누락")
            dog = result["dog"]
            require(all(field in dog for field in ("energy_level", "separation_anxiety_level", "sociability_level",
                                                   "training_level", "independence_level", "shelter")),
                    stage, "성향/보호소 필드 누락")
            require(result["score_breakdown"].get("base_score") == 50, stage, "점수 구성 누락")
            require(max(0, min(sum(result["score_breakdown"].values()), 100)) == result["score"],
                    stage, "점수와 구성 합계 불일치")
        require(len({r["id"] for r in results}) == len(results), stage, "중복 결과 ID")
        require(len({r["dog_id"] for r in results}) == len(results), stage, "중복 강아지")
        order = [(-r["score"], r["dog_id"]) for r in results]
        require(order == sorted(order), stage, "점수/동점 정렬 불일치")
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        raise DemoError(f"{stage}: 결과 응답 구조 검증 실패") from exc


def validate_history(history, expected, stage):
    require(isinstance(history, list), stage, "이력이 배열이 아닙니다.")
    try:
        by_id = {row["id"]: row for row in history}
        require(len(by_id) == len(history), stage, "중복 이력 ID")
        for result in expected:
            require(result["id"] in by_id, stage, "이번 실행의 결과가 이력에 없습니다.")
            saved = by_id[result["id"]]
            require(saved["survey_id"] == result["survey_id"] and saved["user_id"] == result["user_id"],
                    stage, "이력 소유자/설문 불일치")
            require(all(saved[field] == result[field] for field in ANALYSIS_FIELDS),
                    stage, "저장된 분석 내용이 변경됐습니다.")
    except (KeyError, TypeError) as exc:
        raise DemoError(f"{stage}: 이력 응답 구조 검증 실패") from exc


def run_demo(client, user_id, timeout=10, check_concurrency=False, report=print):
    def call(method, path, stage, payload=None):
        data = request_json(client, method, path, stage, timeout, payload)
        report(f"{stage}: HTTP 200")
        return data

    options = call("GET", "/surveys/options", "1. 선택지 조회")
    require(isinstance(options, dict), "1. 선택지 조회", "선택지 객체 누락")
    for field, enum in {**SURVEY_FIELDS, "mbti": MBTI}.items():
        values = options.get(field)
        require(isinstance(values, list) and values, "1. 선택지 조회", f"{field} 누락")
        require(all(isinstance(v, dict) and isinstance(v.get("label"), str) and v["label"]
                    and isinstance(v.get("code"), str) for v in values),
                "1. 선택지 조회", f"{field} label 누락")
        require({v.get("code") for v in values} == {v.value for v in enum},
                "1. 선택지 조회", f"{field} 코드 불일치")

    def create_survey(payload, stage):
        response = call("POST", "/surveys/", stage, {"user_id": user_id, **payload})
        require(isinstance(response, dict) and type(response.get("id")) is int and response["id"] > 0,
                stage, "설문 ID 누락")
        require(response.get("user_id") == user_id and all(response.get(k) == v for k, v in payload.items()),
                stage, "정규화된 설문 응답 불일치")
        return response["id"]

    first_id = create_survey(FIRST_SURVEY, "2. 첫 설문 저장")
    if check_concurrency:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request_json, client, "POST", f"/matching/{first_id}",
                                   "3. 동시 매칭 생성", timeout) for _ in range(2)]
            first, competing = [future.result() for future in futures]
        validate_results(first, user_id, first_id, "3. 동시 매칭 생성")
        require(first == competing, "3. 동시 매칭 생성", "동시 요청 결과가 다릅니다.")
        report("3. 동시 매칭 생성: 두 요청 HTTP 200 / 동일 결과")
    else:
        first = call("POST", f"/matching/{first_id}", "3. 매칭 생성")
        validate_results(first, user_id, first_id, "3. 매칭 생성")
    repeated = call("POST", f"/matching/{first_id}", "4. 매칭 재호출")
    require(first == repeated, "4. 매칭 재호출", "동일 설문의 결과/ID가 변경됐습니다.")
    history = call("GET", f"/matching/history/{user_id}", "5. 이력 조회")
    validate_history(history, first, "5. 이력 조회")
    scoped = [r for r in history if r.get("survey_id") == first_id]
    require(len(scoped) == len(first), "5. 이력 조회", "동일 설문에 중복 이력이 있습니다.")
    second_id = create_survey(SECOND_SURVEY, "6. 새 설문 저장")
    require(second_id != first_id, "6. 새 설문 저장", "새 설문 ID가 같음")
    second = call("POST", f"/matching/{second_id}", "6. 새 설문 매칭")
    validate_results(second, user_id, second_id, "6. 새 설문 매칭")
    require(not ({r["id"] for r in first} & {r["id"] for r in second}), "6. 새 설문 매칭", "새 이력 ID 재사용")
    history = call("GET", f"/matching/history/{user_id}", "6. 이전 분석 보존 확인")
    validate_history(history, first + second, "6. 이전 분석 보존 확인")
    for survey_id, results in ((first_id, first), (second_id, second)):
        require(len([r for r in history if r.get("survey_id") == survey_id]) == len(results),
                "6. 이전 분석 보존 확인", "설문별 이력 개수 불일치")
    return {"passed": True, "user_id": user_id, "survey_ids": [first_id, second_id],
            "result_ids": [[r["id"] for r in first], [r["id"] for r in second]],
            "result_counts": [len(first), len(second)], "concurrency_checked": check_concurrency}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=local_base_url, default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=positive_int, required=True)
    parser.add_argument("--timeout", type=positive_timeout, default=10)
    parser.add_argument("--check-concurrency", action="store_true")
    args = parser.parse_args(argv)
    try:
        # Tokens are supplied via environment, never logged or placed in CLI history.
        token = os.getenv("DEMO_ACCESS_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(base_url=args.base_url, timeout=args.timeout, trust_env=False, headers=headers) as client:
            result = run_demo(client, args.user_id, args.timeout, args.check_concurrency)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except DemoError as exc:
        print(f"시연 실패: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
