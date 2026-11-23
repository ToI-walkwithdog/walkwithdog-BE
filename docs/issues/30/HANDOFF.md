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
| 429 | `TOO_MANY_REQUESTS` | 클라이언트 IP별 60초에 10회 초과 | `{"retry_after_seconds"}` | 로그인 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요. |
| 500 | `INTERNAL_SERVER_ERROR` | 세션 저장 실패 등 | `{}` | 일시적인 오류가 발생했습니다. |

- 401에는 `WWW-Authenticate: Bearer` 헤더가 붙는다.
- 처리 순서: 입력 검증, `identifier` 정규화(앞뒤 공백 제거, ASCII면 소문자), `lower(login_id)`로 조회, scrypt 검증(계정이 없거나 해시가 없어도 더미 해시로 1회 계산), `AuthSession`에 토큰 해시 저장과 커밋, 응답
- 로그인은 가입 규칙(4~20자, 영문과 숫자)을 다시 적용하지 않는다. 밑줄이 있는 기존 아이디도 로그인할 수 있다.
- 요청 제한은 입력 검증보다 먼저 실행된다. 422와 성공 요청도 횟수에 포함되고, 제한 중에는 올바른 비밀번호도 429다.

### POST /auth/logout (#32에서 변경)

- 성공: HTTP 200 `{"code":"OK","data":{},"message":"로그아웃이 완료되었습니다."}`, `Cache-Control: no-store`. 기존 204에서 바뀌었다.
- 현재 토큰의 세션만 삭제한다. 같은 사용자의 다른 세션은 유지된다.
- 401: `AUTHENTICATION_REQUIRED`(헤더 없음, Bearer 아님), `INVALID_ACCESS_TOKEN`(위조, 이미 로그아웃, 256자 초과), `SESSION_EXPIRED`. 응답 유실 후 재시도하면 `INVALID_ACCESS_TOKEN`이므로 FE는 로그아웃 완료로 처리해도 된다.

### GET /users/me (#32에서 변경)

- 성공: HTTP 200 `{"code":"OK","data":{"id","login_id","nickname","role","shelter_id"},"message":"사용자 정보를 조회했습니다."}`, `Cache-Control: no-store`
- 401: 로그아웃의 3종에 `USER_NOT_FOUND`(세션의 사용자가 없음)를 더한 4종. 성공과 401 모두 `no-store`, 401에는 `WWW-Authenticate: Bearer`
- 조회 중 DB 쓰기를 하지 않는다. 만료 세션도 삭제하지 않는다.
- `account_status`, 서약과 Step 1~3 상태 필드는 없다. #45(온보딩 진행 상태)와 #33(서약)에서 같은 `data`에 추가한다.

### GET /auth/me 제거

- 호출처 확인: FE `main`(`04e497e`)은 네트워크 호출이 없는 목업이고, 조직의 다른 저장소는 FE뿐이며, BE 코드와 스크립트에서도 호출하지 않는다. 테스트만 사용하고 있어 `/users/me`로 바꿨다.
- 제거 후 `/auth/me`는 404다.

### 그 밖의 사용자 API

- `GET /users/{user_id}`, `PATCH /users/{user_id}/shelter`는 응답 본문 형식과 `detail` 401을 유지한다. 필드는 `id`, `login_id`, `nickname`, `role`, `shelter_id`
- 다른 비온보딩 API도 기존 `get_current_user`의 `detail` 401을 유지한다. 계약 형식 401이 필요한 온보딩 API는 `app.auth.get_contract_user`(또는 `get_contract_session`)를 쓴다.

## 2. 검증 규칙

- 아이디: 앞뒤 공백 제거, ASCII 입력만 소문자 변환, `^[a-z0-9]{4,20}$`. 소문자 변환 후 검사하므로 FE 정규식 `^[A-Za-z0-9]{4,20}$`와 결과가 같다. 저장은 소문자다. ASCII가 아닌 문자는 소문자로 바꾸지 않아 형식 오류가 된다(예: 켈빈 기호 U+212A).
- 닉네임: 변경 없음. NFC 정규화 후 앞뒤 공백 제거, `^[가-힣A-Za-z0-9_]{3,20}$`, 중복은 `lower()`로 비교
- 비밀번호: 변경 없음. 정규화 없이 15~128자, 영문 대문자, 소문자, 숫자, 특수문자 각 1개 이상. scrypt
- 고유성: `uq_users_login_id_lower`, `uq_users_nickname_lower`. UNIQUE 위반은 제약 이름으로 필드를 판별하고, 판별하지 못하면 롤백 후 다시 조회한다. 그래도 알 수 없으면 500이다.

