# 인수인계: 온보딩 회원가입 (Issue #30)

- 갱신: 2026-11-22 (KST)
- 브랜치: `feature/onboarding-signup` (`f647882`), `origin/dev`(`07111fc`, F-08·F-09 포함) 병합 진행 중
- 범위: 로그인 아이디, 아이디·닉네임 사전 중복 확인, 가입 입력 검증, 대소문자 무시 DB 고유성, 공개 API 요청 제한
- 제외: 개인정보 동의(#31), 로그인 변경(#32), 최초 서약(#33), 통합 온보딩 테스트(#34), 이메일 인증·비밀번호 재설정(#14)

## 1. API 계약

세 API 모두 `{"code", "data", "message"}`로 응답한다. 인증이 필요 없다. 다른 `/users` API는 기존 FastAPI `detail` 형식을 유지한다.

### POST /users/

- 요청: `login_id`, `nickname`, `email`, `password`. 그 외 필드(`role`, `shelter_id` 등)는 무시한다. 비밀번호 확인은 FE에서 처리한다.
- 성공: HTTP 200 `OK`, `data`에 사용자 정보(`id`, `login_id`, `nickname`, `email`, `role`, `shelter_id`)만 포함, 메시지 `회원가입이 완료되었습니다.` 토큰은 발급하지 않는다.

| HTTP | code | data | message |
|---|---|---|---|
| 422 | `REQUIRED_FIELD` | `{"field"}` | 필수 입력 항목을 확인해 주세요. |
| 422 | `INVALID_LOGIN_ID_LENGTH` | `{"field","min_length":4,"max_length":20}` | 아이디는 4~20자로 입력해 주세요. |
| 422 | `INVALID_LOGIN_ID_FORMAT` | `{"field":"login_id"}` | 아이디에는 영문 소문자, 숫자, 밑줄만 사용할 수 있습니다. |
| 422 | `INVALID_NICKNAME_LENGTH` | `{"field","min_length":3,"max_length":20}` | 닉네임은 3~20자로 입력해 주세요. |
| 422 | `INVALID_NICKNAME_FORMAT` | `{"field":"nickname"}` | 닉네임에 사용할 수 없는 문자가 포함되어 있습니다. |
| 422 | `INVALID_EMAIL` | `{"field":"email"}` | 올바른 이메일을 입력해 주세요. |
| 422 | `INVALID_PASSWORD_LENGTH` | `{"field","min_length":15,"max_length":128}` | 비밀번호는 15~128자로 입력해 주세요. |
| 422 | `PASSWORD_UPPERCASE_REQUIRED` 외 3개 | `{"field":"password","rule"}` | 비밀번호에 영문 대문자를 1개 이상 포함해 주세요. (소문자, 숫자, 특수문자 동일 형식) |
| 409 | `LOGIN_ID_ALREADY_EXISTS` / `NICKNAME_ALREADY_EXISTS` / `EMAIL_ALREADY_EXISTS` | `{"field"}` | 이미 사용 중인 아이디/닉네임/이메일입니다. |
| 429 | `TOO_MANY_REQUESTS` | `{"retry_after_seconds"}` | 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요. |
| 500 | `INTERNAL_SERVER_ERROR` | `{}` | 일시적인 오류가 발생했습니다. |

- 여러 필드가 틀리면 `login_id`, `nickname`, `email`, `password` 순서로 첫 오류 하나만 반환한다. 중복도 같은 순서로 판정한다.
- 필드 누락, `null`, 빈 문자열, 공백만 있는 값(비밀번호 제외), 문자열이 아닌 값은 `REQUIRED_FIELD`다. 본문이 없거나 JSON이 아니면 `{"field":"login_id"}`다.

### GET /users/id-availability?login_id=

- 성공 200 `{"available": true|false}`, 메시지 `사용 가능한 아이디입니다.` 또는 `이미 사용 중인 아이디입니다.`
- 422 `LOGIN_ID_REQUIRED`(아이디를 입력해 주세요.), `INVALID_LOGIN_ID_LENGTH`, `INVALID_LOGIN_ID_FORMAT`(아이디에 사용할 수 없는 문자가 포함되어 있습니다.)
- 형식 오류 문구만 가입과 다르다. 검증 함수는 하나이며 `format_message` 인자로 문구만 바꾼다.

### GET /users/nickname-availability?nickname=

- 성공 200 `{"available": true|false}`, 메시지 `사용 가능한 닉네임입니다.` 또는 `이미 사용 중인 닉네임입니다.`
- 422 `NICKNAME_REQUIRED`(닉네임을 입력해 주세요.), `INVALID_NICKNAME_LENGTH`, `INVALID_NICKNAME_FORMAT`

사전 조회는 값을 선점하지 않는다. 가입 시 다시 확인하고 DB 고유 인덱스가 최종 방어선이다.

## 2. 검증 규칙

- 아이디: 앞뒤 공백 제거, ASCII 입력만 소문자 변환, `^[a-z0-9_]{4,20}$`. ASCII가 아닌 문자는 소문자로 바꾸지 않아 형식 오류가 된다(예: 켈빈 기호 U+212A).
- 닉네임: Unicode NFC 정규화 후 앞뒤 공백 제거, `^[가-힣A-Za-z0-9_]{3,20}$`. 입력한 대소문자 그대로 저장하고 중복은 `lower()`로 비교한다.
- 이메일: 앞뒤 공백 제거, `email_validator`(도달 가능성 검사 없음), 정규화 후 전체 소문자로 저장, 최대 100자.
- 비밀번호: 공백 제거나 정규화 없이 길이 15~128자, 영문 대문자·소문자·숫자·특수문자(`string.punctuation` 32자) 각 1개 이상. scrypt로 해싱한다.
- 고유성: `uq_users_login_id_lower`, `uq_users_nickname_lower`, `uq_users_email_lower` (`lower(column)`). 기존 `users_email_key`도 유지한다.
- UNIQUE 위반: PostgreSQL 제약 이름(SQLite는 메시지)으로 필드를 판별하고, 판별하지 못하면 롤백 후 다시 조회한다. 그래도 알 수 없으면 500이다.

## 3. Rate Limit

- 구현: `app/rate_limit.py`, 라우트 연결은 `app/routers/users.py`
- 알고리즘: Sliding Window Log. 어느 구간에서도 설정 횟수를 넘지 않는다. 허용된 요청만 기록하고, `retry_after_seconds`는 가장 오래된 요청이 구간을 벗어날 때까지 남은 초(올림, 최소 1)다. 헤더 `Retry-After`와 본문 값이 같다.
- 기본값(초기 제안, 운영 정책 미확정)

| 대상 | 기본값 | 환경변수 |
|---|---|---|
| `POST /users/` | IP당 600초에 5회 | `SIGNUP_RATE_LIMIT_REQUESTS`, `SIGNUP_RATE_LIMIT_WINDOW_SECONDS` |
| 아이디·닉네임 중복 확인 합산 | IP당 60초에 30회 | `AVAILABILITY_RATE_LIMIT_REQUESTS`, `AVAILABILITY_RATE_LIMIT_WINDOW_SECONDS` |

- 가입과 중복 확인은 따로 센다. 제한 검사는 입력 검증보다 먼저 실행되므로 422 요청도 횟수에 포함된다. JSON 형식이 깨진 요청은 FastAPI가 먼저 거절해 포함되지 않는다.
- 요청 식별: `request.client.host`. `X-Forwarded-For`를 직접 읽지 않는다. 프록시 뒤에서는 uvicorn `--forwarded-allow-ips`에 프록시 IP를 지정해야 하며, 지정하지 않으면 모든 요청이 프록시 IP 하나로 묶인다.
- **제약: 저장소가 프로세스 메모리다. worker나 서버가 여러 개면 각자 세므로 실제 허용량이 그 수만큼 늘어난다. 다중 worker 운영 준비는 끝나지 않았다.** 공유 저장소는 `hit(key, limit, window)`만 구현해 교체하면 된다.
- 저장소 장애: 요청을 허용한다(fail-open). 로그에는 예외 종류만 남긴다.

## 4. 마이그레이션

- 파일: `migrations/016_onboarding_signup.sql` (F-08과 번호가 겹쳐 기존 014에서 016으로 변경, SQL 주석의 파일명과 오류 메시지 번호도 `016`으로 수정, 내용과 데이터 보존 정책은 동일)
- 순서: `014_f08_live_walk.sql`(F-08, `users.phone` 추가) → `015_f09_walk_review.sql`(F-09) → `016_onboarding_signup.sql`
- 016은 `users`만 바꾼다(`login_id` 추가, `nickname` 20자, `password` NOT NULL, 고유 인덱스 3개). `phone`과 다른 테이블은 건드리지 않는다.
- 016은 `users`가 비어 있을 때만 적용된다. 행이 있으면 `016: users has existing rows`로 트랜잭션 전체가 롤백된다. 이미 적용한 DB에 사용자가 생긴 뒤 다시 실행해도 같은 이유로 거절되며 아무것도 바뀌지 않는다.

```powershell
psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/016_onboarding_signup.sql
```

- 마이그레이션 적용 이력을 기록하는 테이블이 없어 개발 DB의 적용 여부는 코드로 확인할 수 없다. 개발 DB에는 적용하지 않았다.
- `AUTO_CREATE_SCHEMA=true`의 `create_all()`은 기존 `users`를 갱신하지 않는다. 마이그레이션 대용으로 쓰지 않는다.

## 5. 테스트 결과 (2026-11-22, Windows 11, `.venv`)

| 테스트 | 결과 |
|---|---|
| `tests/test_signup.py` + `tests/test_rate_limit.py` | 50 passed, 113 subtests |
| `tests/test_f08_live_walk.py` + `tests/test_f09_walk_review.py` | 22 passed |
| `tests/test_walk_domain.py` + `tests/test_auth.py` + `tests/test_shelter_permissions.py` | 53 passed, 79 subtests |
| `tests/test_signup_postgres.py` (PostgreSQL 16.16) | 8 passed, 18 subtests, 같은 DB에서 2회 연속 |
| `tests/` 전체 | 426 passed, 2 failed, 82 skipped |

- 전체 실패 2건은 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`이며 `origin/dev`(07111fc) 단독에서도 같다(368 passed, 2 failed).
- 건너뛴 82개는 DB 주소 환경변수가 없는 다른 PostgreSQL 테스트다.
- PostgreSQL 테스트는 `SIGNUP_TEST_DATABASE_URL`에 빈 로컬 `toi_walk_test_*` DB가 필요하다. 테스트마다 `su_<uuid>` 스키마를 만들고, 이번 실행이 만든 것·테스트 계정 소유·테스트 DB인 경우에만 삭제한다. 실패해도 정리한다.
- F-08·F-09 이후 016 검증: 실제 `005 → 006 → 007 → 014 → 015` 파일을 적용한 스키마에 016을 적용해 `phone` 유지, 다른 테이블·인덱스·제약 불변, F-09 테이블과 제약 유지, `create_all()` 없이 가입 API 동작을 확인했다. 기존 행(전화번호 포함)이 있으면 스키마와 데이터 모두 그대로다.

## 6. 남은 작업과 위험

- 개발 DB: 기존 사용자가 있으면 016이 적용되지 않는다. `login_id` 부여, 닉네임 길이·중복, NULL·평문 비밀번호 처리, `pg_dump` 백업 확인 순서를 먼저 정해야 한다.
- Rate Limit 운영: 횟수·기간 확정, 공유 IP(학교·회사·공용 Wi-Fi), worker 수, 공유 저장소(Redis 등), 프록시 설정, 장애 시 허용·차단 정책
- Notion 명세 4개는 로그인 화면만 받아져 직접 대조하지 못했다. 오류 메시지는 요청서의 Notion 표를 기준으로 했다.
- #31: `SignupRequest`에 `privacy` 추가, `register_user`와 같은 트랜잭션에서 동의 이력 저장
- #32: `/users/me`에 `login_id` 노출 여부, 아이디 로그인 여부
- #13: 로그인 Rate Limit은 `RateLimiter`를 재사용할 수 있다.
