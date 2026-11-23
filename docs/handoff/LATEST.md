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
