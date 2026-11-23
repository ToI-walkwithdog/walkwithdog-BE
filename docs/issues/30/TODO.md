# 온보딩 회원가입 TODO (Issue #30)

F-03~F-07 기록은 `docs/team/TODO.md`에 있다. 이 문서는 Issue #30 항목만 관리한다.

## DB 설계

- [x] `users.id` 숫자 PK와 `role`, `shelter_id`, `created_at` 유지
- [x] `login_id VARCHAR(20)`, `nickname VARCHAR(20)`, `email VARCHAR(100)` NOT NULL, `lower()` 고유 인덱스 3개 (`app/models.py`, `migrations/016_onboarding_signup.sql`)
- [x] `password VARCHAR(255) NOT NULL`, scrypt 해시 저장
- [x] 마이그레이션 번호 `014` → `016` 변경 (F-08 `014_f08_live_walk.sql`, F-09 `015_f09_walk_review.sql` 다음)
- [x] F-08 `users.phone` 이후에 016 적용 가능 확인 (`phone VARCHAR(50)` NULL 허용 유지, PostgreSQL 실검증)
- [ ] 개발 DB 보존 대상 조사와 016 적용 계획 결정 (016은 빈 `users`에서만 적용됨, 아래 "미결" 참고)
  - 2026-11-23: 읽기 전용 사전 검사 SQL 작성(`scripts/precheck_017_users.sql`). 개발 DB 접속 정보가 없어 미실행
- [x] 이메일 미수집: 모델, 가입 요청과 응답, 중복 판정에서 `email` 제거 (`feature/onboarding-login-id`)
- [x] `migrations/017_remove_user_email.sql` 작성, 격리 PostgreSQL 검증
- [ ] 개발 DB에 017 적용 (사전 검사 결과 보고와 승인 후, `HANDOFF.md` 6절)

## API 및 오류 처리

- [x] `GET /users/id-availability`, `GET /users/nickname-availability` 공개, `/users/{user_id}`보다 먼저 등록
- [x] 사전 조회와 가입이 같은 정규화·검증 함수 사용 (`app/account_rules.py`)
- [x] 비밀번호 15~128자, 대문자·소문자·숫자·특수문자 각 1개 이상
- [x] 사전 조회는 HTTP 200 `data.available`만 반환, 값 선점 없음
- [x] 가입 성공 HTTP 200 `{code, data, message}`, 필드별 422 코드와 `data`
- [x] 오류 메시지를 요청서의 Notion 명세 표 기준으로 수정, `INVALID_LOGIN_ID_FORMAT`은 API별 문구 분리
- [x] 409 `LOGIN_ID_ALREADY_EXISTS`, `NICKNAME_ALREADY_EXISTS`, `EMAIL_ALREADY_EXISTS` 구분과 롤백
- [x] 2026-11-23: 아이디 허용 문자를 영문 대소문자와 숫자로 변경(밑줄 불허), `INVALID_EMAIL`과 `EMAIL_ALREADY_EXISTS` 제거
- [x] 429 `TOO_MANY_REQUESTS` 실제 적용, 실제 남은 시간을 `retry_after_seconds`와 `Retry-After`로 반환 (`app/rate_limit.py`)
- [ ] Rate Limit 운영 정책 확정 (횟수·기간, 공유 IP, worker 수, 공유 저장소, 장애 시 정책)
- [ ] `privacy` 검증과 동의 저장 연동 (Issue #31)

## 테스트

- [x] SQLite 회원가입 (`tests/test_signup.py`)
- [x] Rate Limit (`tests/test_rate_limit.py`)
- [x] 격리 PostgreSQL: 고유 인덱스, 409 분류, 경쟁 상태, 동시 가입, 016 적용·중단, F-08/F-09 이후 016 적용 (`tests/test_signup_postgres.py`, 같은 DB에서 2회 연속 통과)
- [x] 보호소·관리자 회귀 (`tests/test_shelter_permissions.py`, `tests/test_auth.py`)
- [x] F-08·F-09 병합 후 통합 회귀 (`tests/test_f08_live_walk.py`, `tests/test_f09_walk_review.py`)
- [ ] 다중 worker·다중 서버에서 Rate Limit 동작 (현재 단일 프로세스 메모리 저장소, 미검증)

## 완료 조건

- [x] 각 필드의 고유성과 회원가입 계약을 PostgreSQL 및 API 테스트에서 확인
- [x] OpenAPI에 사전 조회, 최종 중복, 가입 성공·오류, 429 응답 기록
- [ ] Notion·FE 계약 문서와 최종 대조 (Notion 페이지에 접근하지 못해 미확인)
- [x] `dev` 대상 PR #38 병합 (`c45c3b3`)
- [ ] 이메일 제거와 아이디 로그인(#32)을 묶은 후속 PR (`feature/onboarding-login-id`)

## 미결

- 개발 DB에 기존 사용자가 있으면 016은 중단된다. 다음을 먼저 정해야 한다.
  1. 기존 계정의 `login_id` 부여 방식
  2. 20자 초과 또는 대소문자 무시 중복 닉네임 처리
  3. `password`가 NULL이거나 평문인 계정 처리
  4. `pg_dump` 백업과 복구 확인 후 적용
- `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422` 실패 2건은 `origin/dev`(07111fc)에서도 같은 결과다. Issue #30 범위 밖이다.
