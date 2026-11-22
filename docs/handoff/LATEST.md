# 인수인계: F-09 산책 완료·평가 (SC-08)

- 날짜: 2026-11-22 (KST)
- 브랜치: `feature/f-09-walk-review`
- 작업 요약: 산책 종료(`COMPLETED`, `INTERRUPTED`) 후 완료 화면용 summary 일괄 제공(경로, 행동마커, 위험신고, 시간, 거리, 보호소 링크, 평가 정보), 다운샘플링 처리, 별점(1~5)·컨디션 태그(7종)·한 줄 코멘트 기반 산책 평가 생성/수정(Upsert) 및 단건 조회, 산책 이력(`GET /users/{id}/walks`) 평가 정보 연동 API 구현 및 전체 회귀 테스트 통과.

## 1. 변경 파일
- `migrations/015_f09_walk_review.sql` (신규): `walk_reviews` 테이블에 `rating`, `comment` 컬럼 추가, `ck_walk_review_rating` 제약 추가, `ck_review_observation` 제약 교체, `walk_review_tags` 테이블 및 인덱스 생성 DDL
- `app/walk_models.py`: `WalkReview` 모델에 `rating`, `comment`, 제약조건 교체 및 `tags` 관계 반영, `WalkReviewTag` 모델 신규 정의
- `app/models.py`: `WalkReviewTag` 모델 export 추가
- `app/walk_review_schemas.py` (신규): `WalkReviewCreate` (별점 1~5, 7종 컨디션 태그 중복 불가 검증, 코멘트 최대 100자), `WalkReviewResponse`, `WalkSummaryResponse`, `SummaryWalkInfo`, `SummaryMetrics`, `SummaryPoint` 스키마 정의
- `app/walk_review_service.py` (신규): `get_walk_summary` (상세 요약 조립, 경로 다운샘플링, `marker_count` 행동마커 전용 카운트, 보호소 링크 정제), `upsert_walk_review` (평가 생성/수정 및 태그 교체), `get_walk_review` (단건 조회, 미작성 시 null)
- `app/routers/walk_reviews.py` (신규): `GET /walks/{walk_id}/summary`, `PUT /walks/{walk_id}/review`, `GET /walks/{walk_id}/review` 엔드포인트 라우터 구현
- `app/walk_map_schemas.py`: `UserWalkItem`에 `rating: int | None = None`, `has_review: bool = False` 필드 추가
- `app/walk_map_service.py`: `user_walks()`에서 `walk.review` eager loading 및 `rating`, `has_review` 매핑
- `app/main.py`: `walk_reviews.router` 등록
- `.env.example`: `WALK_SUMMARY_MAX_POINTS=2000` 환경 변수 추가
- `tests/test_f09_walk_review.py` (신규): F-09 완료 기준 14개 테스트 케이스 작성 (100% 통과)
- `tests/test_walk_domain.py`: `WalkReview`의 `rating`, `comment` 컬럼 및 `ck_walk_review_rating` 제약 추가에 따른 007 DDL 프로젝션 및 스키마 스냅샷 보정
- `docs/TODO.md`: `# F-09 산책 완료·평가 TODO` 추가 및 체크리스트 갱신
- `docs/handoff/LATEST.md`: F-09 인수인계 문서 작성

## 2. 결정 사항
- **DB 스키마 마이그레이션 (`migrations/015_f09_walk_review.sql`)**:
  - 기존 레거시 행(별점 미등록 상태)과의 호환성을 위해 `rating` 컬럼은 DB 레벨에서 `NULL`을 허용(`rating IS NULL OR rating BETWEEN 1 AND 5`)하되, 신규 작성 API(`WalkReviewCreate`)에서는 필수로 강제함.
  - 관찰 지표 제약(`ck_review_observation`)을 `rating IS NOT NULL OR (기존 5개 지표 중 하나 이상)`으로 교체하여 기존 행과 신규 행 모두 만족하도록 함.
  - 컨디션 태그는 `walk_review_tags` 테이블을 신설하고 `(review_id, tag_code)` 유니크 제약 및 7종 코드(`ENERGETIC`, `CALM`, `EXCITED`, `TENSE`, `TIRED`, `SCARED`, `UNWELL`) 체크 제약을 적용함.
