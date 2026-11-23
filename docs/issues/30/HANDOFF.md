# 인수인계: 온보딩 회원가입과 아이디 로그인 (Issue #30, #32)

- 갱신: 2026-11-23 (KST)
- 브랜치: `feature/onboarding-login-id` (`origin/dev` `c45c3b3`에서 분기)
- 이전 단계: PR #38(`feature/onboarding-signup`)이 `dev`에 병합됨. 로그인 아이디, 중복 확인, 가입 검증, 요청 제한, `016`
- 이번 단계: 이메일 미수집(#30), 아이디 허용 문자 변경(#30), `identifier` 로그인(#32), 사용자 조회 응답의 이메일 제거(#32), `017`
- 제외: 개인정보 동의(#31), 최초 서약(#33), 통합 온보딩 테스트(#34), 이메일 인증과 비밀번호 재설정(#14)

## 1. API 계약

아래 네 API는 `{"code", "data", "message"}`로 응답한다. 다른 API는 FastAPI 기본 `detail` 형식을 유지한다.

### POST /users/

- 요청: `login_id`, `nickname`, `password`. 이메일은 받지 않는다. `email`을 포함해 그 밖의 필드(`role`, `shelter_id` 등)는 무시한다. 비밀번호 확인은 FE에서 처리한다.
- 성공: HTTP 200 `OK`, `data`는 `id`, `login_id`, `nickname`, `role`, `shelter_id`. 메시지 `회원가입이 완료되었습니다.` 토큰은 발급하지 않는다.

| HTTP | code | data | message |
|---|---|---|---|
| 422 | `REQUIRED_FIELD` | `{"field"}` | 필수 입력 항목을 확인해 주세요. |
| 422 | `INVALID_LOGIN_ID_LENGTH` | `{"field","min_length":4,"max_length":20}` | 아이디는 4~20자로 입력해 주세요. |
| 422 | `INVALID_LOGIN_ID_FORMAT` | `{"field":"login_id"}` | 아이디에는 영문과 숫자만 사용할 수 있습니다. |
| 422 | `INVALID_NICKNAME_LENGTH` | `{"field","min_length":3,"max_length":20}` | 닉네임은 3~20자로 입력해 주세요. |
| 422 | `INVALID_NICKNAME_FORMAT` | `{"field":"nickname"}` | 닉네임에 사용할 수 없는 문자가 포함되어 있습니다. |
| 422 | `INVALID_PASSWORD_LENGTH` | `{"field","min_length":15,"max_length":128}` | 비밀번호는 15~128자로 입력해 주세요. |
| 422 | `PASSWORD_UPPERCASE_REQUIRED` 외 3개 | `{"field":"password","rule"}` | 비밀번호에 영문 대문자를 1개 이상 포함해 주세요. (소문자, 숫자, 특수문자 동일 형식) |
| 409 | `LOGIN_ID_ALREADY_EXISTS` / `NICKNAME_ALREADY_EXISTS` | `{"field"}` | 이미 사용 중인 아이디/닉네임입니다. |
| 429 | `TOO_MANY_REQUESTS` | `{"retry_after_seconds"}` | 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요. |
| 500 | `INTERNAL_SERVER_ERROR` | `{}` | 일시적인 오류가 발생했습니다. |

- 삭제: `INVALID_EMAIL`, `EMAIL_ALREADY_EXISTS`
- 여러 필드가 틀리면 `login_id`, `nickname`, `password` 순서로 첫 오류 하나만 반환한다. 중복도 같은 순서로 판정한다.
- 필드 누락, `null`, 빈 문자열, 공백만 있는 값(비밀번호 제외), 문자열이 아닌 값은 `REQUIRED_FIELD`다. 본문이 없거나 JSON이 아니면 `{"field":"login_id"}`다.

### GET /users/id-availability?login_id=

- 성공 200 `{"available": true|false}`, 메시지 `사용 가능한 아이디입니다.` 또는 `이미 사용 중인 아이디입니다.`
- 422 `LOGIN_ID_REQUIRED`(아이디를 입력해 주세요.), `INVALID_LOGIN_ID_LENGTH`, `INVALID_LOGIN_ID_FORMAT`(아이디에 사용할 수 없는 문자가 포함되어 있습니다.)
- 가입과 같은 검증 함수를 쓰므로 밑줄은 여기서도 형식 오류다.

### GET /users/nickname-availability?nickname=

- 변경 없음. 성공 200 `{"available"}`, 422 `NICKNAME_REQUIRED`, `INVALID_NICKNAME_LENGTH`, `INVALID_NICKNAME_FORMAT`

### POST /auth/login (#32에서 변경)

- 요청: `{"identifier": "walker04", "password": "..."}`. 기존 `email` 필드는 없앴고 별칭으로도 받지 않는다(`email`만 보내면 `IDENTIFIER_REQUIRED`).
- 성공: HTTP 200, `Cache-Control: no-store`

```json
{"code": "OK", "data": {"access_token": "...", "token_type": "bearer", "expires_in": 3600}, "message": "로그인에 성공했습니다."}
```

| HTTP | code | 조건 | data | message |
|---|---|---|---|---|
| 422 | `IDENTIFIER_REQUIRED` | 누락, `null`, 공백, 문자열 아님, 본문 없음 | `{"field":"identifier"}` | 아이디를 입력해 주세요. |
| 422 | `PASSWORD_REQUIRED` | 누락, `null`, 빈 값, 공백만 있음 | `{"field":"password"}` | 비밀번호를 입력해 주세요. |
| 422 | `INVALID_IDENTIFIER_FORMAT` | 공백 제거 후 100자 초과 | `{"field":"identifier"}` | 아이디 형식을 확인해 주세요. |
| 401 | `INVALID_CREDENTIALS` | 미등록 아이디, 비밀번호 불일치, scrypt가 아닌 저장값, 128자 초과 비밀번호, 이메일 주소 입력 | `{}` | 아이디와 비밀번호를 확인해 주세요. |
| 500 | `INTERNAL_SERVER_ERROR` | 세션 저장 실패 등 | `{}` | 일시적인 오류가 발생했습니다. |

- 401에는 `WWW-Authenticate: Bearer` 헤더가 붙는다.
- 처리 순서: 입력 검증, `identifier` 정규화(앞뒤 공백 제거, ASCII면 소문자), `lower(login_id)`로 조회, scrypt 검증(계정이 없거나 해시가 없어도 더미 해시로 1회 계산), `AuthSession`에 토큰 해시 저장과 커밋, 응답
- 로그인은 가입 규칙(4~20자, 영문과 숫자)을 다시 적용하지 않는다. 밑줄이 있는 기존 아이디도 로그인할 수 있다.
- 로그인 요청 제한(429)은 아직 없다. 담당(#13 또는 #32)이 미정이다.

### 사용자 조회 응답 (`UserResponse`)

- 영향: `GET /users/me`, `GET /auth/me`, `GET /users/{user_id}`, `PATCH /users/{user_id}/shelter`
- 필드: `id`, `login_id`, `nickname`, `role`, `shelter_id`. `email`을 없애고 `login_id`를 추가했다.
- 응답 형식(본문 그대로 반환, 401은 `detail`)과 `/auth/me`, 로그아웃 204는 바꾸지 않았다. 4절 참고.

## 2. 검증 규칙

- 아이디: 앞뒤 공백 제거, ASCII 입력만 소문자 변환, `^[a-z0-9]{4,20}$`. 소문자 변환 후 검사하므로 FE 정규식 `^[A-Za-z0-9]{4,20}$`와 결과가 같다. 저장은 소문자다. ASCII가 아닌 문자는 소문자로 바꾸지 않아 형식 오류가 된다(예: 켈빈 기호 U+212A).
- 닉네임: 변경 없음. NFC 정규화 후 앞뒤 공백 제거, `^[가-힣A-Za-z0-9_]{3,20}$`, 중복은 `lower()`로 비교
- 비밀번호: 변경 없음. 정규화 없이 15~128자, 영문 대문자, 소문자, 숫자, 특수문자 각 1개 이상. scrypt
- 고유성: `uq_users_login_id_lower`, `uq_users_nickname_lower`. UNIQUE 위반은 제약 이름으로 필드를 판별하고, 판별하지 못하면 롤백 후 다시 조회한다. 그래도 알 수 없으면 500이다.

## 3. Rate Limit

변경 없음. `POST /users/`는 IP당 600초에 5회, 중복 확인 두 API는 합산 IP당 60초에 30회. 프로세스 메모리 저장소라 다중 worker에서는 정확하지 않다. 운영 정책은 미확정이다.

## 4. #32 남은 항목 (이번 브랜치에서 구현하지 않음)

| 항목 | 상태 | 이유 |
|---|---|---|
| `POST /auth/logout` HTTP 200 JSON | 미구현, 204 유지 | Issue #32의 "FE 실제 연동 시점과 맞춘다" 결정 대기 |
| `/auth/me` 제거, `/users/me`로 일원화 | 미구현, 두 경로 유지 | 위와 같음 |
| `GET /users/me`의 `{code,data,message}` 형식, 401 4종, `no-store` | 미구현 | 공통 인증 의존성과 API별 401 코드 집합이 Issue #32에서 미확정 |
| 로그인 429 | 미구현 | 담당 Issue 미정(#13 또는 #32) |

## 5. 마이그레이션과 배포 순서

### 파일

- `016_onboarding_signup.sql`: 변경 없음. `users`가 비어 있을 때만 적용된다.
- `017_remove_user_email.sql`(신규): `uq_users_email_lower` 인덱스, `users_email_key` 제약, `users.email` 컬럼을 제거한다.
  - 한 트랜잭션, `LOCK TABLE users IN ACCESS EXCLUSIVE MODE`
  - 사전 조건을 하나라도 만족하지 않으면 전체 중단: `login_id`가 NOT NULL로 존재, `uq_users_login_id_lower` 존재, 공백 아이디 0건
  - `CASCADE`를 쓰지 않는다. 뷰 등 다른 객체가 `email`을 쓰면 실패하고 전체 롤백된다.
  - 재실행하면 아무것도 바꾸지 않는다.
  - 백업은 검사하지 않는다. 적용 전에 직접 확인한다.

### 배포 호환성

- 새 코드는 `email`을 쓰지 않는다. 017 전 스키마에서는 `email NOT NULL` 때문에 가입이 실패한다.
- 이전 코드는 `email`로 로그인한다. 017 후 스키마에서는 로그인이 실패한다.
- 따라서 API 중지, 017 적용, 새 코드 기동을 한 번에 한다. 단계 배포가 필요하면 `email`의 NOT NULL만 먼저 해제하는 중간 마이그레이션을 따로 만들어야 한다(현재 없음).

### 개발 DB 적용 절차 (승인 후)

1. 읽기 전용 사전 검사: `psql -X -v ON_ERROR_STOP=1 -d <DB_NAME> -f scripts/precheck_017_users.sql`
   - 출력에는 개수와 객체 이름만 있다. 이메일, 비밀번호, 아이디 값은 출력하지 않는다.
2. 결과로 경로를 고른다.
   - A. 016 적용됨, 아이디 NULL/공백/중복 0건: 백업 후 017 적용
   - B. 016 미적용, `users` 비어 있음: 016, 017 순서로 적용
   - C. 016 미적용, 보존할 계정 있음: 아래 "기존 계정 아이디 전환" 후 017
3. 백업: `pg_dump -Fc -t users -t auth_sessions <DB_NAME> > users_before_017.dump`, 격리 DB에 `pg_restore`로 복원 확인. 덤프는 저장소에 넣지 않는다(`.gitignore`의 `*.dump`).
4. API 중지, `psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/017_remove_user_email.sql`, 새 코드 기동
5. 확인: 사전 검사 재실행(017 마커 true, 이메일 참조 0건), 일반 사용자, 보호소, 관리자 계정 각 1개 로그인
6. 복구: 017은 한 트랜잭션이라 실패하면 변경이 남지 않는다. 커밋 후 문제가 있으면 API를 중지하고 3번 덤프로 `users`를 복원한 뒤 이전 코드로 되돌린다. 이메일 값은 덤프 외에는 복구할 수 없다.

### 기존 계정 아이디 전환 (경로 C, 팀 승인 필요)

016은 기존 행이 있으면 중단되므로 별도 절차가 필요하다. 아이디를 임의로 만들지 않는다.

1. 사전 검사 10, 11절로 대상 계정 수를 역할별로 확인한다.
2. 계정별 `(user_id, login_id)` 매핑표를 만든다. 아이디는 `^[A-Za-z0-9]{4,20}$`, 소문자 기준 고유. 닉네임 20자 초과나 대소문자 중복, `password` NULL 계정 처리도 함께 정한다. 매핑표는 저장소, Issue, PR에 올리지 않는다.
3. 팀 승인 후 한 트랜잭션에서: `login_id VARCHAR(20)` NULL 허용 추가, 임시 테이블로 매핑 적용(소문자 저장), NULL/공백/중복 0건 검증, NOT NULL 설정, `uq_users_login_id_lower`와 `uq_users_nickname_lower` 생성, 닉네임 길이와 `password` NOT NULL 처리. 검증 실패 시 롤백
4. 역할별 로그인 확인 후 017 적용
5. 보호소와 관리자 계정의 아이디와 임시 비밀번호는 담당자에게 별도 안전한 경로로 전달한다.

## 6. 테스트 결과 (2026-11-23, Windows 11, `.venv`, Python 3.12)

| 테스트 | 결과 |
|---|---|
| `tests/` 전체 (SQLite, `SIGNUP_TEST_DATABASE_URL` 설정) | 433 passed, 2 failed, 82 skipped |
| `tests/test_signup_postgres.py` (PostgreSQL 16.16, 격리 테스트 DB) | 12 passed, 19 subtests, 같은 DB에서 2회 연속 |
| `scripts/precheck_017_users.sql` | 격리 테스트 DB의 임시 스키마에서 016 전, 016 후 두 상태로 실행 성공, 스키마 삭제 확인 |

- 실패 2건은 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`로, 변경 전 `origin/dev`(`c45c3b3`)에서도 같다(418 passed, 2 failed, 90 skipped).
- 건너뛴 82개는 각자 빈 DB가 필요한 다른 PostgreSQL 테스트(`WALK_TEST_DATABASE_URL` 등)다. 이 테스트들의 사용자 픽스처에서 `email`만 뺐고 실행하지 않았다.
- `scripts/verify_auth_postgres.py`는 로컬 개발 DB가 필요해 실행하지 않았다. 문법 검사만 통과했다.
- 017 PostgreSQL 검증: 실제 `002`, `005`~`007`, `014`~`016` 파일 적용 후 역할별 계정 3개(밑줄 있는 기존 아이디 포함)에 017 적용. 이메일 컬럼, 인덱스, 제약만 사라지고 다른 테이블, 인덱스, 제약, 행이 그대로임을 확인. `create_all()` 없이 세 역할 로그인 성공, 이메일 로그인 401, 기존 아이디와 대소문자만 다른 신규 가입 409. 016 미적용, 공백 아이디, 이메일 의존 뷰가 있으면 중단되고 아무것도 바뀌지 않음을 확인

## 7. 남은 작업과 위험

- 개발 DB 사전 검사 미실행: 이 PC에 개발 DB 접속 정보(`DATABASE_URL`)가 없다. 5절 1번을 접속 권한이 있는 사람이 실행해야 한다.
- 017 개발 DB 미적용. 이 브랜치를 `dev`에 병합하면 017 적용 전까지 가입이 실패한다.
- #31: `SignupRequest`에 `privacy` 추가, 가입 트랜잭션에서 동의 이력 저장
- #32 남은 항목: 4절
- 결정 대기: 가입 요청의 `email`을 무시할지 거부할지(현재 무시), 이메일 UNIQUE를 대신할 다계정 방지 수단
- `requirements.txt`의 `email_validator`는 앱 코드 사용처가 없어졌지만 유지했다. FastAPI 표준 의존성 목록을 고정한 파일이라 별도 정리가 필요하다.
- Notion 로그인 명세는 `INVALID_IDENTIFIER_FORMAT`을 가입 규칙 위반과 이메일 입력에도 쓰도록 적혀 있다. 구현은 Issue #32의 2026-11-23 결정(가입 규칙 미적용, 100자 초과만 422, 이메일은 401)을 따랐다.
