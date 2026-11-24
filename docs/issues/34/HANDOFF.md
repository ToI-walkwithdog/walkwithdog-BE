# 인수인계: 온보딩 회원가입부터 홈 진입까지 통합 검증 (Issue #34)

- 작성: 2026-11-24 (KST)
- 브랜치: `test/onboarding-e2e` (`origin/dev` `6062200`에서 분기, `main` 모드), PR 대상 `dev`
- 근거: [Issue #34](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/34)(digest `17bbb94b…`), Notion 온보딩 API 상세 명세, FE `walkwithdog-FE` `main` `04e497e`
- 개발 DB와 운영 DB에는 접속하지 않았다. 실제 SMS는 보내지 않았다(MockSmsProvider).

## 1. 범위와 결과 요약

- 새 통합 테스트 `tests/test_onboarding_integration_postgres.py`: 실제 API 라우터와 격리 PostgreSQL로 회원가입부터 홈 진입까지의 흐름, 복구, 우회, 연령 제한, 역할을 검증한다.
- 검증 중 **결함 1건**을 찾아 승인된 범위 확장으로 함께 고쳤다(2절).
- 이미 충분히 검증된 단위 테스트(14세와 120세 경계, 단계별 동시 저장, SMS 한도 세부)는 반복하지 않았다.

## 2. 발견한 결함과 수정

| 항목 | 내용 |
|---|---|
| 증상 | 같은 서약 기록에 대해 `PUT /users/me/pledge`의 `acknowledged_at`은 `…+09:00`, `GET /users/me`의 `pledge_acknowledged_at`은 `…+00:00`이었다. 시점은 같고 표현만 달랐다 |
| 원인 | `app/routers/users.py`의 지역 `as_utc()`(#33)는 시간대가 없는 값에만 UTC를 붙이고, 시간대가 있는 값은 그대로 돌려줬다. PostgreSQL은 `TIMESTAMPTZ`를 세션 시간대로 돌려주므로(로컬 테스트 DB는 `Asia/Seoul`) 서약 API의 응답이 DB 서버 설정에 따라 달라졌다. SQLite 테스트는 시간대를 버려서 드러나지 않았다 |
| 영향 | 서약 API 응답의 시각 표현만. 저장 데이터와 최초 서약 시각은 바뀌지 않았다. 다른 API(`/users/me`, Step 1~3, 산책 신청 서비스)는 이미 UTC로 변환한다 |
| 수정 | 지역 `as_utc()`를 지우고 `onboarding_common.as_utc()`(항상 UTC로 변환)를 쓴다. 응답은 #45에서 정한 UTC ISO 8601이다 |
| 검증 | 통합 흐름 A1, A2의 문자열 비교(서약 응답 = `/users/me`)를 그대로 두고 통과. 새 테스트로 세션 시간대 `Asia/Seoul`과 `UTC` 모두에서 두 API가 같은 `+00:00` 문자열을 주고, 재요청은 최초 시각을 유지하며, 저장 시점이 바뀌지 않음을 확인. 수정 전 함수를 다시 넣으면 새 테스트(E2)가 실패한다. A1과 A2는 DB 서버 기본 시간대가 UTC가 아닐 때(로컬 `Asia/Seoul`)만 함께 실패하고, CI의 PostgreSQL 기본값은 UTC이므로 CI에서 이 결함을 잡는 것은 세션 시간대를 명시하는 E2다 |

## 3. 통합 테스트 목록

| # | 테스트 | 검증 |
|---|---|---|
| A1 | `test_full_onboarding_without_dog_experience_on_the_migrated_schema` | 중복 확인, 회원가입과 동의, 로그인, `PLEDGE`, 서약, SMS 발송과 확인, 기본 정보, 경험 없음, 지역 목록, 수동 지역 선택, 산책 루틴(`UP_TO_30_MIN`), 완료, 로그아웃 뒤 기존 토큰 401, 재로그인 뒤 같은 상태. 각 단계 응답의 다음 단계가 `/users/me`와 같고 서약 이후 `PLEDGE`가 다시 나오지 않음. 단계별 DB 행 수 |
| A2 | `test_full_onboarding_with_dog_experience_on_the_migrated_schema` | 경험 있음(단일 `dog_size_code`, 어려웠던 상황 2개), 시도 3곳(세종 전체 포함), 시간대 4개, `BETWEEN_30_AND_60_MIN` |
| B1 | `test_logging_in_again_after_each_step_restores_the_next_step` | 단계마다 로그아웃과 재로그인(앱 종료 가정) 후 `PLEDGE`, `BASIC_INFORMATION`, `PET_EXPERIENCE`, `WALKING_ROUTINE`, `null` 복원 |
| C1 | `test_lost_responses_are_retried_and_different_answers_get_409` | 서약과 세 단계의 같은 요청 재시도 200(최초 시각), 다른 내용 409 세 종류, 행 수 불변 |
| C2 | `test_a_step1_answer_that_fails_after_its_commit_is_recovered_by_the_retry` | Step 1의 다음 단계 계산(#45, 커밋 뒤)이 실패하면 500이지만 기록은 커밋됨. `/users/me`는 `PET_EXPERIENCE`, 같은 요청 재시도는 최초 시각의 200, 인증 소비 시각은 저장 시각과 같음 |
| D1 | `test_skipping_steps_gets_the_first_incomplete_step_and_stores_nothing` | 서약 전 SMS, Step 1 조회, Step 2, Step 3은 409 `PLEDGE`(SMS 미발송), 서약 뒤 Step 2와 3은 `BASIC_INFORMATION`, Step 1 뒤 Step 3은 `PET_EXPERIENCE`. 모두 `/users/me`와 같음 |
| E1 | `test_a_new_pledge_version_mid_onboarding_is_asked_before_the_next_step` | Step 1 뒤 서약 버전 변경: `PLEDGE` 복귀, Step 1 조회와 Step 2는 409 `PLEDGE`, 재서약 후 완료, 두 버전 기록 보존 |
| E2 | `test_pledge_time_is_the_same_utc_string_whatever_the_session_time_zone` | 2절의 결함 회귀 테스트 |
| F1 | `test_a_failed_login_after_sign_up_keeps_the_account_and_needs_no_new_sign_up` | 틀린 비밀번호 401 `INVALID_CREDENTIALS`와 세션 없음, 같은 아이디 재가입 409, 동의 행 유지, 정상 로그인 후 `PLEDGE` |
| G1 | `test_an_sms_failure_is_recovered_by_a_new_send` | 발송 실패 503 `SMS_DELIVERY_UNAVAILABLE`(`FAILED` 기록), 그 기록 확인 409 `VERIFICATION_NOT_SENT`, 60초 뒤 재발송으로 Step 1 완료, 일일 횟수 2(실패 포함) |
| G2 | `test_an_empty_region_master_blocks_the_choice_until_regions_are_loaded` | 지역 마스터가 비면 시도 목록 503 `REGION_DATA_UNAVAILABLE`, 루틴 저장 422 `INVALID_REGION_CODE`(행 없음), 적재 후 완료 |
| H1 | `test_an_under_14_birth_date_blocks_onboarding_until_the_correction` | 서약 후 만 14세 미만: 422 `AGE_BELOW_MINIMUM`, `AGE_REVIEW`, 프로필 미저장, 서약과 SMS와 Step 2는 403 `AGE_REVIEW_REQUIRED`, Step 3은 403 `ONBOARDING_NOT_ALLOWED`. 정정 후 `ACTIVE`, `age_review_required_at=NULL`, 서약 유지, SMS부터 다시 진행해 완료 |
| H2 | `test_an_exit_of_an_age_review_account_revokes_every_device` | 두 기기 로그인 후 가입 종료(테스트 환경 정책 `delete`): 두 토큰 모두 401, 로그인 401, 사용자, 세션, 동의, 서약, 인증 기록 정리 |
| I1 | `test_shelter_and_admin_log_in_by_id_and_are_not_onboarding_targets` | 보호소와 관리자: 아이디 로그인, `email` 없음, 온보딩 필드 `null`과 서약 필드 유지, 서약 403 `PLEDGE_NOT_ALLOWED`, 온보딩 API 403 `ONBOARDING_NOT_ALLOWED`, 기록 없음 |
| K1 | `test_a_step1_failure_after_the_insert_rolls_back_on_postgres` | 기본 정보 INSERT와 인증 소비 UPDATE가 PostgreSQL에 반영된 뒤(flush 후 같은 트랜잭션에서 1행씩 확인) COMMIT이 실패: 500(`OperationalError`), 기본 정보 행 없음, 인증 소비 취소, 재시도 성공 |
| H3 | `test_an_exit_is_refused_and_keeps_everything_when_activity_must_be_retained` | 보존 대상 활동(설문)이 있는 연령 확인 계정의 가입 종료: 기록 삭제 정책(`delete`)이어도 409 `ACCOUNT_HAS_RETAINED_ACTIVITY`, 사용자, 세션, 동의, 서약, 인증, 설문 행이 모두 그대로, 세션 계속 사용 가능 |

## 4. 테스트 환경

- `SIGNUP_TEST_DATABASE_URL`: 빈 로컬 격리 DB `toi_walk_test_*`. CI의 같은 그룹에서 실행된다. 테스트마다 `su_<uuid>` 스키마를 만들고 지운다. 클래스 시작과 끝에 DB가 비어 있는지 확인한다.
- 주요 흐름은 `create_all()` 없이 실제 SQL로 만든 스키마에서 실행한다. 016 이전 dev 기본 테이블과 `005_user_roles`, `006_user_shelters`, `007_walk_domain`, `014_f08_live_walk`, `015_f09_walk_review` 다음에 `002_auth_sessions`, `016_onboarding_signup`, `017_remove_user_email`, `018_user_consents`, `019_first_walk_guide`, `023_user_pledge_acknowledgements`, `024_onboarding_basic_info`, `025_onboarding_dog_experience`, `022_regions`, `026_onboarding_walking_routine`을 적용한다. 각 파일의 manifest `requires`가 이미 적용됐는지와 모델 컬럼이 일치하는지 확인한다.
- 가입 종료(H2, H3)는 채팅과 매칭 활동 테이블을 읽으므로 모델 스키마(`create_all`)에서 실행한다. 이 테이블은 이 SQL 체인이 만들지 않는 기본 스키마다.
- 설정: `DATABASE_URL=sqlite://`(가져오기용), `PLEDGE_CURRENT_VERSION=pledge-test-v1`, 테스트용 `PHONE_VERIFICATION_HMAC_KEY`, 테스트용 개인정보 문서 버전, 주입 시계. H2만 `APP_ENV=test`, `ACCOUNT_EXIT_RECORD_POLICY=delete`.
- 요청 제한 의존성은 이 파일에서만 끈다(`test_rate_limit`에서 따로 검증).

## 5. 기존 테스트 범위 구분

| 구분 | 항목 |
|---|---|
| 기존 테스트로 검증 완료 | 가입과 동의 원자성, 선가입과 동시 가입, 필수 동의와 문서 버전, 토큰 만료, 동시 서약과 단계별 동시 저장(PG), SMS 실패, 시간 초과, 한도 세부, 만 14세와 120세 경계와 121세 거부, 정정 세부 규칙, 서약 전 `PUT /basic-info` 409(SQLite), 403 접근 분류 |
| 이번에 통합으로 추가 | 3절 전체 |
| 현재 미구현으로 검증 불가 | 현재 위치 판별(#46)과 위치 공급자 장애, 실제 SMS 공급자. A1과 A2는 `POST /regions/resolve-location` 없이 수동 지역 선택만으로 완료되므로 위치 판별이 온보딩 완료의 필수 조건이 아님을 보여 준다 |

기존 실패 5건(`test_walk_domain` 3건, `test_walk_settings_api` 1건과 하위 1건)은 #66 범위로 두고 신규 회귀와 구분한다.

## 6. 명세 불일치와 미확정 정책

테스트는 현재 구현의 동작을 검증하고 차이를 여기에 남긴다.

1. 401 코드 집합이 명세마다 다르다(Step 3 명세 2종, 위치 판별 1종). 구현은 공통 의존성의 4종이다.
2. 연령 확인 계정의 403이 Step 3만 `ONBOARDING_NOT_ALLOWED`, 나머지는 `AGE_REVIEW_REQUIRED`다(#45 통일안).
3. 다음 단계 필드 이름: Step 1 `next_step`, 그 외 `next_onboarding_step`.
4. 가입 종료 시 동의와 서약 기록 보존 정책이 미확정이다(#41). 운영 정책에서는 해당 기록이 있으면 409다. #34는 테스트 환경의 `delete` 정책으로만 정리를 검증했다.
5. 시각 표기: 구현은 UTC, Notion 예시는 +09:00.
6. FE: 회원가입 응답에 토큰이 없는데 FE는 가입 화면(1-3) 다음 바로 서약(1-6)으로 간다. 로그인 후 바로 홈으로 간다. 연동 때 로그인 단계와 `/users/me` 분기가 필요하다.
7. 서약 버전 관리 위치(#33), 실제 SMS 공급자, 실제 지역 마스터 적재는 미정이거나 미구현이다.
