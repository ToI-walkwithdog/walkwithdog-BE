# 작업 인수인계

## 배포 환경변수 구성 (#128, 16-3)

- 로컬 uvicorn [`.env.example`](../../.env.example), 로컬 Compose [`deploy/.env.example`](../../deploy/.env.example), EC2 API [`deploy/.env.api.example`](../../deploy/.env.api.example)를 구분한다. 템플릿은 비밀값을 비워 두며 그대로 실행하지 않는다. 실제 값은 승인 후 입력한다.
- EC2의 `.env`는 DB 초기 구성, `.env.api`는 API 접속 및 기능 설정으로 분리한다. HMAC 키는 환경별, 용도별로 분리하며 SOLAPI를 연동하지 않는다. #116~#118 담당자가 승인된 기능 설정을 전달한다. `ACCOUNT_EXIT_RECORD_POLICY`는 미설정으로 둔다.
- `scripts/check_runtime_env.py`는 템플릿과 실제 배포 설정을 구분해 읽기 전용으로 검사한다. 비밀값은 출력하지 않는다. 일반 인증 테스트는 고정 OTP 환경변수와 분리하고 루트 `.env`에는 고정 OTP 값을 두지 않는다. 테스트, 리뷰, CI 결과는 #128 PR을 참조한다.
- 실제 EC2 환경 파일 작성, DB 변경, AWS 설정 및 배포는 별도 승인 대상이다. 최초 시연 서버 배포 전 설정 검증, Mock SMS, 접근 제한 및 프록시 뒤 요청 제한을 확인한다. [EC2 설치](../deployment/EC2_SETUP.md), [실행 절차](../deployment/RUNBOOK.md)

## Mock SMS 테스트 환경 (#124)

