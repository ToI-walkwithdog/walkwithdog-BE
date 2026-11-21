# 인수인계: 온보딩 회원가입 (Issue #30)

- 날짜: 2026-11-21 (KST)
- 브랜치: `feature/onboarding-signup` (기준 `origin/dev` eba6d4b)
- 작업 요약: 로그인 아이디 도입, 닉네임과 아이디 사전 중복 확인 API, 가입 입력 검증 강화, 아이디와 닉네임과 이메일의 대소문자 무시 DB 고유성.
- 제외: 개인정보 동의(#31), 아이디 또는 이메일 로그인과 `/users/me` 변경(#32), 최초 서약(#33), 통합 테스트(#34), 이메일 인증과 비밀번호 재설정(#14).

## 1. 변경 파일

- `app/api_contract.py` (신규): `ContractRoute`를 쓰는 라우트에만 `{code, data, message}` 응답 적용. 요청 검증 오류는 422, 예상하지 못한 예외는 500 `INTERNAL_SERVER_ERROR`로 변환하고 로그에는 예외 타입만 남김. 429 응답 헬퍼 `too_many_requests()` (현재 호출하는 곳 없음).
- `app/account_rules.py` (신규): 아이디, 닉네임, 이메일 정규화와 비밀번호 규칙. 가입과 중복 확인 API가 같은 함수를 사용.
- `app/signup_service.py` (신규): 중복 사전 조회, 계정 저장, UNIQUE 위반 시 롤백과 충돌 필드 판별.
- `app/models.py`: `User.login_id` 추가, `nickname` 50자에서 20자, `password` NOT NULL, `lower()` 고유 인덱스 3종.
- `app/schemas.py`: `UserCreate`를 `SignupRequest`로 교체, 가입 및 중복 확인 응답 스키마 추가.
- `app/routers/users.py`: 가입 API 교체, 중복 확인 API 2종 추가.
- `app/auth.py`: 로그인 불가 계정 표시값 `UNUSABLE_PASSWORD = "!"` 추가 (scrypt 해시가 아니므로 항상 검증 실패).
- `migrations/014_onboarding_signup.sql` (신규)
- `scripts/seed_demo.py`: 데모 계정에 `login_id`와 `UNUSABLE_PASSWORD` 지정, 아이디와 닉네임 충돌 시 저장 없이 중단.
- `scripts/verify_auth_postgres.py`: 새 가입 요청과 응답 형식 반영.
- 테스트: `tests/test_signup.py`, `tests/test_signup_postgres.py` (신규). 기존 fixture 10개 파일에 `login_id`, `password` 추가.

## 2. API 계약 (FE 참고)

모든 응답은 `{"code", "data", "message"}`. 다른 `/users` API의 오류 형식(`detail`)은 바뀌지 않았다.

### POST /users/

요청: `login_id`, `nickname`, `email`, `password`. 그 외 필드(`role`, `shelter_id` 등)는 무시한다. 비밀번호 확인은 FE에서 처리한다.

성공 HTTP 200 `OK`, 메시지 `회원가입이 완료되었습니다.`

```json
{"code": "OK", "data": {"id": 25, "login_id": "walker04", "nickname": "산책왕", "email": "walker@example.com", "role": "user", "shelter_id": null}, "message": "회원가입이 완료되었습니다."}
```

토큰은 발급하지 않는다. 로그인은 기존 `POST /auth/login`(이메일)을 사용한다.

| HTTP | code | data |
|---|---|---|
| 422 | `REQUIRED_FIELD` | `{"field"}` |
| 422 | `INVALID_LOGIN_ID_LENGTH` | `{"field":"login_id","min_length":4,"max_length":20}` |
| 422 | `INVALID_LOGIN_ID_FORMAT` | `{"field":"login_id"}` |
| 422 | `INVALID_NICKNAME_LENGTH` | `{"field":"nickname","min_length":3,"max_length":20}` |
| 422 | `INVALID_NICKNAME_FORMAT` | `{"field":"nickname"}` |
| 422 | `INVALID_EMAIL` | `{"field":"email"}` |
| 422 | `INVALID_PASSWORD_LENGTH` | `{"field":"password","min_length":15,"max_length":128}` |
| 422 | `PASSWORD_UPPERCASE_REQUIRED` 등 4종 | `{"field":"password","rule":"uppercase"}` (`lowercase`, `number`, `special`) |
| 409 | `LOGIN_ID_ALREADY_EXISTS`, `NICKNAME_ALREADY_EXISTS`, `EMAIL_ALREADY_EXISTS` | `{"field"}` |
| 500 | `INTERNAL_SERVER_ERROR` | `{}` |

- 여러 필드가 틀리면 `login_id`, `nickname`, `email`, `password` 순서의 첫 오류 하나만 반환한다. 중복도 같은 순서로 판정한다.
- 필드 누락, `null`, 빈 문자열, 공백만 있는 값(비밀번호 제외), 문자열이 아닌 값은 `REQUIRED_FIELD`. 본문이 없거나 JSON이 아니면 `{"field":"login_id"}`.

### GET /users/id-availability?login_id=

- 성공 HTTP 200 `{"available": true|false}`. 메시지 `사용 가능한 아이디입니다.` 또는 `이미 사용 중인 아이디입니다.`
- 422 `LOGIN_ID_REQUIRED`, `INVALID_LOGIN_ID_LENGTH`, `INVALID_LOGIN_ID_FORMAT`.

### GET /users/nickname-availability?nickname=

- 성공 HTTP 200 `{"available": true|false}`. 메시지 `사용 가능한 닉네임입니다.` 또는 `이미 사용 중인 닉네임입니다.`
- 422 `NICKNAME_REQUIRED`, `INVALID_NICKNAME_LENGTH`, `INVALID_NICKNAME_FORMAT`.

두 API 모두 인증이 필요 없고, 값을 선점하지 않는다. 가입 시 다시 검사하며 동시 가입은 DB 고유 인덱스가 최종 방어한다.

### 429 TOO_MANY_REQUESTS

응답 계약 `{"retry_after_seconds": n}`과 `Retry-After` 헤더만 정의했다. 제한 수치가 정해지지 않아 현재 어떤 API도 429를 반환하지 않는다.

## 3. 결정 사항

- 아이디: 앞뒤 공백 제거, ASCII 입력만 소문자 변환, `^[a-z0-9_]{4,20}$`. ASCII가 아닌 문자는 소문자로 바꾸지 않아 형식 오류가 된다(예: 켈빈 기호 `K`).
- 닉네임: NFC 정규화 후 앞뒤 공백 제거, `^[가-힣A-Za-z0-9_]{3,20}$`. 입력한 대소문자는 그대로 저장하고 중복은 `lower()`로 비교한다.
- 이메일: 앞뒤 공백 제거, `email_validator`(전달 가능성 검사 없음, 기존 `EmailStr`와 같은 라이브러리), 정규화 후 전체 소문자로 저장, 최대 100자.
- 비밀번호: 공백 제거나 변형 없이 15~128자, 영문 대문자, 소문자, 숫자, 특수문자 각 1개 이상. 특수문자는 ASCII 문장부호 32종(`string.punctuation`)으로 정했다. 공백과 한글은 허용하지만 특수문자로 세지 않는다.
- 고유성: `uq_users_login_id_lower`, `uq_users_nickname_lower`, `uq_users_email_lower` (`lower(column)`). 기존 `users_email_key`는 유지한다. 사전 조회도 `lower(column) = lower(value)`로 같은 식을 쓴다.
- UNIQUE 위반: PostgreSQL 제약 이름(SQLite는 메시지)으로 필드를 판별하고, 판별하지 못하면 롤백 후 다시 조회한다. 그래도 알 수 없으면 500.
- 로그인 불가 데모 계정은 `password=NULL` 대신 `UNUSABLE_PASSWORD`를 저장한다.

## 4. DB 적용 방법

`migrations/014_onboarding_signup.sql`은 `users`가 비어 있을 때만 적용된다. 행이 있으면 한 트랜잭션 안에서 예외를 내고 아무것도 바꾸지 않는다. 기존 계정에 아이디를 임의로 만들지 않기 위해서다.

```powershell
psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/014_onboarding_signup.sql
```

개발 DB에 보존할 사용자가 있으면 적용 전에 다음을 정해야 한다(미결정).

1. 기존 계정의 `login_id` 부여 방식 (사용자 직접 설정, 관리자 지정, 계정 정리 중 선택)
2. 20자 초과 또는 대소문자 무시 중복 닉네임 처리
3. `password`가 NULL이거나 평문인 계정 처리 (`UNUSABLE_PASSWORD` 지정 여부)
4. `pg_dump` 백업과 복구 확인 후 적용

`AUTO_CREATE_SCHEMA=true`의 `create_all()`은 기존 `users` 테이블을 갱신하지 않는다.

## 5. 테스트

```powershell
$env:PYTHONPATH="."; python -m pytest tests -q
```

- 결과: 373 passed, 2 failed, 88 skipped. 실패 2건은 `tests/test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`의 `difficulty: INTERMEDIATE` 하위 케이스로, `dev`(eba6d4b)에서도 같은 결과(346 passed, 2 failed)다.
- 신규 `tests/test_signup.py`: 27 passed, 94 subtests passed.
- Windows에서는 `requirements.txt` 외에 `tzdata`가 필요하고 `uvloop`는 설치되지 않는다.

PostgreSQL 테스트(미실행):

```powershell
$env:SIGNUP_TEST_DATABASE_URL="postgresql://<user>:<password>@localhost:5432/toi_walk_test_signup"
python -m pytest tests/test_signup_postgres.py -v
```

빈 `toi_walk_test_*` 로컬 DB가 필요하다. 대소문자 무시 고유 인덱스, 제약별 409, 사전 조회 이후 선가입, 6건 동시 가입, 014 적용(빈 테이블)과 중단(기존 행 존재)을 검증한다. 기존 PostgreSQL 테스트의 `users` INSERT도 새 NOT NULL 컬럼에 맞게 고쳤지만 실행하지 못했다.

## 6. 확인하지 못한 것

- Notion 명세 4건: 로그인 없이 열리지 않아 직접 확인하지 못했다. 문자 규칙, 오류 코드, `data`는 사용자가 전달한 Notion 기준을 따랐다.
- 오류 메시지 문구: `회원가입이 완료되었습니다.`, `이미 사용 중인 아이디입니다.` 외에는 Notion 원문과 대조하지 못했다.
- PostgreSQL 통합 테스트 전체 (로컬 PostgreSQL 없음).
- 실제 개발 DB의 `users` 데이터 현황 (`.env` 없음).

## 7. 다음 작업

- #31: `SignupRequest`에 `privacy` 추가, `register_user`의 같은 트랜잭션에서 동의 이력 저장.
- #32: `/users/me`에 `login_id` 노출, 아이디 또는 이메일 로그인. 가입 응답 형식이 `data`로 감싸졌으므로 FE 가입 호출 전환 필요.
- 요청 제한 수치가 정해지면 `too_many_requests()`를 연결하고 테스트 추가.
