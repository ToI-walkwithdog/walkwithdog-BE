# 인수인계: 온보딩 Step 2 반려견 경험 조회와 저장 (Issue #42)

- 작성: 2026-11-23 (KST). 이 문서는 구현 후에 작성했다. 코드와 테스트는 바꾸지 않았다.
- 브랜치: `feature/onboarding-dog-experience`, 기능 커밋 `188da56` (`feature/onboarding-age-review`의 `1663aa3`에서 분기)
- PR: [Draft PR #57](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/57), 대상 브랜치 `feature/onboarding-age-review`(#53)
- 근거: [Issue #42](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/42)의 "확정 결정 (2026-11-23 사용자 결정)", Notion [Step 2 반려견 경험 저장](https://app.notion.com/p/420b7bbac8c781c884a8f1cc9f33c3c8), [Step 2 반려견 경험 조회](https://app.notion.com/p/420b7bbac8c781d1b839e4d5c8b09116)
- 상태: `dev` 미병합, 개발 DB 미적용

## 1. 개요와 범위

일반 사용자가 반려견을 키우거나 돌본 경험을 입력하면 Step 2가 완료된다. 경험이 없다고만 답해도(`{"has_experience": false}`) 완료다.

| 구분 | 내용 |
|---|---|
| 포함 | 조회와 저장 API, 경험 테이블과 어려웠던 상황 테이블, 서약과 Step 1 선행 검증, 계정 상태 검증, 멱등 처리, 동시성, `first_incomplete_step()`의 `PET_EXPERIENCE` 판정, #41 가입 종료 삭제 순서 연동, 접근 제어 자동 테스트 분류 |
| 제외 | 매칭 설문(`surveys.dog_experience`) 연동, Step 3 산책 루틴, 완료 후 수정, FE 화면 구현 |

`surveys`와 `volunteer_profiles`는 읽거나 쓰지 않는다.

## 2. 주요 변경 파일

| 파일 | 내용 |
|---|---|
| `app/dog_experience_service.py` | 입력 검증, 조회, 저장, 재요청 비교 |
| `app/routers/onboarding_experience.py` | API 2개, `get_step2_user`, `DogExperienceRequest` |
| `app/models.py` | 코드 집합 4개, `VolunteerOnboardingExperience`, `VolunteerExperienceChallenge` |
| `app/onboarding_common.py` | `first_incomplete_step()`에 `PET_EXPERIENCE` 판정 |
| `app/age_review_service.py` | 가입 종료 삭제 순서에 Step 2 테이블 추가 |
| `app/main.py` | 라우터 등록 |
| `migrations/021_onboarding_dog_experience.sql` | 새 테이블 2개 |
| `tests/test_dog_experience.py`, `tests/test_age_review.py`, `tests/test_age_review_access.py`, `tests/test_signup_postgres.py` | 테스트 |

## 3. API 계약

두 API 모두 `{code, data, message}` 형식이다. 검사 순서: Bearer 인증(401 `AUTHENTICATION_REQUIRED`, `INVALID_ACCESS_TOKEN`, `SESSION_EXPIRED`, `USER_NOT_FOUND`) → `role=user`(403 `ONBOARDING_NOT_ALLOWED`) → 계정 상태(403 `AGE_REVIEW_REQUIRED`) → 서약과 Step 1(409 `PREVIOUS_STEP_NOT_COMPLETED`, `data.required_step`은 `PLEDGE` 또는 `BASIC_INFORMATION`) → 본문(422, 저장만) → 기존 기록 비교 → 저장. 공통으로 500 `INTERNAL_SERVER_ERROR`가 있다.

### GET /users/me/onboarding/dog-experience

- 미완료: 200 `{"step2_completed": false, "experience": null, "next_onboarding_step": "PET_EXPERIENCE"}`
- 완료: 200 `step2_completed=true`, `experience`(`has_experience`, `experienced_breeds_text`, `dog_size_code`, `walking_frequency`, `perceived_difficulty`, `challenge_codes`, `completed_at`), `next_onboarding_step=WALKING_ROUTINE`
- 경험 없음의 상세 값은 `null`, `challenge_codes`는 `[]`다. `challenge_codes`는 코드 정의 순서로 정렬해 돌려준다.
- 메시지 "반려견 경험 정보를 조회했습니다.", `Cache-Control: no-store`, DB 쓰기 없음

### PUT /users/me/onboarding/dog-experience

| 필드 | 타입 | 필수 | 검증 |
|---|---|---|---|
| `has_experience` | boolean | Y | JSON boolean만 허용 |
| `experienced_breeds_text` | string | 경험 있음일 때 | NFC 정규화, 앞뒤 공백 제거, 1~200자, 출력 가능한 문자만. `모름`과 여러 견종을 한 문자열로 허용 |
| `dog_size_code` | string | 경험 있음일 때 | `SMALL`, `MEDIUM`, `LARGE` 중 1개 |
| `walking_frequency` | string | 경험 있음일 때 | `DAILY`, `WEEKLY_3_4`, `WEEKLY_1_2`, `RARELY` |
| `perceived_difficulty` | string | 경험 있음일 때 | `EASY`, `NORMAL`, `HARD` |
| `challenge_codes` | array[string] | N | `LEASH_PULLING`, `BARKING_EXCITEMENT`, `OTHER_DOG_REACTION`, `NONE`. 중복 불가, `NONE`은 단독. `[]`(선택 없음)와 `["NONE"]`(특별히 없음)을 구분 |

- 성공 200 `{"step2_completed": true, "completed_at": "...", "next_onboarding_step": "WALKING_ROUTINE"}`, 메시지 "반려견 경험 입력이 완료되었습니다."
- 경험 없음은 `{"has_experience": false}`만으로 완료다. 상세 값은 저장하지 않는다.

422 순서와 코드

1. `EXPERIENCE_REQUIRED` (`has_experience` 누락, boolean 아님, 본문 없음)
2. 경험 없음: 상세 값이 있으면 `EXPERIENCE_DETAILS_NOT_ALLOWED`(`data`는 `{}`). `null`, 빈 문자열, 공백 문자열, `[]`는 상세 값으로 보지 않는다.
3. 경험 있음: 견종(`EXPERIENCED_BREED_REQUIRED`, `INVALID_EXPERIENCED_BREED`) → 크기(`DOG_SIZE_REQUIRED`, `INVALID_DOG_SIZE`) → 빈도(`WALKING_FREQUENCY_REQUIRED`, `INVALID_WALKING_FREQUENCY`) → 난이도(`DIFFICULTY_REQUIRED`, `INVALID_DIFFICULTY`) → 어려웠던 상황(`INVALID_CHALLENGE`, `CHALLENGE_NONE_CONFLICT`)

- 필드 오류의 `data`는 `{"field": "<필드 이름>"}`이다.
- 409 `DOG_EXPERIENCE_ALREADY_COMPLETED`: 완료 후 다른 내용
- **알 수 없는 필드는 무시한다**(`extra="ignore"`). 예전 필드 `dog_size_codes`도 무시하므로, `{"has_experience": false, "dog_size_codes": ["SMALL"]}`은 현재 200으로 완료된다. 거부 여부는 미확정이다(아래 11절).

## 4. 반려견 크기: 단일 `dog_size_code` (2026-11-23 사용자 결정)

- Notion 저장, 조회 명세는 `dog_size_codes: string[]`와 연결 테이블 `volunteer_experience_sizes`로 적혀 있다.
- Issue #42의 사용자 결정으로 단일 선택 `dog_size_code: string`으로 바꿨고 연결 테이블은 만들지 않았다. Notion 오류가 아니라 새 결정에 따른 변경이며, Notion 갱신은 Notion 담당자 몫으로 남아 있다.
- `origin/dev`에 크기 연결 테이블 관련 모델, 마이그레이션, 코드가 없어 데이터 이전은 필요 없다(Issue #42 확인 기록).

## 5. DB와 마이그레이션

`migrations/021_onboarding_dog_experience.sql` (한 트랜잭션, 새 테이블 2개만 추가, 재실행 시 변경 없음, 되돌리는 SQL은 파일 주석)

`volunteer_onboarding_experiences` (행이 있으면 Step 2 완료)

| 컬럼 | 타입 | 제약 |
|---|---|---|
| `user_id` | INTEGER | PK, FK `users(id)` (`fk_onboarding_experiences_user_id`, NO ACTION) |
| `has_experience` | BOOLEAN | NOT NULL |
| `experienced_breeds_text` | VARCHAR(200) | NULL |
| `dog_size_code` | VARCHAR(10) | CHECK `ck_onboarding_experience_dog_size` |
| `walking_frequency` | VARCHAR(20) | CHECK `ck_onboarding_experience_frequency` |
| `perceived_difficulty` | VARCHAR(20) | CHECK `ck_onboarding_experience_difficulty` |
| `completed_at`, `updated_at` | TIMESTAMPTZ | NOT NULL |

- `ck_onboarding_experience_details`: 경험 있음이면 견종, 크기, 빈도, 난이도가 모두 NOT NULL, 경험 없음이면 모두 NULL
- `volunteer_experience_challenges`: `user_id` INTEGER, `challenge_code` VARCHAR(30), 복합 PK `pk_volunteer_experience_challenges (user_id, challenge_code)`, CHECK `ck_experience_challenge_code`, FK `fk_experience_challenges_experience` → `volunteer_onboarding_experiences(user_id)`
- `NONE` 배타성과 "경험 없음이면 상황 행 없음"은 서버 검증으로 보장한다(DB 제약 아님).
- `step2_completed` 컬럼은 두지 않고 메인 행 존재로 계산한다.

## 6. 처리 흐름, 트랜잭션, 잠금

`save_experience()`

1. 기존 기록이 있으면 재요청 비교: 같은 내용이면 최초 `completed_at`으로 200, 다르면 409. 비교에는 크기와 어려웠던 상황 집합(순서 무관)이 포함된다.
2. 트랜잭션: `lock_user()`로 `users` 행 `FOR SHARE`(`populate_existing()`으로 잠금 시점 값) → `ensure_active()` → `first_incomplete_step()`로 현재 버전 서약과 Step 1 재확인(409) → 메인 행 삽입, `flush` → 어려웠던 상황 행 삽입 → 커밋
3. `IntegrityError`: 롤백 후 다시 읽어 동시 요청이 먼저 저장했으면 1번과 같이 200 또는 409. 기록이 없으면 500
4. 그 밖의 예외: 전체 롤백 후 500

- 요청 시작 시 검사(`get_step2_user`)와 잠긴 행 재확인을 모두 한다. 계약 형식 API라 `detail` 403을 주는 `require_active_writer()`는 쓰지 않고, #40 Step 1 저장과 같은 `lock_user()`와 계약 형식 `ensure_active()` 조합을 썼다.
- 잠금 규칙은 #33, #40, #41과 같다: `users` 행 먼저. Step 1 상태 전환(`FOR UPDATE`)과 이 저장(`FOR SHARE`)은 서로 기다린다.
- 격리 수준은 기본 READ COMMITTED다.

## 7. 멱등성, 동시성, 롤백

- 같은 사용자의 동시 저장: 메인 PK와 상황 복합 PK로 한 벌만 남는다. 늦은 요청은 같은 답이면 200(같은 완료 시각), 다르면 409
- 응답 유실 후 같은 입력 재시도: 200, 같은 완료 시각
- 어려웠던 상황 행 저장 실패: 메인 행까지 롤백, 재시도하면 성공
- 완료 후에는 이 API로 경험 유무나 크기를 바꿀 수 없다.

## 8. #41 가입 종료와의 관계

- 두 테이블은 `users`를 직접 또는 경험 테이블을 거쳐 참조한다(모두 NO ACTION, CASCADE 없음).
- 가입 종료 삭제 순서: `auth_sessions` → `session_tokens` → `phone_verifications` → `phone_verification_daily_counts` USER 행 → `volunteer_experience_challenges` → `volunteer_onboarding_experiences` → `volunteer_onboarding_basics` → `user_pledge_acknowledgements` → `user_consents` → `users`
- Step 2 기록은 동의, 서약처럼 보존 정책 대기 대상이 아니라 기본 정보와 같은 온보딩 기록으로 분류했다.
- 운영 환경의 가입 종료 차단(`ACCOUNT_EXIT_RECORD_POLICY`, `APP_ENV`)과 보존 정책은 바꾸지 않았다.
- 정상 흐름에서 연령 확인 필요 계정은 Step 2 기록을 가질 수 없다(Step 2는 `ACTIVE`와 Step 1 완료가 전제). 예외적으로 있으면 FK 순서로 삭제한다.
- 021 적용 전에는 Step 2 API와 함께 가입 종료도 실패한다(삭제 대상 테이블이 없음).

## 9. 접근 제어 자동 테스트

- 두 경로를 분류하지 않은 상태에서 `tests/test_age_review_access.py`가 실패해 누락을 알려 주는 것을 확인했다.
- 두 경로를 계약 형식 차단으로 분류했다. 분류 수: 허용 5, 계약 차단 7, `detail` 차단 45, 공개 12.
- 이 테스트는 목록에 없는 경로를 모두 `detail` 차단으로 간주해 호출한다. 계약 형식 경로는 응답 형식이 달라 명시 분류가 필요했다. `get_current_user`를 쓰는 새 경로는 따로 분류하지 않아도 검사에 들어간다.
- 계약 형식 경로에 미인증 401과 보호소, 관리자 403 검사를 추가했다.

## 10. 테스트 결과 (PR #57 본문 기록 인용. 이번 문서 작업에서 다시 실행하지 않음)

실행 환경: Windows 11, Python 3.12 `.venv`, SQLite, 격리 PostgreSQL 16.16 테스트 DB(`SIGNUP_TEST_DATABASE_URL`)

| 실행 | 결과 |
|---|---|
| `tests/` 전체 | 567 passed, 2 failed, 82 skipped |
| 선행 #53 기준 | 545 passed, 2 failed, 82 skipped |
| `tests/test_dog_experience.py` | 12 passed |
| `tests/test_signup_postgres.py` Step 2 | 8 passed, 경합 테스트 3회 반복 통과 |
| `tests/test_age_review.py` | 21 passed |
| `tests/test_age_review_access.py` | 8 passed, 303 subtests |

- 실패 2건은 선행 브랜치에서도 실패하는 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`다. 건너뛴 82건은 각자 빈 DB가 필요한 산책 PostgreSQL 테스트로 실행하지 않았다.
- PostgreSQL: Step 2 저장이 먼저 잠그면 상태 전환이 기다린 뒤 완료, 상태 전환이 먼저면 403과 기록 없음, 같은 답 동시 4건 모두 200과 같은 시각과 1행, 다른 답 동시 2건은 200과 409, 상황 저장 실패 시 전체 롤백, CHECK 제약, 016~020 뒤 021 적용과 재실행, `create_all()` 없이 동작, 가입 종료의 Step 2 행 삭제와 커밋 실패 시 롤백
- SQLite: 경험 없음, 경험 있음, 조회 3가지, 재요청, 409, 선행 단계 409(본문 검증보다 먼저), 역할과 상태 403, 401, 422 21가지, 경험 없음과 상세 값, 잠금 직전 상태 변경, DB 제약
- 미실행: 개발 DB 검증, FE 실연동

## 11. 미확정 정책

- [ ] 견종 최대 200자(Notion 제안값, 컬럼 길이와 같음)
- [ ] 빈도 "거의 없음"(`RARELY`)에서도 난이도 필수인지(현재 필수)
- [ ] 예전 필드 `dog_size_codes`와 알 수 없는 필드: **현재 무시**. 거부할지, 경험 없음과 함께 오면 422로 볼지 협의 필요
- [ ] 상세 화면 초기 선택값을 비울지(FE 협의, 비우기 권장)
- [ ] 역할 차단(`ONBOARDING_NOT_ALLOWED`)과 연령 확인 필요 차단(`AGE_REVIEW_REQUIRED`) 코드의 API 간 정합성(#45)

## 12. FE 변경 필요 사항

- 경험 여부 화면과 상세 화면이 같은 상태를 쓴다. 지금은 두 화면이 각자 상태를 만든다.
- 경험 없음이면 경험 여부 화면의 "다음"에서 `{"has_experience": false}`를 저장하고 200 이후 산책 루틴 화면으로 간다. 상세 값은 보내지 않는다.
- 경험 있음이면 상세 화면에서 전체 값을 저장하고 200 이후 이동한다. 크기는 단일 `dog_size_code`로 보낸다.
- 저장 실패와 시간 초과에서는 이동하지 않는다. 409 `PREVIOUS_STEP_NOT_COMPLETED`는 `required_step` 화면으로, 409 `DOG_EXPERIENCE_ALREADY_COMPLETED`는 조회 후 같은 값이면 진행한다.
- "특별히 없음"은 다른 항목과 함께 고를 수 없게 한다.
- 경험 여부 화면의 "처음인 경우, 홈화면으로 바로 이동해요" 문구를 실제 동작에 맞게 바꾼다.
- FE 파일: `src/screens/onboarding/UserInput2Screen.tsx`, `UserInput3Screen.tsx` (`walkwithdog-FE` `04e497e`, 이번 작업에서 수정하지 않음)

## 13. Notion 설계와 실제 구현의 차이

| 항목 | Notion | 구현 | 근거 |
|---|---|---|---|
| 크기 | `dog_size_codes` 배열, `volunteer_experience_sizes` | 단일 `dog_size_code` 컬럼 | 2026-11-23 사용자 결정 |
| 상황 테이블 키 | `UNIQUE(user_id, code)` | 복합 PK | Issue #42 |
| 조회 API 선행 단계 | Step 1 미완료 처리 없음 | 409 `PREVIOUS_STEP_NOT_COMPLETED` | Issue #42 |
| 선행 단계 코드 | `BASIC_INFORMATION` | `PLEDGE` 추가 | 2026-11-23 온보딩 순서 결정 |
| 저장 잠금 | PK 기반 중복 방지 | PK와 `users` 행 `FOR SHARE` 재확인 | #41 쓰기 재확인 원칙 |

## 14. 개발 DB와 배포 대기

- 021은 016~020 다음에 적용한다. 실제 개발 DB 적용 여부는 확인하지 못했다. 적용은 승인 후 진행한다.
- 마이그레이션 번호: `origin/dev`에 `017_f10_behavior_marker.sql`, `018_f11_walk_overtime.sql`이 들어와 017, 018 번호가 각각 두 파일이다. 021 자체는 `dev`와 겹치지 않지만 온보딩 파일 전체의 번호 정리가 병합 전 과제로 남아 있다. 이 문서 작성 시점에 파일 이름과 SQL은 바꾸지 않았다.
- `dev`의 F-10~F-12 새 경로 3개: 2026-11-23 로컬 시험 병합(`dev` `baafada`와 `188da56`, Push하지 않음)에서 접근 제어 자동 테스트의 `detail` 차단 검사에 자동으로 포함되어 통과했다. 시험 병합 전체 결과는 592 passed, 2 failed(기존 산책 설정 테스트), 82 skipped다. 실제 병합 후 다시 확인해야 한다.
- 기존 대기 사항 유지: #41 가입 종료의 개인정보 보존 정책, 제한 계정의 산책 취소, 종료와 보호소 승인 정책, 개발 DB 016~020 사전 검사와 적용 승인, 산책 PostgreSQL 테스트 82건 미실행, 산책 설정 테스트 실패 2건

## 15. 후속 Issue와 의존 관계

- 선행: #33(서약), #40(Step 1, 공통 기능), #41(가입 종료, 접근 제어)
- 후속: Step 3 산책 루틴(`first_incomplete_step()`의 `WALKING_ROUTINE` 완료 판정, 산책 루틴의 편한 크기는 같은 코드 집합을 따로 저장), #45(`GET /users/me` 진행 상태와 오류 코드 정합성)

## 16. 관련 링크

- Issue #42, PR #57, 선행 PR #53
- Notion: [Step 2 반려견 경험 저장](https://app.notion.com/p/420b7bbac8c781c884a8f1cc9f33c3c8), [Step 2 반려견 경험 조회](https://app.notion.com/p/420b7bbac8c781d1b839e4d5c8b09116), [온보딩 전체 설계](https://app.notion.com/p/41fb7bbac8c7809b914bcf3e80d9d876)의 "#42 반려견 경험 구현 결과" 절
