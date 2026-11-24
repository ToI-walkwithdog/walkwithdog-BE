# 인수인계: 온보딩 Step 3 산책 루틴 조회와 저장 (Issue #44)

- 작성: 2026-11-24 (KST)
- 브랜치: `issue/44` (`origin/dev` `44cc859`에서 분기), PR 대상 `dev`
- 근거: [Issue #44](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/44) (본문 digest `2821b61f…`), Notion "Step 3 산책 루틴 저장"(2026-11-24 수정)과 "Step 3 산책 루틴 조회"
- 개발 DB와 운영 DB에는 접속하지 않았다. SQL은 격리 PostgreSQL에서만 검증했다.

## 1. FE 연동 요약

### 코드와 구간

| 항목 | 요청 필드 | 개수 | 코드 | 의미 |
|---|---|---|---|---|
| 산책 가능 지역 | `region_codes` | 1~3개, 중복 불가 | 지역 조회 API의 `region_code` | 활성 시군구(`SIGUNGU`) 또는 세종 전체 `36_SPECIAL_ALL`. 여러 시도를 섞을 수 있다. 시도 코드(`11`, 세종 `36` 등)는 거부한다 |
| 가능 시간대 | `available_time_slot_codes` | 1~4개, 중복 불가 | `WEEKDAY_MORNING`, `WEEKDAY_AFTERNOON`, `WEEKEND_MORNING`, `WEEKEND_AFTERNOON` | 오전 06:00 이상 12:00 미만, 오후 12:00 이상 22:00 미만 |
| 한 번에 가능한 산책 시간 | `walking_duration_code` | 1개 | `UP_TO_30_MIN` | 30분 이하(30분 포함). FE `30분 이내` |
| | | | `BETWEEN_30_AND_60_MIN` | 30분 초과 60분 미만. FE `30분 ~ 1시간` |
| | | | `OVER_60_MIN` | 60분 이상. FE `1시간 이상` |
| 편하게 산책할 수 있는 크기 | `comfortable_dog_size_code` | 1개 | `SMALL`, `MEDIUM`, `LARGE` | 체중 기준은 미확정. 화면의 kg 문구는 정책값이 아니다 |

- **산책 시간 코드 변경**: 이전 Issue 초안의 `UNDER_30_MIN`(30분 미만)은 쓰지 않는다. 2026-11-24 Notion 저장 명세를 따라 `UP_TO_30_MIN`(30분 이하)으로 확정했고, `BETWEEN_30_AND_60_MIN`은 30분 초과 60분 미만이다. `UNDER_30_MIN`을 보내면 422 `INVALID_DURATION`이 된다.
- 경계값은 서버의 `app.walking_routine_service.walking_duration_code(minutes)` 한 곳에 정의했다. 30분은 `UP_TO_30_MIN`, 30분 초과 60분 미만은 `BETWEEN_30_AND_60_MIN`, 60분은 `OVER_60_MIN`이다. API는 분이 아니라 코드를 받는다.
- 지역 표시명은 응답하지 않는다. FE는 지역 조회 API(`GET /regions/sido`, `GET /regions/sigungu`)의 이름을 쓴다.
- 위치 판별과 수동 선택은 구분하지 않는다. 최종 선택한 코드 배열만 보낸다. 좌표, 선택 출처, 사용자 ID, 완료 시각은 보내지 않는다. 보내도 무시하고 저장하지 않는다.
- 위치 권한이 없어도 수동으로 지역을 1개 이상 고르면 저장할 수 있다.

### `PUT /users/me/onboarding/walking-routine`

요청 예시

```json
{"region_codes": ["11680", "11650"], "available_time_slot_codes": ["WEEKDAY_MORNING", "WEEKEND_AFTERNOON"],
 "walking_duration_code": "BETWEEN_30_AND_60_MIN", "comfortable_dog_size_code": "MEDIUM"}
```

성공 200 `{"code":"OK","data":{"step3_completed":true,"completed_at":"<UTC ISO 8601>","next_onboarding_step":null},"message":"산책 루틴 입력이 완료되었습니다."}`

- `next_onboarding_step`은 공통 함수 계산값이다. 서약, Step 1~3이 모두 완료된 ACTIVE 계정이면 `null`이다. Step 3 저장 후 서약 화면으로 돌아가지 않는다.
- 같은 입력을 다시 보내면 최초 완료 시각과 함께 200을 돌려준다. 배열 순서는 상관없다. 응답이 유실된 뒤 재시도해도 결과가 같다.
- 완료 후 다른 입력은 409 `WALKING_ROUTINE_ALREADY_COMPLETED`다. 수정은 별도 API(마이페이지 산책 선호, 범위 밖)에서 다룬다.

### `GET /users/me/onboarding/walking-routine`

- 미완료: 200, `{"step3_completed": false, "routine": null, "next_onboarding_step": "WALKING_ROUTINE"}`
- 완료: 200, `routine`에 `region_codes`(코드순), `available_time_slot_codes`(위 표 순서), `walking_duration_code`, `comfortable_dog_size_code`, `completed_at`
- `Cache-Control: no-store`. DB를 바꾸지 않는다.

### 오류 (응답 `{code, data, message}`)

| HTTP | code | data | 조건 |
|---|---|---|---|
| 401 | `AUTHENTICATION_REQUIRED`, `INVALID_ACCESS_TOKEN`, `SESSION_EXPIRED` | `{}` | 공통 인증 의존성(#32) |
| 403 | `ONBOARDING_NOT_ALLOWED` | `{}` | 보호소, 관리자, `AGE_REVIEW_REQUIRED` 계정 |
| 409 | `PREVIOUS_STEP_NOT_COMPLETED` | `{"required_step": "PLEDGE" \| "BASIC_INFORMATION" \| "PET_EXPERIENCE"}` | 조회와 저장 모두. 첫 미완료 단계 |
| 409 | `WALKING_ROUTINE_ALREADY_COMPLETED` | `{}` | 완료 후 다른 입력 |
| 422 | `REGIONS_REQUIRED` | `{"field":"region_codes"}` | 누락, null, 빈 배열 |
| 422 | `REGION_LIMIT_EXCEEDED` | `{"field":"region_codes","max":3}` | 4개 이상 |
| 422 | `INVALID_REGION_CODE` | `{"field":"region_codes"}` | 배열 아님, 문자열 아님, 중복, 미존재, 비활성, 비활성 시도 아래, 시도 수준 코드 |
| 422 | `TIME_SLOTS_REQUIRED` | `{"field":"available_time_slot_codes"}` | 누락, null, 빈 배열 |
| 422 | `INVALID_TIME_SLOT` | `{"field":"available_time_slot_codes"}` | 5개 이상, 중복, 알 수 없는 코드 |
| 422 | `DURATION_REQUIRED`, `INVALID_DURATION` | `{"field":"walking_duration_code"}` | 누락이나 공백, 고정 코드 외 |
| 422 | `DOG_SIZE_REQUIRED`, `INVALID_DOG_SIZE` | `{"field":"comfortable_dog_size_code"}` | 누락이나 공백, 고정 코드 외 |
| 500 | `INTERNAL_SERVER_ERROR` | `{}` | DB 오류. 전체 롤백 |

- 메시지는 Notion 명세 문구다. 409 `PREVIOUS_STEP_NOT_COMPLETED`의 메시지는 공통 함수의 단계별 문구를 쓴다(`PET_EXPERIENCE`는 Notion의 "이전 단계를 먼저 완료해 주세요.").
- 검사 순서(Issue #44 서버 처리 순서)
  1. 인증(401)
  2. 역할과 계정 상태(403)
  3. 서약, Step 1, Step 2(409)
  4. 네 필드의 존재와 개수
  5. 중복과 고정 코드
  6. 지역 마스터
  7. 기존 완료 기록 비교
  - 예를 들어 지역 코드가 중복되고 산책 시간이 비어 있으면 `DURATION_REQUIRED`가 먼저 나온다.

## 2. 서버 구현

| 파일 | 내용 |
|---|---|
| `app/models.py` | `TIME_SLOT_CODES`, `WALKING_DURATION_CODES`, 모델 3개. `app.region_models`를 같은 메타데이터에 등록(지역 FK) |
| `app/walking_routine_service.py` | 본문 검증, 경계 함수, 조회, 저장 트랜잭션 |
| `app/routers/onboarding_routine.py` | 두 라우트와 요청 모델. 의존성에서 401, 403, 409 확인 |
| `app/onboarding_common.py` | `first_incomplete_step()`에 루틴 확인 추가. 모두 완료면 `None`. `PET_EXPERIENCE` 409 문구 추가 |
| `app/age_review_service.py` | 가입 종료 삭제 순서에 Step 3 연결 행, 루틴 행 추가(FK 순서) |
| `app/main.py` | 라우터 등록 |

저장 트랜잭션(서약, Step 1, Step 2와 같은 규칙)

1. users 행을 `FOR SHARE`로 잠그고 잠근 행의 상태로 역할, ACTIVE, 이전 단계를 다시 확인한다. 동시에 진행 중인 연령 확인 전환(`FOR UPDATE`)은 이 저장이 끝날 때까지 기다린다. 먼저 커밋된 전환은 403으로 거부한다.
2. 지역을 `require_selectable_regions(lock=True)`로 확인한다. PostgreSQL에서는 선택한 지역과 상위 시도 행을 `FOR SHARE`로 잠근다. 지역 동기화가 같은 코드를 비활성으로 바꾸려 하면 이 저장의 커밋을 기다린다. 먼저 커밋된 비활성화는 422로 거부한다.
3. 기존 완료 기록과 비교해 같으면 200(최초 시각), 다르면 409를 돌려준다.
4. 루틴 1행, 지역 1~3행, 시간대 1~4행을 저장하고 커밋한다. 어느 행이든 실패하면 전체 롤백한다.

- 같은 계정의 동시 저장은 PK가 한 건만 남긴다. 늦은 요청은 저장된 값을 다시 읽어 200 또는 409로 답한다. 연결 테이블은 복합 PK라 중복 행이 생기지 않는다.
- `step3_completed` 컬럼은 두지 않는다. 루틴 행이 있으면 완료다.
- 연령 확인 필요 계정의 403 코드: 이 API는 Notion대로 `ONBOARDING_NOT_ALLOWED`를 쓴다. 반려견 경험 API는 `AGE_REVIEW_REQUIRED`를 쓴다. 접근 제어 테스트(`tests/test_age_review_access.py`)에서 두 경로를 별도 분류로 고정했다.

## 3. DB (`migrations/026_onboarding_walking_routine.sql`)

- `volunteer_onboarding_routines`: `user_id` INTEGER PK 겸 `users` FK, `walking_duration_code` VARCHAR(30), `comfortable_dog_size_code` VARCHAR(20), `completed_at`, `updated_at` TIMESTAMPTZ, 모두 NOT NULL. CHECK `ck_onboarding_routine_duration`, `ck_onboarding_routine_dog_size`
- `volunteer_routine_regions`: 복합 PK `(user_id, region_code)`, 루틴 FK, `regions` FK
- `volunteer_routine_time_slots`: 복합 PK `(user_id, time_slot_code)`, 루틴 FK, CHECK `ck_routine_time_slot_code`
- manifest 정식 등록: `requires` `022_regions.sql`, `025_onboarding_dog_experience.sql`, 트랜잭션 `file`, 체크섬 `e1ec4187…e029`
- 추가 전용이다. 기존 테이블과 행은 바꾸지 않는다. 재실행해도 바뀌지 않는다. 롤백 SQL은 파일 머리말에 있다.
- **기동 순서**: 이 코드는 다음 온보딩 단계를 계산할 때 `volunteer_onboarding_routines`를 읽는다. 022와 026을 적용하기 전에 기동하면 Step 2를 마친 계정의 연령 확인 정정, Step 2 조회, Step 3 API가 500이 된다. 실제 DB 적용은 프로젝트 정책대로 모든 기능 PR 병합 뒤 통합 마이그레이션에서 진행한다. 그 전에는 공용 서버에 배포하지 않는다.
- 격리 PostgreSQL 테스트의 마이그레이션 체인(`API_MIGRATIONS_AFTER_018`)에 022와 026을 025 뒤에 추가했다. 022는 선행 SQL이 없으므로 어느 단계 뒤에 붙여도 된다.

## 4. 검증

| 범위 | 파일 | 내용 |
|---|---|---|
| 기능 | `tests/test_walking_routine.py` | 정상(지역 1개와 3개, 여러 시도, 세종 전체, 시간대 1개와 4개, 조회 2가지, 재저장), 422 전체와 순서, 지역 마스터 거부, 409 선행 단계(조회와 저장), 403, 401, 잠금 직전 상태 변경, 연결 행 실패 롤백, 단계 계산, 30분과 60분 경계, DB CHECK, `UNDER_30_MIN` 잔존 검사 |
| PostgreSQL | `tests/test_signup_postgres.py` | 공유 잠금 대기, 상태 변경 뒤 거부, 동일 입력 병렬 저장, 다른 입력 병렬 저장, 연결 행 실패 롤백, 지역 비활성화 경합, DB 제약, 026 단독 적용과 재실행, SQL CHECK와 API 코드 일치, 가입 종료 삭제와 롤백 |
| 회귀 | `tests/test_age_review.py`, `tests/test_age_review_access.py` | 가입 종료 시 Step 3 행 삭제와 정책상 보존, 새 경로 접근 분류 |

실행 결과는 PR 본문에 적는다.

## 5. 남은 결정과 후속

[TODO.md](TODO.md)를 본다.
