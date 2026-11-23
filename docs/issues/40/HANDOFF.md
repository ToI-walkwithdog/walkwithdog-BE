# 인수인계: 온보딩 Step 1 기본 정보 입력과 SMS 인증 (Issue #40)

- 작성: 2026-11-23 (KST). 이 문서는 구현 후에 작성했다. 코드와 테스트는 바꾸지 않았다.
- 브랜치: `feature/onboarding-basic-info`, 기능 커밋 `a15329b`
- PR: [Draft PR #52](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/52), 대상 브랜치 `feature/onboarding-pledge`(#50)
- 근거: [Issue #40](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/40)과 "최종 결정 반영 (2026-11-23)", Notion Step 1 명세 4개(아래 링크)
- 상태: `dev` 미병합, 개발 DB 미적용. **실제 SOLAPI 발송과 단말 수신 테스트는 하지 않았다.** SMS는 Mock 공급자로만 검증했다.

## 1. 개요와 범위

서약을 마친 일반 사용자가 실명, 생년월일, SMS로 인증한 휴대전화 번호를 저장하면 온보딩 Step 1이 완료된다. 계정 상태, 만 나이 계산, 첫 미완료 단계 계산 같은 공통 기능도 이 Issue에서 만들었고, #33 서약 API에 연령 확인 필요 계정 차단을 연결했다.

| 구분 | 내용 |
|---|---|
| 포함 | API 4개, `users.account_status`와 `age_review_required_at`, Step 1 테이블 3개, 만 14세 미만 상태 전환, SMS 상태 관리, Mock 공급자, SOLAPI 인터페이스 자리, 인증 기록 정리 함수와 수동 스크립트, #33 연결 |
| 제외 | 연령 정정과 가입 종료(#41), 실제 SOLAPI 계정과 발신번호, `users.phone` 변경, 완료한 기본 정보 수정, `010` 외 번호 |

## 2. 주요 변경 파일

| 파일 | 내용 |
|---|---|
| `app/account_status.py` | `ACTIVE`, `AGE_REVIEW_REQUIRED`, `ensure_active()`, `lock_user()`, `change_to_age_review()` |
| `app/onboarding_common.py` | 테스트 시계 `get_clock`, KST 날짜, `age_years()`, 단계 코드, `get_onboarding_user`, `first_incomplete_step()` |
| `app/basic_info_service.py` | 이름, 생년월일 검증, 조회, 저장 |
| `app/phone_verification_service.py` | 번호 정규화와 마스킹, HMAC, 발송 예약과 상태 전이, 확인, 정리 |
| `app/sms_provider.py` | 공급자 인터페이스, `MockSmsProvider`, `SolapiSmsProvider` 자리 표시자 |
| `app/routers/onboarding_basic.py` | API 4개 |
| `app/pledge_service.py`, `app/routers/users.py` | #33 서약의 상태 검증 연결 |
| `app/models.py`, `app/main.py` | 모델 3개와 `users` 컬럼 2개, 라우터 등록 |
| `migrations/024_onboarding_basic_info.sql` | 컬럼 2개, 테이블 3개 |
| `scripts/purge_phone_verifications.py` | 인증 기록 정리 수동 실행(기본 dry-run) |
| `.env.example` | 설정 이름(값 없음) |
| `tests/test_onboarding_basic_info.py`, `tests/test_pledge.py`, `tests/test_signup_postgres.py` | 테스트 |

## 3. API 계약

모든 API는 `{code, data, message}` 형식이고 사용자 ID를 요청에서 받지 않는다.

공통 검사 순서: Bearer 인증(401 3종과 `USER_NOT_FOUND`) → `role=user`(403 `ONBOARDING_NOT_ALLOWED`) → 계정 상태(403 `AGE_REVIEW_REQUIRED`) → 현재 버전 서약(409 `PREVIOUS_STEP_NOT_COMPLETED`, `data.required_step=PLEDGE`, "최초 산책 서약을 먼저 완료해 주세요.") → API별 검증. 서약 전에는 SMS 발송, 기록, 카운터 증가가 없다.

| Method | 경로 | 성공 200 `data` | 주요 오류 |
|---|---|---|---|
| GET | `/users/me/onboarding/basic-info` | `step1_completed`, `profile`(`full_name`, `birth_date`, `age_years`, `phone_masked`, `phone_verified_at`, `completed_at`) 또는 `null`, `next_step` | 공통 오류만. DB 쓰기와 SMS 없음, `no-store` |
| POST | `/users/me/phone-verifications` | `verification_id`, `expires_in_seconds`(300), `resend_after_seconds`(60) | 422 `PHONE_NUMBER_REQUIRED`, `INVALID_PHONE_NUMBER` / 409 `PHONE_ALREADY_REGISTERED` / 429 `PHONE_VERIFICATION_RATE_LIMITED`(`retry_after_seconds`) / 503 `SMS_DELIVERY_UNAVAILABLE` |
| POST | `/users/me/phone-verifications/confirm` | `verified`, `verified_at` | 422 `INVALID_VERIFICATION_PAYLOAD`, `VERIFICATION_CODE_INVALID` / 404 `VERIFICATION_NOT_FOUND` / 410 `VERIFICATION_EXPIRED` / 409 `VERIFICATION_NOT_SENT`, `VERIFICATION_ALREADY_CONSUMED` / 429 `VERIFICATION_ATTEMPTS_EXCEEDED` |
| PUT | `/users/me/onboarding/basic-info` | `user_id`, `full_name`, `birth_date`, `age_years`, `phone_masked`, `phone_verified_at`, `step1_completed`, `completed_at`, `next_step`(`PET_EXPERIENCE`) | 422 `REQUIRED_FIELD`, `INVALID_FULL_NAME`, `INVALID_BIRTH_DATE`, `AGE_BELOW_MINIMUM`(`minimum_age` 14), `AGE_ABOVE_MAXIMUM`(`maximum_age` 120) / 404 `VERIFICATION_NOT_FOUND` / 409 `PHONE_VERIFICATION_REQUIRED`, `BASIC_INFO_ALREADY_COMPLETED`, `PHONE_ALREADY_REGISTERED`. `no-store` |

- 오류 메시지는 Notion Step 1 명세 문구를 따른다. 공통으로 500 `INTERNAL_SERVER_ERROR`가 있다.
- 미완료 조회는 404가 아니라 200, `profile=null`, `next_step=BASIC_INFORMATION`이다.

## 4. 입력 검증

- 실명: Unicode NFC 정규화 후 앞뒤 공백 제거, 내부 공백 유지, 2~10자, 제어 문자 불가(`INVALID_FULL_NAME`)
- 생년월일: `YYYY-MM-DD` 형식의 실존 날짜. 미래 날짜는 `INVALID_BIRTH_DATE`. `2002.12.10` 같은 형식도 422다.
- 만 나이: `age_years()`가 Asia/Seoul 날짜 기준으로 계산한다. 2월 29일 출생자는 평년에 3월 1일에 나이가 오른다. 만 14~120세(양 끝 포함)만 허용한다. Asia/Seoul은 고정 +09:00으로 계산한다(한국은 일광절약시간이 없고 `tzdata`에 의존하지 않음).
- 만 14세 미만: 별도 트랜잭션에서 `users` 행을 `FOR UPDATE`로 잠그고 `account_status=AGE_REVIEW_REQUIRED`, `age_review_required_at`(처음 전환 시각만)을 커밋한 뒤 422 `AGE_BELOW_MINIMUM`. 생년월일과 기본 정보는 저장하지 않고 서약, 동의 이력은 유지한다.
- 만 120세 초과: 422 `AGE_ABOVE_MAXIMUM`, 상태는 바꾸지 않는다.
- 전화번호: 숫자와 하이픈 입력(`010-?\d{4}-?\d{4}`), 국내 `010`만 허용, `+8210...` E.164로 저장. 응답은 `010-****-5678` 형식으로 마스킹한다.

## 5. DB와 마이그레이션

`migrations/024_onboarding_basic_info.sql` (한 트랜잭션, 재실행 시 변경 없음, 되돌리는 SQL은 파일 주석)

- `users.account_status VARCHAR(24) NOT NULL DEFAULT 'ACTIVE'`, CHECK `ck_users_account_status`(`ACTIVE`, `AGE_REVIEW_REQUIRED`). 기존 행은 기본값으로 `ACTIVE`가 된다(PostgreSQL 11 이상에서 메타데이터 변경).
- `users.age_review_required_at TIMESTAMPTZ`: 처음 연령 확인 필요 상태가 된 시각. #41 정정 성공 시 `NULL`
- `volunteer_onboarding_basics`: `user_id` PK와 FK, `full_name VARCHAR(10)`, `birth_date DATE`, `phone_e164 VARCHAR(16)` UNIQUE, `phone_verified_at`, `completed_at`, `updated_at`. 행이 있으면 Step 1 완료다.
- `phone_verifications`: UUID PK(`verification_id`), `user_id` FK, `phone_e164`, `code_digest`(HMAC), `send_status` CHECK 5종, 공급자 메시지 ID와 그룹 ID, `provider_request_key`, `last_checked_at`, `expires_at`, `attempt_count`(0 이상), `verified_at`, `verified_valid_until`, `consumed_at`, `created_at`. 인덱스 3개(사용자와 생성 시각, 번호와 생성 시각, 만료 시각)
- `phone_verification_daily_counts`: PK `(scope, subject, kst_date)`, `scope`는 `USER` 또는 `PHONE`. `PHONE`의 subject는 번호의 HMAC이라 이 테이블에는 번호가 없다.
- 기존 `users.phone`(#28 실시간 산책 연락처)은 읽지도 쓰지도 않는다. 실시간 산책 확인은 계속 `users.phone`을 보여 준다.

## 6. SMS 인증 정책과 흐름

| 정책 | 값 |
|---|---|
| 인증번호 | `secrets`로 만든 숫자 6자리. 요청 ID와 함께 HMAC-SHA256만 저장. 원문은 DB, 응답, 로그에 없음 |
| 유효 시간 | 300초 |
| 재발송 대기 | 60초, 계정과 번호 각각 |
| 확인 실패 | 요청당 5회. 1~5회는 422, 이후는 올바른 코드도 429 |
| 인증 후 저장 가능 시간 | 15분(`verified_valid_until`) |
| 일일 발송 | 계정별, 번호별 각각 5회, KST 자정 초기화 |
| 최신 요청만 유효 | 재발송하면 이전 요청은 확인 410, 저장 409 `PHONE_VERIFICATION_REQUIRED` |

발송 상태: `PENDING` → `SENDING` → `SENT` | `FAILED` | `UNKNOWN`

| 단계 | 트랜잭션 |
|---|---|
| T1 발송 예약 | `users` 행 `FOR UPDATE` → 잠긴 행에서 상태와 서약 재확인 → 다른 계정의 등록 번호면 409 → 재발송 대기 → 일일 카운터 `INSERT ... ON CONFLICT DO UPDATE ... WHERE send_count < 5 RETURNING` → `PENDING` 저장 → 커밋 |
| T2 발송 점유 | 조건부 UPDATE로 `PENDING` → `SENDING`, 커밋. 같은 요청의 중복 발송 방지 |
| 공급자 호출 | 트랜잭션과 행 잠금 없음 |
| T3 결과 | 접수 `SENT`(200), 실패 확정 `FAILED`(503), 시간 초과와 그 밖의 예외 `UNKNOWN`(503, `verification_id` 미포함). 자동 재발송 없음 |
| 확인 | `UNKNOWN`이면 트랜잭션 없이 공급자에 한 번 조회해 확정하거나 그대로 둔다. 이어서 요청 행 `FOR UPDATE` → 사용 완료, 미발송, 최신 여부, 만료, 실패 횟수 → HMAC 상수 시간 비교 → 실패 횟수 증가 또는 성공 기록 → 커밋 |

- `SENT`는 공급자 접수이고 단말 수신을 뜻하지 않는다.
- 인증 성공 응답이 유실되어 같은 코드로 다시 확인하면 같은 `verified_at`으로 200이고 실패 횟수를 늘리지 않는다.
- 일일 한도는 예약 시점에 세므로 `FAILED`와 `UNKNOWN`도 포함하고 결과 확인 후 차감하지 않는다(미확정 정책).

### SMS 공급자

| 설정 | 동작 |
|---|---|
| `SMS_PROVIDER` 미설정 | 공급자 없음. 발송 503, 아무것도 기록하지 않음 |
| `SMS_PROVIDER=mock` | `APP_ENV`가 local, development, test일 때만 허용. 그 밖의 환경은 503 |
| `SMS_PROVIDER=solapi` | `SOLAPI_API_KEY`, `SOLAPI_API_SECRET`, `SOLAPI_SENDER_NUMBER`가 모두 있어야 하지만, 어댑터가 자리 표시자라 호출 시 `SmsProviderUnavailable`로 503 |
| `PHONE_VERIFICATION_HMAC_KEY` | 32자 이상 필수. 없거나 짧으면 발송과 확인이 500으로 실패하고 저장하지 않음 |

실제 운영 키와 발신번호는 설정하지 않았다. 값은 저장소, 문서, Issue, PR에 남기지 않는다.

### 인증 기록 정리

- `purge_phone_verifications()`: 사용 완료, 인증 후 15분 경과, 미인증 만료 요청과, KST 기준 어제보다 이전 날짜의 일일 카운터를 삭제한다. 하루 1회 이상 실행하면 24시간 이내 정리 기준을 지킨다.
- `scripts/purge_phone_verifications.py`는 기본 dry-run이다. 자동 실행 방식(배치, 스케줄러, 요청 시)은 정하지 않았다.

## 7. 기본 정보 저장 흐름

1. 이름, 생년월일 형식 검증
2. 기존 Step 1 기록이 있으면 재요청 판정: 같은 입력이면 최초 완료 시각으로 200, 다르면 409 `BASIC_INFO_ALREADY_COMPLETED`. 연령 판정보다 먼저 해서 완료 계정의 재요청이 상태를 바꾸지 않는다.
3. 미래 날짜, 만 14세 미만(상태 전환 후 422), 만 120세 초과
4. 트랜잭션: `users` 행 `FOR SHARE`(`lock_user`) → 잠긴 행에서 상태와 서약 재확인 → 인증 요청 행 `FOR UPDATE` → 다른 요청이 먼저 저장했으면 재요청으로 응답 → 인증 성공, 미사용, 15분 이내, 최신 요청 확인 → 다른 계정 번호 확인 → 기본 정보 삽입과 `consumed_at` 기록 → 커밋
5. 실패하면 기본 정보와 인증 사용 처리를 모두 롤백한다. 만 14세 미만 상태 전환은 별도 트랜잭션이라 롤백되지 않는다.

- 같은 번호 동시 저장: `phone_e164` UNIQUE에서 한 계정만 성공, 나머지는 409 `PHONE_ALREADY_REGISTERED`
- 같은 계정 동시 저장: 잠금 뒤 재확인과 PK로 1행, 모두 같은 완료 시각

## 8. 공통 기능 (후속 Issue 재사용)

- 잠금 규칙: 모든 경로에서 `users` 행을 먼저, 사용자 소유 행을 다음에 잠근다. 상태 전환과 발송 예약은 `FOR UPDATE`, 서약과 Step 1 저장은 `FOR SHARE`다. 서로 충돌하므로 상태 전환은 이미 확인을 통과한 저장을 기다리고, 이후 요청은 새 상태를 본다.
- `lock_user()`는 `populate_existing()`으로 인증 시점에 읽은 객체가 아니라 잠금 시점 값을 다시 읽는다. 격리 수준은 기본 READ COMMITTED다.
- `first_incomplete_step()` 판정 순서: `AGE_REVIEW` → `PLEDGE` → `BASIC_INFORMATION` → `PET_EXPERIENCE`. 이 커밋 시점에는 Step 2, 3이 없어 Step 1 완료 계정은 `PET_EXPERIENCE`를 반환한다(#42에서 `WALKING_ROUTINE` 판정 추가).
- `GET /users/me`에 단계 정보를 노출하는 일은 #45 범위다.

## 9. #33 서약 API 연결

- `get_pledge_user`: 역할 다음에 `ensure_active()`(403 `AGE_REVIEW_REQUIRED`)
- `acknowledge_pledge()`: `lock_user()`로 다시 읽은 행에서 상태를 재확인한 뒤 삽입한다. 상태 전환 이후의 서약은 기록 없이 403, 먼저 잠근 서약은 기록이 남는다.
- 상태 전환은 기존 서약 이력을 지우지 않는다.
- #33에서 skip했던 `test_age_review_account_gets_403_and_no_record`를 실제 테스트로 바꿨다.

## 10. 보안과 로그

- 인증번호, 전체 전화번호, 생년월일, HMAC 키, 공급자 키, 토큰을 로그에 남기지 않는다. 로그에는 예외 종류만 남긴다(테스트로 확인).
- 인증 요청이 다른 사용자 소유면 존재 여부를 숨기고 404다.

## 11. 테스트 결과 (PR #52 본문 기록 인용. 이번 문서 작업에서 다시 실행하지 않음)

실행 환경: Windows 11, Python 3.12 `.venv`, SQLite, 격리 PostgreSQL 16.16 테스트 DB(`SIGNUP_TEST_DATABASE_URL`). 시각은 주입한 테스트 시계로 검증했다(실제 대기 없음).

| 실행 | 결과 |
|---|---|
| `tests/` 전체 | 507 passed, 2 failed, 82 skipped |
| 선행 #50 기준 | 463 passed, 2 failed, 83 skipped |
| `tests/test_onboarding_basic_info.py` | 36 passed |
| `tests/test_pledge.py` | 14 passed |
| `tests/test_signup_postgres.py` | 25 passed, 동시성과 경합 9건 3회 반복 통과 |

- 실패 2건은 선행 브랜치에서도 실패하는 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`다. 건너뛴 82건은 각자 빈 DB가 필요한 산책 PostgreSQL 테스트로 실행하지 않았다.
- SQLite: 서약 전 4개 API 409와 무발송, 역할 403, 401, 정상 흐름과 조회, 번호 형식, 300초, 60초, 15분, 실패 5회, 성공 재확인, 타인 요청 404, 일일 5회와 KST 자정, 번호별 한도, 공급자 실패, 시간 초과 `UNKNOWN`과 조회 확정, 연결 오류, 공급자와 HMAC 키 미설정, Mock 운영 차단, 만 14세와 120세 경계, 2월 29일, 미만 계정 상태 전환과 서약 유지, 상태 전환 후 4개 API와 서약 403, 저장 실패 롤백, 정리 함수, 로그에 번호와 인증번호 없음
- PostgreSQL: 024 적용과 재실행, 기존 계정과 `users.phone` 유지, 같은 계정 동시 발송 6건 중 1건 발송, 같은 번호 동시 발송 한도, 두 계정 같은 번호 동시 저장 1건 성공, 같은 계정 동시 저장 1행, 서약 공유 잠금 중 상태 전환 대기, 상태 전환 후 서약 403과 기록 없음
- 기존 016~019 마이그레이션 테스트는 단계별 검증을 마친 뒤 새 코드를 띄우기 전에 023과 024를 적용한다(새 코드가 `users.account_status`를 읽음).
- 미실행: **실제 SOLAPI 발송과 단말 수신 E2E**, 개발 DB 검증, FE 실연동

## 12. FE 연동

- 서약 완료 후 Step 1 화면에 들어온다. 409 `required_step=PLEDGE`이면 서약 화면으로 보낸다.
- 생년월일은 `YYYY.MM.DD` 입력을 `YYYY-MM-DD`로 바꿔 보낸다. 전화번호 입력은 `010` 11자리로 제한한다.
- 고정 인증번호와 클라이언트 인증 상태를 없애고 `verification_id`로 확인과 저장을 한다. 저장 200 이후에만 다음 화면으로 이동한다.
- 503은 자동 재발송 없이 안내하고 `resend_after_seconds` 이후 재요청하게 한다. 확인과 저장 응답이 유실되면 같은 요청을 다시 보내면 된다.
- 422 `AGE_BELOW_MINIMUM` 이후 이동할 연령 확인 화면은 FE에 없다(#41과 함께 FE 후속 작업).

## 13. Notion 설계와 실제 구현의 차이

| 항목 | 구현 | 근거 |
|---|---|---|
| 서약 미완료 409 메시지 | "최초 산책 서약을 먼저 완료해 주세요." | Notion에 `PLEDGE`용 문구 없음 |
| 저장 순서 | 기존 Step 1 재요청 판정을 연령 판정보다 먼저 | 완료 계정의 재요청이 상태를 바꾸지 않게 |
| 대체된 요청 | 확인 410 `VERIFICATION_EXPIRED`, 저장 409 `PHONE_VERIFICATION_REQUIRED` | 최신 요청만 유효 |
| `UNKNOWN` 발송 응답 | 503, `verification_id` 미포함 | 명세의 503 `data`가 `{}` |
| 확인 422의 `data.field` | 실제 오류 필드 | 명세 예시는 `code`만 |
| 저장 요청의 잘못된 UUID | 404 `VERIFICATION_NOT_FOUND` | 명세에 형식 오류 코드 없음 |
| 연령 확인 필요 계정의 Step 1 차단 | 403 `AGE_REVIEW_REQUIRED` | Notion 오류 표에 없음, 미확정 |
| Asia/Seoul 계산 | 고정 +09:00 | 일광절약시간 없음 |

## 14. 미확정 정책

- [ ] 연령 확인 필요 계정의 Step 1 API 차단 코드(`AGE_REVIEW_REQUIRED` 또는 `ONBOARDING_NOT_ALLOWED`)
- [ ] `next_step`과 `next_onboarding_step` 통일(#45)
- [ ] `FAILED`, `UNKNOWN`의 일일 한도 포함과 결과 확인 후 차감
- [ ] 인증 기록 정리 자동 실행 방식
- [ ] `users.phone`과 인증 번호의 관계, 실시간 산책 확인이 보여 줄 번호
- [ ] SOLAPI 계정, 발신번호, 키, 결과 조회 API(`messageId`, `groupId`) 검증과 실제 수신 E2E
- [ ] 개인정보 처리방침의 실명, 생년월일, 연락처, SMS 위탁, 보유기간 고지(#31과 함께)
- [ ] 탈퇴 계정 번호의 재사용 시점

## 15. 개발 DB와 배포 대기

- 024는 023 서약 다음에 적용한다. 실제 개발 DB 적용 여부는 확인하지 못했다. 적용은 승인 후 진행한다.
- 이 브랜치 이후 코드는 모든 사용자 조회에서 `users.account_status`를 읽는다. 024 적용 전에는 로그인도 실패하므로 019 첫 산책 가이드를 포함해 manifest의 정식 순서에 따라 024까지 적용한 뒤 새 코드를 띄운다.
- 운영 설정: `PHONE_VERIFICATION_HMAC_KEY`, `SMS_PROVIDER`, `APP_ENV`, SOLAPI 키 3종. 값은 문서에 남기지 않는다.
- 기본 정보 SQL을 024로 배정했다. 기존 017, 018 예외와 020 보호소 평가 파일은 보존했다.

## 16. 후속 Issue와 의존 관계

- 선행: #32(계약 401), #33(현재 버전 서약 조회)
- 후속: #41(정정과 가입 종료, `get_current_user` 상태 검사, 쓰기 재확인), #42(Step 2, `PET_EXPERIENCE` 다음 판정), Step 3 산책 루틴, #45(`GET /users/me` 진행 상태)
- 관련: #28, PR #36(`users.phone`)

## 17. 관련 링크

- Issue #40, PR #52
- Notion: [Step 1 기본 정보 조회](https://app.notion.com/p/41fb7bbac8c78103a3d9d9fbbb6e8d89), [Step 1 전화번호 인증번호 발송](https://app.notion.com/p/41fb7bbac8c781508fb9c4e60a6fb23d), [Step 1 전화번호 인증번호 확인](https://app.notion.com/p/41fb7bbac8c7819f8470e0e0b61125c6), [Step 1 기본 정보 저장](https://app.notion.com/p/41fb7bbac8c781b09438fb76cc270114), [온보딩 전체 설계](https://app.notion.com/p/41fb7bbac8c7809b914bcf3e80d9d876)의 "#40 기본 정보 입력과 SMS 인증 구현 결과" 절
