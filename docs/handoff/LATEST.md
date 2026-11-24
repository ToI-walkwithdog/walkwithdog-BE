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
