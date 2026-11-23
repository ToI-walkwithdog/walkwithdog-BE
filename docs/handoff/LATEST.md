# 작업 인수인계 — F-14 첫 산책 가이드 (SC-03 사용자 홈, SC-04 첫 산책 가이드)

## 1. 작업 목표
- 처음 산책 봉사를 진행하는 사용자도 안전하게 산책할 수 있도록 사용자 홈(SC-03) 하단에서 핵심 안전 수칙 4단계 카드 슬라이드(SC-04)를 제공하고, 마지막 장 ‘확인했어요’ 열람 완료 상태 및 반려견 무경험자 대상 첫 예약 전 열람 권유 여부를 제공하는 백엔드 API를 구현한다.
- 저장소는 기존 FastAPI + PostgreSQL(SQLAlchemy) 구조를 그대로 유지한다(Firestore 미사용).

## 2. 변경 파일 목록
- `app/models.py`: `User` 모델에 `first_walk_guide_completed_at` (`TIMESTAMPTZ NULL`) 컬럼 추가
- `app/main.py`: `walk_guides.router` 등록
- `app/walk_guide_schemas.py`: 4단계 가이드 카드 슬라이드(`GuideSlideItem`), 가이드 열람·권유 상태(`FirstWalkGuideStatusResponse`), 전체 가이드 응답(`FirstWalkGuideResponse`) 스키마 신규 작성
- `app/walk_guide_service.py`: 4개 핵심 안전 수칙(강아지 다루는 법, 금지 행동, 배변 처리, 신고 버튼 위치) 정적 데이터, ‘확인했어요’ 멱등 완료 처리(`complete_first_walk_guide`), 반려견 무경험자 첫 예약 전 열람 권유 판별(`get_first_walk_guide_status`) 로직 신규 작성
- `app/routers/walk_guides.py`: `GET /walk-guides/first-walk`, `GET /walk-guides/first-walk/status`, `POST /walk-guides/first-walk/complete` 엔드포인트 신규 작성
- `migrations/019_first_walk_guide.sql`: 기존 PostgreSQL DB용 `users.first_walk_guide_completed_at` 컬럼 추가 마이그레이션 SQL 신규 작성
- `tests/test_f14_first_walk_guide.py`: F-14 완료 기준 검증 수용 테스트 4건 신규 작성
- `docs/TODO.md`, `docs/handoff/LATEST.md`: F-14 작업 내역 및 테스트 결과 기록

## 3. 현재 상태
- 테스트 실행 명령 및 결과:
  - `$env:PYTHONPATH="."; pytest tests/test_f14_first_walk_guide.py tests/test_walk_requests.py -v` → **전체 통과 (36 passed)**
- 완료 기준 대조:
  | 완료 기준 | 테스트 항목 | 결과 |
  |---|---|---|
  | 홈 하단에서 첫 산책 가이드 4개 핵심 수칙(강아지 다루는 법·금지 행동·배변 처리·신고 버튼 위치) 슬라이드 조회 | `test_get_first_walk_guide_returns_four_safety_slides` | 통과 |
  | 마지막 장에서 ‘확인했어요’ 호출 시 열람 완료 상태 저장 및 중복 호출 멱등 유지 | `test_complete_first_walk_guide_marks_completed_and_is_idempotent` | 통과 |
  | 예외 처리: 반려견 경험 없음 + 가이드 미열람 + 첫 예약 전 사용자에게 권유 플래그(`should_recommend_guide: true`) 제공 및 완료 후 해제 | `test_recommends_guide_for_inexperienced_user_before_first_reservation` | 통과 |
  | 예외 처리: 산책 신청(`POST /walk-requests`)은 `409`로 차단하지 않고 첫 예약 생성 후 권유 플래그 해제 | `test_recommendation_clears_after_first_reservation_without_blocking_request` | 통과 |
- 확인한 것:
  - SQLite 기반 API 통합 테스트에서 4개 카드 슬라이드 구조, ‘확인했어요’ 완료 시각 저장 및 멱등성, 설문(`Survey.dog_experience`) 무경험자 첫 예약 전 `should_recommend_guide` 판별, 산책 신청 비차단(`201`) 및 기존 산책 신청 테스트(`tests/test_walk_requests.py`) 회귀 없음
- 확인하지 못한 것:
  - 운영/개발 PostgreSQL 실서버에 `migrations/019_first_walk_guide.sql` 수동 적용 후 Swagger(`/docs`)를 통한 실DB 엔드포인트 호출 검증
  - Android 앱(SC-03 사용자 홈 하단 카드·권유 안내 및 SC-04 `ViewPager` 카드 슬라이드·‘확인했어요’ 버튼) 실제 UI 연동

## 4. 주의사항 및 규칙
- 비밀값은 `.env`로 분리하고 `.gitignore` 포함 상태를 유지한다(API 키, 서비스 계정 키, 개인정보 커밋 금지).
- 기존 PostgreSQL 인스턴스에는 `migrations/019_first_walk_guide.sql`을 적용해야 `users.first_walk_guide_completed_at` 컬럼이 반영된다.
- `main`, `dev` 브랜치에 직접 커밋/푸시하지 않고 `feature/f-14-first-walk-guide` 브랜치에서 `dev` 대상 PR로 병합한다.

## 5. 다음 할 일
- [x] `app/models.py`, `app/main.py`, `app/walk_guide_schemas.py`, `app/walk_guide_service.py`, `app/routers/walk_guides.py`, `migrations/019_first_walk_guide.sql` 구현 및 `tests/test_f14_first_walk_guide.py` 작성·통과 (`36 passed`)
- [ ] `feature/f-14-first-walk-guide` 브랜치 커밋·push 후 `dev` 대상 PR 생성
- [ ] (앱) SC-03 사용자 홈 하단 카드/권유 안내 및 SC-04 `ViewPager` 슬라이드·‘확인했어요’ API 연동