- **경로 다운샘플링 정책 (`GET /walks/{walk_id}/summary`)**:
  - `WALK_SUMMARY_MAX_POINTS`(기본값 2000)을 초과하는 산책 좌표는 천장 나눗셈(`step = max(1, -(-total // max_points))`)을 이용해 첫 점(`sequence_no=0`)과 마지막 점(`sequence_no=total-1`)을 반드시 보존하고 균등 간격으로 샘플링하여 반환하며 `is_downsampled=true`로 표시함.
  - 다운샘플링은 지도 렌더링용 `points` 배열에만 적용되며, 총 거리(`total_distance_m`) 및 시간(`duration_seconds`)은 DB 저장값을 그대로 유지함.
- **메트릭 카운트 정합성**:
  - `summary.marker_count`는 순수 행동마커(`walk_markers`) 건수만 카운트하며, F-07 위험 신고(`walk_incidents`)는 포함하지 않음.
- **보호소 외부 링크 (`shelter_link`) 정제**:
  - `shelters.website_url`이 `NULL`, 공백이거나 `http://` 또는 `https://`로 시작하지 않는 유효하지 않은 URL인 경우 안전하게 `null`을 반환함.
- **평가 작성 및 수정(Upsert) 동작**:
  - `PUT /walks/{walk_id}/review` 호출 시 해당 산책의 평가가 없으면 `201 Created`, 기존 평가가 있으면 `200 OK`로 수정 처리.
  - 수정 시 기존 태그는 요청된 `condition_tags`로 완전히 교체됨.
  - "나중에 작성" 기능은 별도 API 없이 평가 행이 생성되지 않은 상태(`review: null`)로 자연스럽게 표현됨.
- **권한 제어 및 보안**:
  - 요약(`summary`) 및 평가(`review`) 조회/작성/수정은 산책을 수행한 봉사자 본인(`actor.id == request.user_id`)만 가능하며, 타 사용자가 접근할 경우 `403 Forbidden`을 반환함.
  - 종료되지 않은 산책(`IN_PROGRESS`, `CANCELLED`)에 대한 요약 또는 평가 시도는 `409 Conflict`를 반환함.

## 3. 실행한 테스트 명령과 결과
```powershell
$env:PYTHONPATH="."; pytest tests/test_f09_walk_review.py tests/test_f08_live_walk.py tests/test_f07_incident_report.py tests/test_walk_recording.py tests/test_f06_walk_tracking.py tests/test_f05_walk_approval.py tests/test_walk_domain.py -v
```

