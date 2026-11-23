# 인수인계: 연령 확인 필요 계정의 생년월일 정정과 가입 종료 (Issue #41)

- 작성: 2026-11-23 (KST). 이 문서는 구현 후에 작성했다. 코드와 테스트는 바꾸지 않았다.
- 브랜치: `feature/onboarding-age-review`
- PR: [Draft PR #53](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/53), 대상 브랜치 `feature/onboarding-basic-info`(#52)
- 기능 커밋 3개

| 커밋 | 내용 |
|---|---|
| `34d493e` | 정정 API, 가입 종료 API, 비밀번호 실패 제한, 로그인 FK 경합 처리 |
| `41fefef` | 기존 `detail` 형식 API 45개의 연령 확인 필요 계정 차단, 전체 경로 자동 테스트 |
| `1663aa3` | 일반 사용자 쓰기 13개 경로의 트랜잭션 내 상태 재확인 |

- 근거: [Issue #41](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/41)과 "최종 결정 반영 (2026-11-23)", Notion [연령 제한 계정 생년월일 정정](https://app.notion.com/p/420b7bbac8c78155a5c6f06ff96c41fb), [연령 제한 계정 가입 종료 및 삭제](https://app.notion.com/p/420b7bbac8c78115b145e7d855165e90)
- 상태: `dev` 미병합, 개발 DB 미적용. 새 마이그레이션 없음.
- **가입 종료는 동의, 서약 기록의 법정 보존 정책이 정해지기 전까지 운영 환경에서 성공하지 않는다.**

## 1. 개요와 범위

#40 Step 1에서 만 14세 미만으로 판정되어 `AGE_REVIEW_REQUIRED`가 된 계정이 생년월일을 정정해 `ACTIVE`로 돌아가거나 가입을 종료한다. 이 상태의 계정이 기존 API로 쓰기를 하지 못하게 막는다.

| 구분 | 내용 |
|---|---|
| 포함 | 정정 API, 가입 종료 API, 기존 API 접근 제어, 쓰기 트랜잭션 내 상태 재확인 |
| 제외 | `ACTIVE` 계정의 일반 탈퇴, 활동 기록의 이관이나 익명화, 운영자 수동 심사, 비밀번호 없는 계정 삭제, 방치 계정 자동 파기 |

## 2. 주요 변경 파일

| 파일 | 커밋 | 내용 |
|---|---|---|
| `app/age_review_service.py` | `34d493e` | 정정, 가입 종료, 보존 대상 확인, 삭제 순서 |
| `app/routers/age_review.py` | `34d493e` | 두 API, 비밀번호 실패 제한 |
| `app/rate_limit.py` | `34d493e` | `FailureLimiter` |
| `app/routers/auth.py` | `34d493e` | 로그인 세션 저장의 FK 오류를 401로 |
| `app/main.py` | `34d493e` | 라우터 등록 |
| `app/auth.py` | `41fefef` | `get_current_user`의 계정 상태 확인 |
| `app/account_status.py` | `41fefef`, `1663aa3` | `is_active()`, `require_active_writer()` |
| `app/routers/walks.py`, `walk_requests.py`, `walk_reviews.py`, `chat.py`, `surveys.py`, `matching.py` | `1663aa3` | 쓰기 전 상태 재확인 |
| `tests/test_age_review.py`, `tests/test_age_review_access.py`, `tests/test_signup_postgres.py` | 3개 | 테스트 |
| `tests/test_chat_simulation.py`, `tests/test_demo_tools.py`, `tests/test_matching.py` | `1663aa3` | 사용자 대역에 `role` 추가 |

## 3. 생년월일 정정: PUT /users/me/age-review/birth-date

- 요청: `{"birth_date": "YYYY-MM-DD"}`
- 성공 200 `data`: `account_status`(`ACTIVE`), `age_review_required`(false), `step1_completed`, `next_onboarding_step`
- 순서: 인증(401 4종) → `role=user`(403 `AGE_REVIEW_NOT_ALLOWED`) → 상태(409 `AGE_REVIEW_NOT_REQUIRED`) → 날짜 검증(422 `BIRTH_DATE_REQUIRED`, `INVALID_BIRTH_DATE`, `AGE_BELOW_MINIMUM`, `AGE_ABOVE_MAXIMUM`) → 트랜잭션: `users` 행 `FOR UPDATE`와 상태 재확인 → `account_status=ACTIVE`, `age_review_required_at=NULL` → 커밋
- 만 나이는 #40의 `age_years()`(Asia/Seoul, 2월 29일 출생자는 평년 3월 1일)로 계산하고 만 14~120세만 허용한다. 범위 밖이면 422이고 상태를 바꾸지 않는다.
- 생년월일은 저장하지 않는다. 응답과 로그에도 남기지 않는다. `volunteer_onboarding_basics`와 `phone_verifications`를 만들지 않는다. Step 1은 미완료로 남아 SMS 인증과 기본 정보 저장을 다시 한다.
- 기존 서약, 동의 이력은 그대로 둔다.
- `next_onboarding_step`은 #40의 `first_incomplete_step()`으로 계산한다. 현재 버전 서약이 있으면 `BASIC_INFORMATION`, 서약 버전이 바뀌었으면 `PLEDGE`다.
- 커밋 실패 시 롤백하고 제한 상태를 유지한다.

## 4. 가입 종료: DELETE /users/me

- 요청 본문: `{"password": "..."}`. 성공 200 `{"deleted": true}`
- 순서: 인증 → `role=user`(403 `ACCOUNT_DELETION_NOT_ALLOWED`) → 상태(409 `AGE_REVIEW_NOT_REQUIRED`) → 본문(422 `PASSWORD_REQUIRED`) → 실패 제한(429 `TOO_MANY_PASSWORD_ATTEMPTS`, `Retry-After`) → scrypt 재확인(403 `PASSWORD_CONFIRMATION_FAILED`, 비밀번호 해시가 없는 계정은 항상 403) → 한 트랜잭션
- 트랜잭션: `users` 행 `FOR UPDATE`와 상태 재확인 → 보존 대상 확인 → FK 순서 명시 삭제 → 커밋. 하나라도 실패하면 전체 롤백되어 계정, 세션, 이력이 그대로 남고 기존 토큰도 계속 동작한다.
- 모든 기기의 세션(`auth_sessions`)과 레거시 `session_tokens`를 같은 트랜잭션에서 지운다. 성공 후 기존 토큰은 401이다. 응답 유실 후 재시도도 401이 될 수 있다.
- 비밀번호 실패 제한: `FailureLimiter`, 계정별 900초에 5회(임시 기본값). 설정 `ACCOUNT_EXIT_PASSWORD_FAILURE_LIMIT`, `ACCOUNT_EXIT_PASSWORD_FAILURE_WINDOW_SECONDS`. 프로세스 메모리 저장이라 여러 프로세스에서는 각자 센다.
- 로그인 경합: 가입 종료 중 로그인의 세션 저장은 잠금을 기다린 뒤 FK로 실패한다. `POST /auth/login`은 이 경우 500 대신 401 `INVALID_CREDENTIALS`를 반환하고 세션이 남지 않는다.

### FK 관계와 삭제 순서 (#41 커밋 기준)

`users`를 참조하는 FK는 모두 `NO ACTION`이다. `ON DELETE CASCADE`를 추가하지 않았고 마이그레이션도 없다. 아래 순서로 명시 삭제하고 `users`를 마지막에 지운다.

1. `auth_sessions`
2. `session_tokens`
3. `phone_verifications`
4. `phone_verification_daily_counts`의 `USER` 행(subject가 사용자 ID)
5. `volunteer_onboarding_basics`(정상 흐름에서는 없음)
6. `user_pledge_acknowledgements`
7. `user_consents`
8. `users`

| 구분 | 테이블 | 처리 |
|---|---|---|
| 보존 정책 대기 | `user_pledge_acknowledgements`, `user_consents` | 아래 "보존 정책" 참고 |
| 보존(삭제 거부) | `walk_requests`(`user_id`, `reviewed_by`), `walk_incidents`(`reported_by`, `acknowledged_by`), `chat_sessions`, `matching_results`, `surveys`, `volunteer_profiles` | 한 행이라도 있으면 409 `ACCOUNT_HAS_RETAINED_ACTIVITY`, 아무것도 지우지 않음 |
| 간접 참조(함께 보존) | `walks`, `walk_reviews`, `chat_messages` | 위 보존 대상을 통해서만 연결 |
| 유지 | `phone_verification_daily_counts`의 `PHONE` 행 | 번호 HMAC만 있는 남용 방지 카운터, 정리 함수로 삭제 |

FK 없이 사용자 ID를 담는 컬럼은 없다(#41 구현 시점 모델 조회).

### 보존 정책과 운영 비활성

- 동의, 서약 기록의 법정 보존 여부와 기간이 미확정이다. 이 기록이 있는 계정의 가입 종료는 기본적으로 409 `ACCOUNT_HAS_RETAINED_ACTIVITY`이고 아무것도 지우지 않는다.
- 삭제는 `ACCOUNT_EXIT_RECORD_POLICY=delete`이면서 `APP_ENV`가 local, development, test일 때만 실행된다(`record_deletion_enabled()`). 운영 환경에서는 설정과 관계없이 실행되지 않는다. 운영에서 켜려면 정책 결정과 `app/age_review_service.py` 수정이 필요하다.
- 실제 흐름에서 연령 확인 필요 계정에는 항상 서약 기록이 있으므로 **운영에서는 이 API가 정책 결정 전까지 성공하지 않는다.**
- 정책 미설정과 실제 활동 기록 모두 같은 409 코드다(Notion 계약). 운영 로그에는 사유만 구분해 남기고 계정 식별 정보는 남기지 않는다.

## 5. 기존 API 접근 제어 (`41fefef`)

- 점검 시점 실제 앱의 경로 67개(Method와 경로 조합)를 분류했다.

| 분류 | 수 | 동작 |
|---|---|---|
| 허용 | 5 | 로그인, 로그아웃, `GET /users/me`, 정정, 가입 종료. 계약 의존성(`get_contract_user`, `get_contract_session`)이라 상태 검사를 거치지 않음 |
| 계약 형식 차단 | 5 | 서약, Step 1 API 4개. 계약 403 `AGE_REVIEW_REQUIRED` |
| `detail` 형식 차단 | 45 | 산책, 산책 신청, 리뷰, 지도, 채팅, 설문, 매칭, 시뮬레이션, 보호소, 관리자 API. `403 {"detail": "연령 확인 정보를 먼저 수정해 주세요."}` |
| 공개 | 12 | 강아지와 보호소 조회, 설문 선택지, 견종 목록, 가입과 중복 확인, `/`, `/db-test`. 사용자 식별 없음 |

- 차단 경로는 50개(계약 5, `detail` 45)다. `detail` 경로 45개는 모두 `get_current_user`(관리자 수집 API는 직접 호출)로 사용자를 얻으므로 `app/auth.py` 한 곳에서 적용했다.
- 형식 차이: 온보딩 계약 API는 `{code, data, message}`의 403 `AGE_REVIEW_REQUIRED`, 기존 API는 FastAPI 기본 `detail` 형식 403이다. 기존 API의 응답 형식은 바꾸지 않았다.
- 상태 검사는 세션 확인 뒤, 역할 검사 앞에서 실행된다. 401과 기존 역할 403의 형식과 메시지는 그대로다. 보호소, 관리자, `ACTIVE` 일반 사용자는 응답이 바뀌지 않는다(상태 전환은 일반 사용자의 Step 1에서만 발생).
- 자동 테스트 `tests/test_age_review_access.py`가 실제 앱의 모든 경로를 네 분류로 나누고 각 경로를 호출해 확인한다. 분류되지 않은 새 경로가 생기면 실패한다. 전체 점검표는 PR #53 본문에 있다.

## 6. 쓰기 트랜잭션의 상태 재확인 (`1663aa3`)

요청 시작 시 검사만으로는 상태 전환과 동시에 들어온 쓰기 1건이 통과할 수 있었다.

### 점검 결과 (`detail` 차단 경로 45개)

| 분류 | 수 | 경로 |
|---|---|---|
| 일반 사용자 쓰기 | 13 | `POST /walk-requests`, `PATCH /walk-requests/{id}`, `POST /walks`, `POST /walks/{id}/points`, `POST /walks/{id}/markers`, `POST /walks/{id}/incidents`, `PATCH /walks/{id}/end`, `GET /walks/active`(멈춘 산책 자동 종료와 봉사자 통계 갱신), `PUT /walks/{id}/review`, `POST /chat/sessions`, `POST /chat/sessions/{id}/messages`, `POST /surveys/`, `POST /matching/{survey_id}` |
| 보호소, 관리자만 쓰기 | 8 | 강아지 등록과 성향, 산책 설정, 보호소 등록, 사고 확인, 견종 등록, 사용자 보호소 지정, 공공데이터 수집 |
| 읽기 전용 | 24 | `POST /matching/score`(쓰기 없음) 포함 |

### 변경

- `require_active_writer(db, user)`: 일반 사용자면 현재 트랜잭션에서 `users` 행을 `FOR SHARE`로 잠그고(`populate_existing()`으로 최신 값) `ACTIVE`가 아니면 기존 형식 403, 행이 없으면 401. 보호소와 관리자는 잠그지 않는다.
- 적용 위치
  - 산책: `walks.persist`, `walk_requests.persist_request`의 시작(업무 행 잠금보다 먼저)
  - 리뷰 저장, 설문 저장, 매칭 결과 저장, 채팅 세션 생성: 기록 추가 직전
  - 채팅 메시지: 외부 AI 응답을 받은 뒤 기록 추가 직전. 외부 호출 중에는 사용자 행을 잠그지 않는다.
- 잠금 모드: 상태 전환, 정정, 가입 종료, SMS 발송 예약은 `FOR UPDATE`, 서약, Step 1 저장, 위 쓰기는 `FOR SHARE`. 같은 사용자의 쓰기끼리는 막지 않고 상태 전환과는 서로 기다린다.
- 쓰기가 먼저 잠그면 상태 전환이 기다리고, 상태 전환이 먼저면 쓰기는 바뀐 상태를 읽고 403으로 롤백한다. 격리 수준은 기본 READ COMMITTED다.
- 데드락 검토: 상태 전환은 사용자 행 하나만 잠근다. 산책 신청은 `FOR SHARE` 뒤 기존 `FOR UPDATE NOWAIT`로 같은 행을 올려 잡는데 같은 트랜잭션이라 충돌하지 않는다(경합 테스트로 확인).
- 서버 백그라운드의 멈춘 산책 자동 종료(`close_stale_walks`)는 시스템 정리라 검사하지 않는다.
- API 계약, 응답 형식, 역할 권한, 마이그레이션은 바꾸지 않았다.

### 남은 위험

- 채팅은 외부 AI 호출 뒤에 확인하므로 호출 중 상태가 바뀌면 호출 비용은 발생하고 기록만 저장되지 않는다.
- 기존 코드가 AI 호출 동안 `chat_sessions` 행을 `FOR UPDATE`로 잡고 있는 점은 범위 밖이라 그대로 두었다.

## 7. 동시성 요약

- 정정이 먼저 잠그면 가입 종료는 409 `AGE_REVIEW_NOT_REQUIRED`, 가입 종료가 먼저면 정정은 401 `USER_NOT_FOUND`
- 같은 정정 4건 동시: 200 1건, 409 3건 / 같은 가입 종료 4건 동시: 200 1건, 401 3건

## 8. 테스트 결과 (PR #53 본문 기록 인용. 이번 문서 작업에서 다시 실행하지 않음)

실행 환경: Windows 11, Python 3.12 `.venv`, SQLite(FK 강제), 격리 PostgreSQL 16.16 테스트 DB(`SIGNUP_TEST_DATABASE_URL`)

| 시점 | 실행 | 결과 |
|---|---|---|
| 선행 #52 | `tests/` 전체 | 507 passed, 2 failed, 82 skipped |
| `34d493e` | `tests/` 전체 | 532 passed, 2 failed, 82 skipped |
| `41fefef` | `tests/` 전체 | 538 passed, 2 failed, 82 skipped |
| `1663aa3` | `tests/` 전체 | 545 passed, 2 failed, 82 skipped |
| `1663aa3` | `tests/test_age_review_access.py` | 7 passed, 288 subtests |
| `34d493e` | `tests/test_age_review.py` | 20 passed |
| `34d493e` | 격리 PostgreSQL #41 경합과 롤백 | 5 passed, 3회 반복 통과 |
| `1663aa3` | 격리 PostgreSQL 쓰기 경합 | 6 passed, 3회 반복 통과 |

- 실패 2건은 선행 브랜치에서도 실패하는 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`다.
- 쓰기 재확인을 임시로 끈 상태에서 상태 전환 선행 테스트 4개가 모두 실패해, 테스트가 경합을 실제로 잡아낸다는 것을 확인했다.
- 동의, 서약 삭제 경로는 테스트 환경 설정(`ACCOUNT_EXIT_RECORD_POLICY=delete`, `APP_ENV=test`)에서만 검증했다.
- 미실행: 운영 설정에서의 동의, 서약 삭제(정책 대기로 의도적 차단), 산책 PostgreSQL 테스트 82건(각자 빈 DB 필요), 개발 DB 검증, FE 실연동

## 9. FE 연동

- 연령 확인 안내, 정정, 가입 종료 화면이 FE에 없다. API만 먼저 구현했다.
- 정정 성공 후 `next_onboarding_step`에 따라 서약 또는 Step 1로 이동한다. 응답 유실 후 409를 받으면 `GET /users/me`로 상태를 확인한다.
- 가입 종료는 `DELETE` 본문을 쓴다. 클라이언트의 `DELETE` 본문 지원을 확인해야 한다. 200 이후 로컬 인증 정보를 지운다. 409 `ACCOUNT_HAS_RETAINED_ACTIVITY`는 문의 안내로 처리한다.
- 기존 API는 연령 확인 필요 계정에 `detail` 형식 403을 준다. FE는 이 응답을 연령 확인 화면으로 연결해야 한다.

## 10. Notion 설계와 실제 구현의 차이

| 항목 | 구현 | 근거 |
|---|---|---|
| 정정 재요청 | 409 `AGE_REVIEW_NOT_REQUIRED` | 명세 오류 표. 명세 본문의 "멱등 200 권장"과 충돌, 미확정 |
| 가입 종료 정책 미설정 | 409 `ACCOUNT_HAS_RETAINED_ACTIVITY` | 보존 정책 미확정. 별도 코드 필요 여부 미확정 |
| 동의, 서약 삭제 | 테스트 환경에서만 실행 | 법정 보존 정책 미확정 |
| 비밀번호 실패 제한 | 계정별 900초 5회, 메모리 | Notion에 429 코드만 있음 |

## 11. 미확정 정책과 승인 필요 항목

- [ ] 동의, 서약 기록의 법정 보존 여부와 기간. 결정 후 운영 삭제 활성화(코드 수정 필요)
- [ ] 보존 대상 테이블 최종 목록과, 보존 대상이 있는 계정의 후속 정리 경로
- [ ] 보호소가 제한 계정의 대기 신청을 승인할 수 있는지(현재 행위자 기준이라 막지 않음)
- [ ] 제한 계정이 자기 신청 취소와 진행 중 산책 종료를 할 수 있는지(현재 차단)
- [ ] 정정 재요청 응답(409 또는 멱등 200)
- [ ] 비밀번호 실패 제한의 횟수, 기간, 다중 프로세스 저장소
- [ ] 가입 종료 거부 사유를 FE가 구분해야 하는지(별도 코드 또는 `data.reason`)
- [ ] 가입 종료 후 아이디, 닉네임, 전화번호 재사용 시점(지금은 즉시 재사용 가능)
- [ ] 방치된 연령 확인 필요 계정의 자동 파기 기한(미구현)
- [ ] 채팅 외부 AI 호출 비용과 `chat_sessions` 행 잠금 문제

## 12. 개발 DB와 배포 대기

- 새 마이그레이션은 없다. 016~020 적용이 선행 조건이다(#30, #40 문서 참고). 실제 개발 DB 적용 여부는 확인하지 못했다.
- 운영 설정: `ACCOUNT_EXIT_RECORD_POLICY`는 비워 둔다. 실패 제한 설정을 확정한다.
- 마이그레이션 번호 충돌(`dev`의 `017_f10_behavior_marker.sql`, `018_f11_walk_overtime.sql`)과 `dev` 신규 산책 경로의 접근 제어 분류는 이 스택을 `dev`에 병합하기 전에 해결해야 한다.

## 13. 후속 연동 (#41 구현 범위 밖)

아래는 이후 Issue에서 #41 코드에 연결한 내용이다. #41 PR의 변경이 아니다.

- #42(PR #57, `188da56`)
  - 가입 종료 삭제 순서에 `volunteer_experience_challenges` → `volunteer_onboarding_experiences`를 `volunteer_onboarding_basics` 앞에 추가했다. Step 2 기록은 동의, 서약 같은 보존 정책 대기 대상이 아니라 기본 정보와 같은 온보딩 기록으로 분류했다.
  - 운영 환경의 가입 종료 차단과 보존 정책은 바꾸지 않았다.
  - `tests/test_age_review_access.py`에 Step 2 경로 2개를 계약 형식 차단으로 분류했다(계약 차단 7개).
- `dev`에 들어온 F-10~F-12 산책 기능은 이 스택과 아직 합쳐지지 않았다. 새 경로 3개(`GET /walks/history`, `GET /shelters/me/walks`, `GET /shelters/me/walks/{walk_id}`)는 접근 제어 자동 테스트 분류가 필요하다.

## 14. 관련 링크

- Issue #41, PR #53, 선행 PR #52, 후속 PR #57
- Notion: [연령 제한 계정 생년월일 정정](https://app.notion.com/p/420b7bbac8c78155a5c6f06ff96c41fb), [연령 제한 계정 가입 종료 및 삭제](https://app.notion.com/p/420b7bbac8c78115b145e7d855165e90), [온보딩 전체 설계](https://app.notion.com/p/41fb7bbac8c7809b914bcf3e80d9d876)의 "#41 연령 확인 필요 계정 정정과 가입 종료 구현 결과" 절