- 시연과 테스트 서버는 SOLAPI 없이 Mock SMS를 쓴다. `SMS_MOCK_FIXED_CODE`(ASCII 숫자 6자리)가 있으면 모든 요청의 인증번호가 그 값이 된다. `SMS_PROVIDER=mock`이고 `APP_ENV`가 `local`, `development`, `test`일 때만 적용되며, 그 밖의 조합에 값이 있으면 두 발송 API가 503 `SMS_DELIVERY_UNAVAILABLE`이고 아무것도 기록하지 않는다. 빈 값이나 미설정이면 기존 무작위 인증번호다. 값은 저장소, Issue, PR, 로그에 남기지 않는다.
- 고정 인증번호는 번호 소유를 증명하지 않으므로 내부 합성 테스트 번호 `010-0000-0000`~`010-0000-9999`만 받는다. 대역 밖 번호는 슬롯과 DB 기록 전에 422 `INVALID_PHONE_NUMBER`이고, 설정값이 있는 동안 대역 밖 번호의 증빙(모드 전에 만든 것 포함)으로는 저장할 수 없다(409 `PHONE_VERIFICATION_REQUIRED`).
- 서버 실행용 Mock은 발송 내용을 보관하지 않는다. #110의 비동기 발송, `SUPPRESSED`, 한도, 만료, 실패 횟수, 최신 요청, 목적 분리 정책은 그대로다.
- 담당별 번호 대역, 환경 전환 영향, FE 테스트 방법, 16-3 환경변수, 최초 시연 서버 배포 전 검증은 [#110 HANDOFF 8절](../issues/110/HANDOFF.md#8-sms-정책-변경mock-sms)에 있다.

## 인증번호 발송의 등록 여부 비노출 (#110)

- 대상은 `POST /users/me/phone-verifications`(온보딩)와 `POST /users/me/profile/phone-verifications`(프로필 번호 변경)입니다. 다른 계정에 등록된 번호인지 발송과 확인 응답으로 구분할 수 없습니다.
- 발송 200 `{verification_id, expires_in_seconds, resend_after_seconds}`는 **서버의 발송 요청 접수**이며 실제 문자 발송이나 수신을 뜻하지 않습니다. 503 `SMS_DELIVERY_UNAVAILABLE`은 공급자 미설정이나 발송 대기열 포화일 때만이고, 아무것도 기록하거나 차감하지 않습니다. 발송 409 `PHONE_ALREADY_REGISTERED`는 없어졌습니다.
- 처리 순서는 발송 슬롯 확보, 계정 상태와 서약(또는 Step 1) 확인, 일일 카운터 차감, 재발송 간격 확인, 등록 여부 확인입니다. 번호 카운터 행의 잠금이 서로 다른 계정의 같은 번호 요청을 직렬화합니다. 거절(재발송 간격, 일일 한도, DB 오류)은 차감까지 전부 롤백합니다.
- 다른 계정에 등록된 번호의 요청은 `SUPPRESSED`로 저장합니다. 인증번호는 만들어 HMAC만 저장하고 버리며, 공급자를 호출하지 않습니다. 어떤 인증번호로도 인증되지 않고 저장에도 쓰이지 않습니다. 한도와 최신 요청 규칙은 실제 발송과 같습니다.
- 실제 발송은 프로세스 내부 Thread Pool(worker 2개, 대기 20개)에서 독립 세션으로 합니다. 발송 직전에 `users` FOR SHARE, 요청 FOR UPDATE 순서로 잠그고, 존재, `PENDING`, 만료 전, 최신 요청, ACTIVE 계정일 때만 `SENDING`으로 바꿉니다. 아니면 `FAILED`이고 공급자를 호출하지 않습니다. 공급자 호출 중에는 트랜잭션이 없습니다. 결과는 `SENDING`일 때만 `SENT`, `FAILED`, `UNKNOWN`으로 기록하며, 그사이 인증된 요청은 항상 `SENT`가 되고 인증 정보는 바뀌지 않습니다. 자동 재발송은 없습니다.
- 확인은 DB만 사용합니다(공급자 lookup 없음). 틀린 인증번호는 발송 상태와 무관하게 422 `VERIFICATION_CODE_INVALID`이고 실패 횟수에 반영됩니다. 맞는 인증번호는 `SENDING`, `SENT`, `UNKNOWN`에서만 인증됩니다. 확인 409 `VERIFICATION_NOT_SENT`는 없어졌습니다. Step 1 저장과 프로필 PATCH도 같은 상태 조건으로 증빙을 다시 확인하고, 소유 증명 후의 409 `PHONE_ALREADY_REGISTERED`는 유지합니다.
- 전제와 남는 위험: 발송 슬롯은 프로세스별이며 배포는 API worker 1개입니다. 정상 종료 때는 대기 중인 발송 작업을 모두 실행한 뒤 끝납니다. 강제 종료(예: `docker stop` 유예 시간 초과 후 SIGKILL)되면 요청이 `PENDING`이나 `SENDING`으로 남고, 사용자가 60초 뒤 재요청합니다. 남은 행은 만료 후 정리 작업이 삭제합니다. 다음은 `docs/TODO.md`의 16-10 전 검토 항목입니다: 슬롯 반납 시점 차이를 통한 과부하 관찰, 발송 작업의 짧은 `users` 공유 잠금으로 생기는 타이밍 차이, 번호별 일일 한도 소진.
- 마이그레이션 `037_phone_verification_suppressed.sql`(상태 CHECK 교체, 기존 행 유지, 재실행 가능, 선행 024)이 필요합니다. 개발 DB 적용은 16-6단계에서 별도 승인 후 진행합니다.
- 배포 순서: 037을 먼저 적용한 뒤 이 코드를 배포합니다. 037 없이 배포하면 등록된 번호의 요청만 CHECK 위반으로 500이 되어, 그 자체가 등록 여부 노출입니다. 되돌릴 때는 `SUPPRESSED` 행을 먼저 삭제해야 이전 CHECK를 다시 걸 수 있습니다.

## 산책 선호 설정 (#105)

- `GET /users/me/walk-preferences`, `PATCH /users/me/walk-preferences`를 추가했습니다. ACTIVE 일반 사용자만 사용할 수 있고, 서약과 온보딩 완료는 요구하지 않습니다. 보호소와 관리자는 403 `PREFERENCE_NOT_ALLOWED`, 나이 확인 대기 계정은 403 `AGE_REVIEW_REQUIRED`입니다. 응답은 `Cache-Control: private, no-store`입니다.
- GET은 `configured`, `version`, `preferences`(저장값 `duration_code`, `size_code`, `updated_at`), `effective_preferences`, `sources`를 반환합니다. 적용값은 필드별로 저장값, 완료된 Step 3 값, `null` 순이며 출처는 `PREFERENCE`, `ONBOARDING`, `NONE`입니다. 미설정이면 `configured=false`, `version=0`, `preferences=null`이고 조회는 아무것도 쓰지 않습니다.
- PATCH는 `expected_version`(엄격한 정수)과 `duration_code`, `size_code` 중 하나 이상을 받습니다. 보내지 않은 필드는 그대로 두고, `null`과 추가 필드(`region_code` 포함)는 422 `INVALID_PREFERENCE_PAYLOAD`, 알 수 없는 코드는 422 `INVALID_DURATION`, `INVALID_DOG_SIZE`입니다. 첫 저장은 `expected_version=0`에서 `version=1`이 됩니다. 값이 같으면 200(`changed_fields: []`, 버전과 수정 시각 유지)이고, 버전이 다르면 값이 같아도 409 `PREFERENCE_VERSION_MISMATCH`입니다.
- 저장은 `users` 행을 FOR UPDATE로 잠근 한 트랜잭션입니다. DB 잠금 시간 초과, 교착, 직렬화 실패를 포함한 DB 오류는 전체 롤백 후 500 `INTERNAL_SERVER_ERROR`입니다(별도 잠금 충돌 코드 없음).
- 온보딩 Step 3 원본, 온보딩 상태, `profile_version`, 예약 자격, 산책 가능 목록 API는 바꾸지 않습니다. 지역은 다루지 않습니다.
- 나이 확인 대기 계정의 가입 종료는 `volunteer_walk_preferences`를 `users`보다 먼저 삭제합니다. 종료가 거절되면 선호도 보존됩니다.
- 마이그레이션 `036_walk_preferences.sql`(새 테이블만 추가, 재실행 가능)이 필요합니다. 실제 의존성은 기본 `users(id)`뿐이라 manifest 선행 파일은 비어 있습니다(`002_auth_sessions.sql`과 같은 방식). 개발 DB 적용은 16단계에서 별도 승인 후 진행합니다.

## 내 정보 수정과 휴대폰 재인증 (#103)

- 대상은 ACTIVE 일반 사용자입니다. 서약과 온보딩 완료는 요구하지 않습니다. 보호소와 관리자는 403 `PROFILE_UPDATE_NOT_ALLOWED`, 나이 확인 대기 계정은 403 `AGE_REVIEW_REQUIRED`입니다. 모든 응답은 `Cache-Control: private, no-store`입니다.
- `GET /users/me/profile`은 `nickname`, `full_name`, `phone_number`(본인 번호, `010-XXXX-XXXX`), `phone_verified_at`, `profile_version`을 반환합니다. Step 1 전이면 이름과 번호는 `null`입니다. `/users/{user_id}/profile`보다 먼저 등록되어 경로가 겹치지 않습니다.
- `PATCH /users/me/profile`은 `expected_profile_version`이 필수입니다. 보내지 않은 필드는 그대로 두고 빈값과 `null`은 422입니다. 닉네임, 이름, 번호를 한 트랜잭션에 저장하고 `profile_version`을 1 올리며 `changed_fields`를 반환합니다. 값이 같으면 저장 없이 `changed_fields: []`입니다. 버전이 다르면 409 `PROFILE_VERSION_MISMATCH`입니다.
- 버전 확인이 값 비교보다 먼저입니다. 버전이 다르면 값이 같아도 409 `PROFILE_VERSION_MISMATCH`입니다. PATCH로 이름이나 번호를 바꾼 뒤 이전 Step 1 요청을 다시 보내면 409 `BASIC_INFO_ALREADY_COMPLETED`입니다.
- 이름과 번호 변경은 기존 Step 1 기록이 있어야 합니다(없으면 409 `PROFILE_BASIC_INFO_REQUIRED`). Step 1 첫 저장도 버전을 1 올리며 같은 요청 재시도는 올리지 않습니다.
- 번호 변경은 `POST /users/me/profile/phone-verifications`와 `/confirm`으로 인증한 뒤 PATCH에 `phone_number`와 `phone_verification_id`를 함께 보냅니다. 확인만으로는 번호가 바뀌지 않습니다. 증명이 없거나 다른 계정, 다른 목적, 다른 번호의 증명이면 409 `PHONE_VERIFICATION_REQUIRED`입니다. 증명은 본인, 같은 번호, 프로필 목적, 미사용, 확인 후 15분 이내, 최신 요청이어야 하고 저장에 성공할 때만 사용 처리됩니다. `phone_verified_at`은 인증 시각입니다.
- 인증 요청은 `phone_verifications.purpose`(`ONBOARDING`, `PROFILE_PHONE_CHANGE`)로 구분하며 다른 목적의 확인과 저장에 쓸 수 없습니다. 재발송 간격과 일일 한도는 두 목적을 합산합니다. 기존 SMS 발송 서비스와 개발용 Mock을 그대로 사용합니다.
- 감사 기록은 별도 테이블 없이 `user_id`, 변경 필드명, 시각만 로그에 남깁니다(`audit_event`, `audit_user_id`, `audit_fields`, `audit_at` 필드 포함). 번호, 이름, 인증번호는 남기지 않습니다.
- 마이그레이션 `035_profile_phone_change.sql`(추가 전용, 재실행 가능)이 필요합니다. manifest 선행 파일은 실제 의존성인 `024_onboarding_basic_info.sql`입니다. 적용 순서는 파일 번호대로 034 다음입니다. 개발 DB 적용은 별도 승인 후 진행합니다. FE 인증번호 입력 UI는 17단계 연동 TODO입니다.

## 본인 산책 이력 및 누적 통계 (#99)

- `GET /walks/history`는 예약에 연결된 보호소의 현재 이름을 각 항목의 `shelter_name`으로 제공합니다. 관계가 없으면 `null`을 반환합니다. 기존 정렬, 페이지네이션과 인계 미확인 필터를 유지합니다.
- `GET /volunteers/me`는 본인의 `COMPLETED`와 `INTERRUPTED` 산책을 대상으로 `history_walk_count`, `history_distance_m`, `history_unique_dog_count`, `history_distance_missing_count`, `history_distance_is_complete`를 제공합니다. 무필터 이력 `total`과 같은 범위를 사용합니다. 기존 봉사 통계와 레벨, 승급 실적의 원본은 유지합니다.
- `history_distance_m`은 미터 단위 Decimal 문자열입니다. 이력이 없으면 `"0.00"`, 누락 0건, 완전성 `true`입니다. 실제 0m는 누락에 포함되지 않으며, 누락이 있으면 알려진 거리의 부분합과 함께 완전성 `false`를 반환합니다.
- 신규 마이그레이션은 없습니다. 기존 상세와 요약을 사용하며 개인 메모는 본인 전용 요약에서 조회합니다.
- 신규 API 계약 테스트를 SQLite와 격리 PostgreSQL에서 각각 실행했습니다. 기존 이력 회귀를 포함해 테스트 12건과 서브테스트 38건이 통과했습니다.
- FE 이력 카드에 보호소명을 표시하고, 이력과 마이페이지에서 누적 통계와 거리 누락 여부를 연동합니다. 조회 최적화는 `docs/TODO.md`에 기록했습니다.

---

# 작업 인수인계 — F-15 보호소의 산책자 평가 (SC-16, SC-09, SC-10)

## 1. 작업 목표
- 보호소가 정상 완료(`COMPLETED`)된 산책에 대해 봉사자(산책자)에게 별점(`0.5` 단위 `0.5~5.0`), 태그 4종 복수 선택(`PUNCTUALITY`, `SAFETY_CARE`, `CONSIDERATE`, `DISAPPOINTING`), 코멘트(선택, 최대 500자)로 1회 평가를 남기고, 해당 사용자가 산책 이력(SC-09)과 마이페이지(SC-10)에서 자신이 받은 평가를 확인할 수 있는 백엔드 API를 구현한다.
- 저장소는 기존 FastAPI + PostgreSQL(SQLAlchemy) 구조를 그대로 유지한다(Firestore 미사용).

## 2. 변경 파일 목록
- `app/walk_models.py`, `app/models.py`: `ShelterVolunteerReview`, `ShelterVolunteerReviewTag` 모델 및 `Walk.volunteer_review` 관계 추가
- `app/shelter_volunteer_review_schemas.py`: 평가 생성 요청(`0.5` 단위 별점, 중복 태그 방지, 최대 500자 코멘트), 평가 응답, `review_summary` 응답 스키마 신규 작성
- `app/shelter_volunteer_review_service.py`: `COMPLETED` 상태·보호소 권한 검증, 산책 1건당 1회 작성 강제(수정·삭제 불가), 평균 별점 및 태그별 누적 횟수 집계 로직 신규 작성
- `app/routers/shelter_volunteer_reviews.py`: `POST /walks/{walk_id}/volunteer-review`, `GET /walks/{walk_id}/volunteer-review`, `GET /volunteers/me/reviews` 엔드포인트 신규 작성
- `app/walk_map_schemas.py`, `app/walk_map_service.py`: SC-09 산책 이력(`GET /walks/history`) 및 SC-16 보호소 산책 이력 항목에 `shelter_review` 필드 추가
- `app/walk_request_schemas.py`, `app/routers/walk_requests.py`: SC-10 마이페이지(`GET /volunteers/me`) 응답에 `review_summary` 추가
- `app/main.py`: `shelter_volunteer_reviews.router` 등록
- `migrations/020_shelter_volunteer_reviews.sql`: 기존 PostgreSQL DB용 `shelter_volunteer_reviews`, `shelter_volunteer_review_tags` 테이블 생성 SQL 신규 작성
- `tests/test_f15_shelter_volunteer_reviews.py`: F-15 완료 기준 검증 수용 테스트 4건 신규 작성
- `docs/TODO.md`, `docs/handoff/LATEST.md`: F-15 작업 내역 및 테스트 결과 기록

## 3. 현재 상태
- 테스트 실행 명령 및 결과:
  - `$env:PYTHONPATH="."; pytest tests/test_f15_shelter_volunteer_reviews.py tests/test_walk_requests.py -v` → **전체 통과 (36 passed)**
- 완료 기준 대조:
  | 완료 기준 | 테스트 항목 | 결과 |
  |---|---|---|
  | 보호소가 `COMPLETED` 상태인 본인 보호소 산책 건에 대해 별점(0.5 단위 0.5~5.0), 태그, 코멘트로 산책자 평가를 1회 작성 | `test_shelter_review_appears_in_user_history_and_mypage` | 통과 |
  | 동일 산책 건 중복 평가 시 `409 Conflict` 반환 및 작성 후 수정·삭제 제한(`405`) | `test_duplicate_review_rejected_and_editing_disallowed` | 통과 |
  | 예외 처리: `IN_PROGRESS` / `INTERRUPTED` 등 미완료·중단 산책 평가 시 `409 Conflict` 반환 | `test_non_completed_walks_cannot_be_reviewed` | 통과 |
  | 예외 처리: 타 보호소 소속 산책 평가(`403`), 봉사자 본인 평가(`403`), 비0.5 단위 별점·중복 태그·500자 초과(`422`) 차단 | `test_authorization_and_input_validation` | 통과 |
  | 보호소가 작성한 평가가 해당 사용자의 산책 이력(SC-09 `GET /walks/history`)과 마이페이지(SC-10 `GET /volunteers/me`, `GET /volunteers/me/reviews`)에 표시 | `test_shelter_review_appears_in_user_history_and_mypage` | 통과 |
- 확인된 것:
  - SQLite 기반 API 통합 테스트에서 평가 생성(`201`), 중복 작성(`409`) 및 수정·삭제(`405`) 차단, 미완료·중단 산책 차단(`409`), 권한(`403`) 및 입력 검증(`422`), SC-09 산책 이력 `shelter_review` 포함, SC-10 마이페이지 `review_summary` 및 전체 평가 목록 조회(`GET /volunteers/me/reviews`) 동작 및 기존 산책 신청 회귀 테스트(`tests/test_walk_requests.py`) 통과 확인
- 확인하지 못한 것:
  - 운영/개발 PostgreSQL 실서버에 `migrations/020_shelter_volunteer_reviews.sql` 수동 적용 후 Swagger(`/docs`)를 통한 실DB 엔드포인트 호출 검증
  - Android 앱(SC-16 보호소 산책 이력 평가 작성 모달 및 SC-09/SC-10 마이페이지 받은 평가 요약·목록) 실제 UI 연동

## 4. 주의사항 및 규칙
- 비밀값은 `.env`로 분리하고 `.gitignore` 포함 상태를 유지한다(API 키, 서비스 계정 키, 개인정보 커밋 금지).
- 기존 PostgreSQL 인스턴스에는 `migrations/020_shelter_volunteer_reviews.sql`을 적용해야 `shelter_volunteer_reviews`, `shelter_volunteer_review_tags` 테이블이 반영된다.
- `main`, `dev` 브랜치에 직접 커밋/푸시하지 않고 `feature/f-15-shelter-volunteer-review` 브랜치에서 `dev` 대상 PR로 병합한다.

## 5. 다음 할 일
- [x] `app/walk_models.py`, `app/models.py`, `app/shelter_volunteer_review_schemas.py`, `app/shelter_volunteer_review_service.py`, `app/routers/shelter_volunteer_reviews.py`, `app/walk_map_schemas.py`, `app/walk_map_service.py`, `app/walk_request_schemas.py`, `app/routers/walk_requests.py`, `app/main.py`, `migrations/020_shelter_volunteer_reviews.sql` 구현 및 `tests/test_f15_shelter_volunteer_reviews.py` 작성·통과 (`36 passed`)
- [ ] `feature/f-15-shelter-volunteer-review` 브랜치 커밋·push 후 `dev` 대상 PR 생성
- [ ] (앱) SC-16 보호소 산책 이력 평가 작성 모달 및 SC-09/SC-10 마이페이지 받은 평가 요약·목록 UI 연동
# 날짜별 산책 일정 및 예약 인수인계 (#29)

- `POST /dogs/`의 기존 JSON 계약은 유지한다. `POST /dogs/walk-registration`은 `image`와 JSON 배열 문자열 `slots`를 포함한 multipart 요청으로 강아지, 산책 설정, 날짜 일정을 저장한다. 기존 강아지는 `GET/POST /dogs/{dog_id}/date-slots` 및 개별 `PATCH/DELETE`를 사용한다. 슬롯의 일자와 시각은 KST 현지 값이며 `duration_minutes`는 30, 60, 90 중 하나다. 실패 시 생성된 사진만 정리하며, 정리 실패는 해당 파일명과 함께 로그로 남긴다.
- 보호소 지역은 `POST /shelters/` 또는 `PATCH /shelters/{shelter_id}/region`으로 설정한다. 기존 산책 설정 PATCH에서 강아지 크기, 숫자 난이도, 명시적인 최대 시간을 추가한다. 값이 없는 기존 데이터는 null로 읽는다. `/dogs/walk-availability`는 활성 지역 1~3개, 선택 크기와 강아지 단위 페이지를 적용한다. `/dogs/{dog_id}/walk-availability`는 동일 가용성 규칙으로 전체 슬롯을 반환한다.
- 날짜별 `POST /walk-requests`는 `date_slot_id`, 오프셋이 포함된 기존 시작·종료 필드, UUID `Idempotency-Key`를 받는다. 동일 계정과 키 및 정규화 본문에는 최초 201을 복구한다. 신규 신청은 현재 온보딩 완료가 필요하다. 슬롯 시각 변경, 실제 점유, 일시적 DB 경합은 각각 `WALK_SLOT_CHANGED`, `WALK_SLOT_UNAVAILABLE`, `WALK_REQUEST_RETRYABLE`로 안내한다. 반복 요일 요청은 기존 입력과 응답 형식을 사용한다.
- 점유 범위는 슬롯 전체의 반개구간 `[시작, 종료)`다. PENDING/APPROVED는 점유하고 REJECTED/CANCELLED는 점유를 해제한다. 승인 대상 자체는 충돌에서 제외하며 새 승인에는 미래 시작 시각이 필요하다. 승인된 본인이 저장된 예약 시간 안에서만 시작한다. 날짜 예약 산책의 예정 종료는 슬롯에 저장된 소요시간을 사용하며 최대 산책 시간이 설정되면 그 범위로 제한한다. 활성 예약이 참조한 슬롯의 변경 및 삭제는 409다. 종료된 예약의 기존 참조는 유지하며 슬롯 교체는 새 행을 만든다.
- SQL은 009, 011, 022 이후 028을 1회 적용한다. 010이 수동 적용됐으면 기존 `date_slot_id`는 유지하고 연쇄 삭제 참조를 이력 보존 참조로 교체한다. 사전에 잘못된 강아지·슬롯 연결과 중복 활성 참조를 확인한다. 위반 데이터가 있으면 028 트랜잭션 전체가 롤백된다. 실제 개발 DB 적용은 별도 승인 후 진행한다.

## 예약 목록, 상세 및 취소 (#84)

- `GET /walk-requests/my`에서 `status=APPROVED`는 완료·중단된 산책과 연결된 예약까지 포함한다. 기존 `tab=APPROVED`는 이전 동작을 유지한다. `status_group=PAST`는 거절과 취소를 포함한다. 세 필터 중 둘 이상을 보내면 `422 RESERVATION_FILTER_CONFLICT`다. 최신 예약순으로 정렬하며 조건을 먼저 적용한 뒤 페이지를 조회한다.
- `GET /walk-requests/{request_id}`에서 모든 상태의 예약, 슬롯과 예약 시간, 거절·취소 이력, 보호소 기본 정보, 기존 최대 산책 시간 설정을 조회한다. 보호소 주소와 연락처는 종료 전 승인 예약의 본인에게만 제공한다. `actions`는 봉사자 화면용이다. 코드 입력 화면으로 이동할 수 있어도 실제 시작은 기존 서버 검증을 거친다.
- `PATCH /walk-requests/{request_id}`의 `CANCEL`은 선택 사유 `SCHEDULE_CHANGED`, `CANNOT_VISIT`, 생략 또는 `null`을 받는다. 시작 전이며 Walk 기록이 없는 대기·승인 건만 취소할 수 있다. 재전송의 `409 REQUEST_ALREADY_CANCELLED` 또는 응답 유실 후 같은 ID의 상세로 상태를 복구한다. 취소와 함께 기록한 `cancelled_at`은 UTC이고 당시 역할은 `USER`, `SHELTER`, `ADMIN` 중 하나다.
- 홈에서 `GET /walks/active`가 돌려준 `walk_request_id`로 진행 중 산책을 복구한다. 활성 산책이 없음을 뜻하는 404를 받으면 `GET /walk-requests/my?startable_only=true&limit=1`로 서버 시각이 시작 이상, 종료 미만인 승인 예약 중 Walk 기록이 없는 첫 후보를 조회한다. 카드와 시작 요청은 같은 예약 ID를 사용한다.
- `029_reservation_cancellation.sql`은 028 이후 적용한다. 이전 취소의 빈 메타데이터와 승인·거절 이력, 날짜 슬롯 참조를 보존한다. 개발 DB 적용과 실제 FE 연동은 별도 승인으로 진행한다.

## 보호소 코드 인증과 산책 시작 (#87)

- `POST /walk-requests/{request_id}/shelter-code`: 해당 예약 보호소 담당자와 관리자만 발급한다. 승인 예약이고 서버 시각이 시작 이상 종료 미만이며 Walk 기록이 없을 때 201 `SHELTER_CODE_ISSUED`와 `data.request_id`, `shelter_code`(앞자리 0 포함 6자리 문자열), `expires_at`(UTC, 발급 10분 뒤와 예약 종료 중 빠른 시각)을 돌려준다. 재발급하면 이전 코드와 그 검증이 무효가 된다. 원문 코드는 이 응답에만 있고 DB에는 HMAC만 저장한다.
- `POST /walk-requests/{request_id}/shelter-code/verify`: 예약 본인이 `{"shelter_code": "012345"}`를 보낸다. 성공은 200 `SHELTER_CODE_VERIFIED`, `data.verified_until`. 같은 유효 코드 재제출은 같은 결과다. 불일치, 미발급, 만료는 422 `SHELTER_CODE_INVALID`이고 실패 횟수를 커밋한 뒤 응답한다. 연속 5회째는 429 `SHELTER_CODE_RATE_LIMITED`와 `data.retry_after_seconds`, `Retry-After`이며 5분 동안 재발급과 무관하게 막힌다. 형식 오류(422 `INVALID_SHELTER_CODE_FORMAT`), 권한(403), 예약 자격(409 `WALK_REQUEST_NOT_ELIGIBLE`)은 횟수에 넣지 않는다. 일시적 잠금 충돌은 409 `WALK_REQUEST_RETRYABLE`이다.
- `POST /walks`: 요청과 성공 응답은 그대로다. 현재 발급의 만료되지 않은 미사용 검증이 있어야 하고, Walk 생성과 검증 사용을 한 트랜잭션에 저장한다. 409는 `detail.code`로 나눈다: `WALK_START_CODE_REQUIRED`(코드 다시 확인), `WALK_START_BOOKING_NOT_ELIGIBLE`(예약 상세 갱신), `WALK_START_ALREADY_STARTED`, `WALK_START_RETRYABLE`(활성 산책 먼저 조회). 온보딩 게이트(#81)는 그대로다.
- 시작 응답이 유실되면 `GET /walks/active`의 `walk_request_id`가 같은 예약일 때만 이어 간다(#84). 예약 상세에는 코드나 검증 데이터가 없다.
- `030_shelter_code_verification.sql`은 029 다음에 적용하며 추가만 한다. 재실행해도 바뀌지 않는다. 배포 환경에 `SHELTER_CODE_HMAC_KEY`(32자 이상 비밀 값)가 필요하다. 개발 DB 적용과 FE 연동은 별도 승인으로 진행한다.

## GPS, 행동 마커, 안전 신고, 산책 종료 (#89)

- 무수신 자동 종료 정산은 `GET /walks/active`, `POST /walks`, 좌표와 마커 저장 전에 별도 트랜잭션으로 먼저 커밋된다. 그래서 이후 404나 409가 나도 `COMPLETED/AUTO_TIMEOUT`이 남는다. 자동 종료 기준은 그대로 마지막 신규 좌표 서버 수신(없으면 시작) 뒤 1시간 초과이며, 예약 종료나 최대 산책 시간은 `is_overdue` 안내만 한다.
- `GET /walks/active`에 `last_point_received_at`(서버 수신 시각)과 `auto_end_at`을 추가했다. `auto_end_at`을 지나면(초과 시점부터) 자동 종료된다. 정확히 그 시각에는 아직 진행 중이다.
- `PATCH /walks/{walk_id}/end`: 이미 무수신 만료된 산책은 사용자 종료나 직원 안전 중단 대신 200과 `COMPLETED/AUTO_TIMEOUT`(종료 시각은 마지막 활동 시각)을 돌려준다. 이미 종료된 산책은 기존 409다.
- `POST /walks/{walk_id}/points`: 5m/s를 넘는 급이동 관측도 원본 순번으로 저장하고, 거리와 이동 통계에서만 뺀다. 순번, 시각, 중복 규칙은 그대로다.
- `POST /walks/{walk_id}/markers`: 선택 필드 `client_marker_id`(UUID)와 `position_recorded_at`. 같은 키와 같은 본문은 200과 기존 마커(종료나 상한 뒤에도), 다른 본문은 409다. 좌표는 정확도 50m 이하이고 관측이 선택 시각 이전 30초 이내면 `CURRENT`(`position_recorded_at`이 없는 기존 호출은 정확도가 없거나 50m 이하이면 `CURRENT`), 아니면 같은 산책의 2분 이내 저장 좌표를 `LAST_KNOWN`으로 쓴다. 둘 다 없으면 409 `detail.code=MARKER_LOCATION_UNAVAILABLE`이다. 키 없는 기존 호출도 동작한다.
- `POST /walks/{walk_id}/incidents`: `OTHER`는 trim 후 1~1,000자가 필수이고 모든 설명은 1,000자 이하다(422). 좌표를 생략하면 사건 시각 이전 30초 이내의 저장 좌표만 쓰고, 없으면 `null`로 접수한다. GPS가 없어도 신고는 막지 않는다. 좌표를 생략한 같은 `client_report_id` 재시도는 이후 GPS가 바뀌어도 처음 접수 결과를 200으로 돌려준다.
- `031_walk_marker_idempotency.sql`은 030 다음에 적용하며 추가만 한다. 재실행해도 바뀌지 않는다. 개발 DB 적용과 FE 연동은 별도 승인으로 진행한다.

## 산책 종료 후 보호소 인계 (#93)

- 종료 결과를 받은 뒤 같은 `walk_id`로 `POST /walks/{walk_id}/handover`를 호출한다. 본인만 접수할 수 있다. `handover_result`와 JSON 불리언 체크 세 개(`dog_returned`, `equipment_returned`, `condition_shared`)가 필수다. `COMPLETE`는 모두 참이고 설명이 비어야 하며, `ISSUE`는 하나 이상 거짓이고 공백 제거 후 1~500자의 `issue_description`이 필요하다.
- `COMPLETED/USER`는 두 결과 모두 허용한다. `COMPLETED/AUTO_TIMEOUT`의 정상 접수에는 `acknowledge_auto_timeout=true`가 필요하며 누락 또는 거짓은 422다. `INTERRUPTED`는 문제만 접수한다. 진행 중, 취소, 도입 전 산책과 다른 내용으로 하는 재제출은 409다.
- 최초 접수는 201, 정규화한 동일 요청은 200과 기존 응답을 돌려준다. 정상은 `SELF_REPORTED`와 `self_confirmed_at`, 문제는 `ISSUE_REPORTED`와 `issue_reported_at`을 제공한다. 두 상태는 봉사자의 진술이며 보호소 직원 수락을 뜻하지 않는다.
- 응답을 받지 못하면 동일 산책 상세에서 `handover_status`와 `handover`를 확인한다. `NOT_CONFIRMED`면 인계 대상 미접수이고 `NOT_REQUIRED`면 도입 전 산책이다. 산책 ID를 잃었으면 `GET /walks/history?handover_status=NOT_CONFIRMED`에서 본인 후보를 조회한다. 본인 요약에는 인계 상태를 추가했다.
- 산책 종료와 확정 통계는 먼저 커밋한다. 새 정상 사용자 종료 산책의 자가 확인 접수 시 승급 평가 대기 시각은 인계 행과 함께 저장한다. 승급 평가와 레벨 변경은 후속 단계다.
- `032_walk_handovers.sql`은 031 다음에 적용하며 기존 산책의 인계 대상값은 `false`다. 기능 적용 후 시작한 산책부터 대상값을 `true`로 저장한다. 개발 DB 적용과 FE 연동은 별도 작업이다.

## 산책 개인 메모 (#95)

- `PUT /walks/{walk_id}/private-memo`는 완료 또는 중단된 본인 산책에 메모 한 건을 저장한다. 인계 상태가 `SELF_REPORTED`, `ISSUE_REPORTED`, 도입 전 `NOT_REQUIRED`인 산책에서 이용할 수 있다. 미접수 `NOT_CONFIRMED`는 409다.
- `memo`는 양끝 공백 제거 후 1~1,000자이고 `expected_version`은 0 이상의 정수다. 빈값은 422이며 저장된 메모를 보존한다. 최초 저장은 요청 버전 0에 201과 버전 1을 반환한다. 현재 내용과 같으면 200과 기존 버전 및 시각을 반환한다. 내용이 다르면 현재 서버 버전과 일치할 때만 수정해 버전을 1 올린다. 버전 충돌은 409다.
- 본인 전용 `GET /walks/{walk_id}/summary`는 `private_memo`로 내용, 버전, 생성 및 수정 시각을 반환한다. 메모가 없으면 null이다. 저장 응답이 유실되면 같은 산책의 요약에서 현재 값과 버전을 확인한다. 보호소, 관리자, 다른 봉사자와 상세, 이력, 평가, 공개 지도, 신고 응답에는 메모를 노출하지 않는다.
- 메모를 별도 트랜잭션으로 저장한다. 메모 저장 오류가 발생해도 종료, 인계, 확정 통계와 승급 평가 대기 기록을 보존한다. `033_walk_private_memos.sql`은 032 다음에 적용하며 기존 산책을 보존한다. 개발 DB 적용과 FE 연동은 후속 작업이다.

## 정상 인계 산책 실적과 자동 레벨업 (#97)

- UTC 정책 시작 시각과 같거나 이후에 시작한 `COMPLETED/USER` 산책 중 본인이 정상 인계를 자가 확인한 건만 인정한다. 누적 인정 횟수 0/3/10/20/40회로 Lv.1~5를 적용하며 저장 레벨이 더 높으면 보존한다. 기존 봉사 `total_*`와 예약 조건은 유지한다.
- FastAPI 시작 직후부터 30초 간격으로 인계의 대기 기록을 확인한다. 평가마다 별도 트랜잭션에서 사용자 행을 잠그고 산책당 인정 기록을 한 건 저장한다. 실제로 승급할 때 레벨과 이벤트를 같은 트랜잭션에 반영한다. 평가 실패는 60초 이후 재처리할 수 있으며, 서버 재시작 후에도 대기와 실패 기록을 이어서 처리한다. 잠금 경합은 다음 주기에 재시도한다.
- 본인 산책 요약에 `level_evaluation_status`와 nullable `level_up_event`를 제공한다. 먼저 `PENDING` 또는 `FAILED`인지 확인하고, `COMPLETED`에서 이벤트가 null이면 평가 완료 후 미승급이다. 미인계와 인계 문제로 보류된 산책은 기존 `handover_status`에서 구분한다. 현재 레벨은 기존 `GET /volunteers/me.level`을 사용한다.
- `034_volunteer_level_up.sql`은 033 다음에 적용한다. `VOLUNTEER_LEVEL_POLICY_VERSION`과 `VOLUNTEER_LEVEL_POLICY_START_AT`을 함께 설정해야 평가가 활성화된다. 시작 시각은 UTC ISO 8601 형식이며, 두 설정이 모두 비어 있으면 백그라운드 평가를 실행하지 않는다. 개발 DB 적용, 실제 활성화와 FE 연동은 별도 작업이다.