### 테스트 결과 요약
- `tests/test_f09_walk_review.py`: 14 passed (100%)
- `tests/test_f08_live_walk.py`: 8 passed (100%)
- `tests/test_f07_incident_report.py`: 12 passed (100%)
- `tests/test_walk_recording.py`: 38 passed (100%)
- `tests/test_f06_walk_tracking.py`: 11 passed (100%)
- `tests/test_f05_walk_approval.py`: 12 passed (100%)
- `tests/test_walk_domain.py`: 20 passed (79 subtests passed) (100%)
- **총합: 115 passed, 11 warnings, 79 subtests passed in 14.52s (전체 성공)**

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| summary에 경로·행동마커·시간·거리·`marker_count` 포함 | `test_summary_includes_metrics_and_marker_count_excludes_incidents` | 통과 (PASSED) |
| `marker_count`는 행동마커만 센다 (위험 신고 미포함) | `test_summary_includes_metrics_and_marker_count_excludes_incidents` | 통과 (PASSED) |
| `website_url`이 NULL·빈 문자열·http(s) 아님이면 `shelter_link`가 null | `test_shelter_link_formatting` | 통과 (PASSED) |
| 별점 없이 저장하면 422, 1~5 밖도 422 | `test_rating_validation_requires_1_to_5` | 통과 (PASSED) |
| 별점+태그+코멘트 저장 후 summary·이력에서 확인된다 | `test_save_review_reflected_in_summary_and_history` | 통과 (PASSED) |
| 코멘트 101자 422, 알 수 없는 태그 422, 태그 중복 422 | `test_review_payload_validation_errors` | 통과 (PASSED) |
| 평가 없는 산책은 이력에 `has_review=false`, 나중에 작성 가능 | `test_unreviewed_walk_has_review_false_and_can_review_later` | 통과 (PASSED) |
| 저장한 평가 수정 가능 (태그 교체 포함, 200 반환) | `test_update_review_and_replace_tags` | 통과 (PASSED) |
| `COMPLETED`·`INTERRUPTED` 작성 가능, `IN_PROGRESS`·`CANCELLED` 409 | `test_review_status_restrictions` | 통과 (PASSED) |
| 타인 산책 작성·조회 403 | `test_other_user_forbidden` | 통과 (PASSED) |
| 마이그레이션 적용 후 기존 `walk_reviews` 행이 제약에 걸리지 않는다 | `test_legacy_review_without_rating_satisfies_constraints` | 통과 (PASSED) |
| 점이 상한을 넘으면 첫·마지막 점 보존, points_returned <= 상한, is_downsampled=true | `test_points_downsampled_when_exceeding_max` | 통과 (PASSED) |
| 상한 이하이면 전체 점이 반환되고 is_downsampled=false | `test_points_not_downsampled_when_within_max` | 통과 (PASSED) |
| 평가가 없을 때 `GET /walks/{id}/review`는 200 + null | `test_get_review_returns_null_when_not_reviewed` | 통과 (PASSED) |
| 없는 산책 404 반환 | `test_nonexistent_walk_returns_404` | 통과 (PASSED) |
| 기존 F-05~F-08 및 도메인 회귀 테스트 통과 | 전체 115개 테스트 통과 | 통과 (PASSED) |

## 5. 로컬 실행 준비
1. PostgreSQL 마이그레이션 실행:
   ```bash
   psql -d <DB_NAME> -f migrations/015_f09_walk_review.sql
   ```
2. 환경 변수 확인 (`.env`):
   ```env
   WALK_SUMMARY_MAX_POINTS=2000
   ```

## 6. 확인하지 못한 것
- **실제 PostgreSQL 통합 테스트**: 외부 PostgreSQL 접속 환경(`DATABASE_URL`)이 분리되어 있어, SQLite 인메모리 테스트 115건을 통해 전체 비즈니스 로직과 DDL/스키마 계약 정합성을 검증하였습니다.
- **지도 SDK 및 RatingBar UI 렌더링**: 모바일 클라이언트 앱 영역이므로, API JSON 스키마 명세에 맞춘 단위/통합 테스트를 통해 데이터 형식을 검증하였습니다.

## 7. 다음 할 일 및 주의사항 (프론트엔드 연동 참고)
- **산책 종료 후 요약 화면 (SC-08)**:
  1. `PATCH /walks/{walk_id}/end` 호출 후 산책 종료 응답을 받으면 즉시 `GET /walks/{walk_id}/summary`를 호출하여 요약 화면에 필요한 모든 데이터(경로, 마커, 시간, 거리, 보호소 링크, 평가 정보)를 일괄 표시합니다.
  2. `is_downsampled: true`인 경우에도 첫 점과 마지막 점이 보존되어 전체 이동 동선이 지도 폴리라인에 안정적으로 렌더링됩니다.
  3. `shelter_link`가 `null`이 아니면 보호소 웹사이트 방문 배너/버튼을 활성화합니다.
- **평가 작성 및 수정**:
  1. 별점(`rating`, 1~5 정수)은 필수이며, 컨디션 태그(`condition_tags`)는 `ENERGETIC`, `CALM`, `EXCITED`, `TENSE`, `TIRED`, `SCARED`, `UNWELL` 7종 중 선택(중복 불가)할 수 있습니다.
  2. 사용자가 평가를 제출하면 `PUT /walks/{walk_id}/review`를 호출합니다. 신규 생성 시 201, 수정 시 200이 반환됩니다.
  3. "나중에 작성" 버튼 클릭 시 별도 API 호출 없이 요약 화면을 닫으면 되며, 마이페이지 이력(`GET /users/{id}/walks`)에서 `has_review: false`로 표시되어 언제든지 다시 작성할 수 있습니다.