## 3. Rate Limit

| 대상 | 기본값 | 환경변수 |
|---|---|---|
| `POST /users/` | IP당 600초에 5회 | `SIGNUP_RATE_LIMIT_*` |
| 아이디, 닉네임 중복 확인 합산 | IP당 60초에 30회 | `AVAILABILITY_RATE_LIMIT_*` |
| `POST /auth/login` | IP당 60초에 10회 | `LOGIN_RATE_LIMIT_REQUESTS`, `LOGIN_RATE_LIMIT_WINDOW_SECONDS` |

- 세 제한은 따로 센다. 프로세스 메모리 저장소라 다중 worker에서는 정확하지 않다. 운영 정책(횟수, 공유 IP, 공유 저장소)은 미확정이다.
- 로그인 제한은 IP 단위다. 아이디 단위 잠금은 없다.

## 4. 이메일 입력 정책 검토 (결정 대기, 코드 변경 없음)

- 현재: `POST /users/`에 `email`이 오면 무시한다. `role`, `shelter_id` 등 다른 추가 필드도 같은 방식이다.
- 영향 범위: 계정 생성 API는 `POST /users/` 하나다(역할은 항상 `user`). 보호소와 관리자 계정을 만드는 API는 없다. FE는 아직 서버를 호출하지 않는다.
- 로그인: `email`만 보내면 `identifier` 누락으로 422 `IDENTIFIER_REQUIRED`가 되어 이미 명시적으로 거부된다.

## 5. Notion과 구현의 차이

| API | Notion | 구현 | 근거 |
|---|---|---|---|
| `POST /auth/login` | 아이디 길이, 허용 문자 위반과 이메일 입력도 422 `INVALID_IDENTIFIER_FORMAT` | 100자 초과만 422, 그 밖에는 조회 후 401 | Issue #32 2026-11-23 결정. 밑줄 있는 기존 아이디의 로그인을 막지 않기 위함 |
| `GET /users/me` | `account_status`, 서약, Step 1~3 필드 포함 | 기본 사용자 필드만 | #45, #33 범위 |
| 로그아웃, `/users/me` 401 코드 | 명세와 동일 | 동일 | 이번 구현 |

## 6. 마이그레이션과 배포 순서

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

### 개발 DB 사전 검사 (접속 권한이 있는 사람이 실행)

- 최소 권한: 대상 DB `CONNECT`, `users`가 있는 스키마 `USAGE`, `users` `SELECT`. 시스템 카탈로그(`information_schema`, `pg_indexes`, `pg_views`, `pg_proc`, `pg_depend`)는 기본 권한으로 읽힌다. 쓰기 권한은 필요 없다. 가능하면 읽기 전용 계정을 쓴다.
- 실행: 비밀번호는 명령줄, 연결 문자열, 셸 기록에 넣지 않는다. `-W`로 입력받거나 본인 PC의 `pgpass.conf`를 쓴다.

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -X -v ON_ERROR_STOP=1 -h <호스트> -U <읽기 계정> -d <DB 이름> -W -f scripts/precheck_017_users.sql *> precheck_017_result.txt
```

  - `users`가 `public`이 아닌 스키마에 있으면 실행 전에 `$env:PGOPTIONS="-csearch_path=<스키마>"`를 지정한다.
  - 스크립트는 `BEGIN TRANSACTION READ ONLY`로 시작해 `ROLLBACK`으로 끝난다. 쓰기가 섞이면 DB가 거부한다.
- 결과 공유: 출력은 개수, 컬럼, 인덱스, 객체 이름뿐이다. 공유 전에 파일에 호스트, 계정, 비밀번호, 이메일, 아이디 값이 없는지 확인하고 PR 댓글로 붙인다. `precheck_017_result.txt`는 커밋하지 않는다.
- 중단 조건: 아래 중 하나라도 해당하면 016, 017과 데이터 변경을 하지 않고 결과만 보고한다. 절 번호는 스크립트 출력의 `== N.` 번호다.
  - 스크립트 오류로 끝까지 실행되지 않음(권한 부족, `users` 없음 등)
  - 5절 `017 users.email removed`가 이미 true(적용 여부 확인 필요)
  - 1절 계정이 있는데 5절 `016 users.login_id`가 false(경로 C, 매핑표 승인 필요)
  - 10절 `null_id`, `blank_id`, `duplicate_groups` 중 하나라도 0보다 큼
  - 6절에 `users.email` 외 이메일 컬럼, 7절에 이메일을 쓰는 뷰나 함수, 8절에 의존 객체가 있음
  - 9절 `null_password` 또는 `not_scrypt`가 0보다 큼(해당 계정은 전환 후에도 로그인할 수 없음, 처리 방법 결정 필요)
  - 11절 `over_20_chars` 또는 `duplicate_groups`가 0보다 큼이고 016이 미적용
- 참고만 하는 항목: 10절 `breaks_new_rule`, `has_underscore`는 로그인을 막지 않는다. 개수만 보고한다.

### 개발 DB 적용 절차 (사전 검사 보고와 승인 후)

1. 위 사전 검사 결과를 보고하고 승인을 받는다.
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

## 7. 테스트 결과 (2026-11-23, Windows 11, `.venv`, Python 3.12)

| 테스트 | 결과 |
|---|---|
| `tests/` 전체 (SQLite, `SIGNUP_TEST_DATABASE_URL` 설정), 2차 | 440 passed, 2 failed, 82 skipped |
| `tests/` 전체, 1차 | 433 passed, 2 failed, 82 skipped |
| `tests/test_signup_postgres.py` (PostgreSQL 16.16, 격리 테스트 DB) | 12 passed, 19 subtests, 같은 DB에서 2회 연속 |
| `scripts/precheck_017_users.sql` | 격리 테스트 DB의 임시 스키마에서 016 전, 016 후 두 상태로 실행 성공, 스키마 삭제 확인 |

- 실패 2건은 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`로, 변경 전 `origin/dev`(`c45c3b3`)에서도 같다(418 passed, 2 failed, 90 skipped).
- 건너뛴 82개는 각자 빈 DB가 필요한 다른 PostgreSQL 테스트(`WALK_TEST_DATABASE_URL` 등)다. 이 테스트들의 사용자 픽스처에서 `email`만 뺐고 실행하지 않았다.
- `scripts/verify_auth_postgres.py`는 로컬 개발 DB가 필요해 실행하지 않았다. 문법 검사만 통과했다.
- 017 PostgreSQL 검증: 실제 `002`, `005`~`007`, `014`~`016` 파일 적용 후 역할별 계정 3개(밑줄 있는 기존 아이디 포함)에 017 적용. 이메일 컬럼, 인덱스, 제약만 사라지고 다른 테이블, 인덱스, 제약, 행이 그대로임을 확인. `create_all()` 없이 세 역할 로그인 성공, 이메일 로그인 401, 기존 아이디와 대소문자만 다른 신규 가입 409. 016 미적용, 공백 아이디, 이메일 의존 뷰가 있으면 중단되고 아무것도 바뀌지 않음을 확인

## 8. 남은 작업과 위험

- 개발 DB 사전 검사 미실행: 이 PC에 개발 DB 접속 정보(`DATABASE_URL`)가 없다. 6절 "개발 DB 사전 검사"를 접속 권한이 있는 사람이 실행해야 한다.
- 017 개발 DB 미적용. 이 브랜치를 `dev`에 병합하면 017 적용 전까지 가입이 실패한다.
- #31: `SignupRequest`에 `privacy` 추가, 가입 트랜잭션에서 동의 이력 저장
- #32: 코드 항목은 구현했다. Notion 로그인 명세의 422 범위 차이(5절)는 Notion 담당자 확인이 남았다.
- #45: `GET /users/me`의 온보딩 진행 상태 필드
- 결정 대기: 가입 요청의 `email`을 무시할지 거부할지(현재 무시, 4절), 이메일 UNIQUE를 대신할 다계정 방지 수단, 로그인 제한 운영값
- `requirements.txt`의 `email_validator`는 앱 코드 사용처가 없어졌지만 유지했다. FastAPI 표준 의존성 목록을 고정한 파일이라 별도 정리가 필요하다.
