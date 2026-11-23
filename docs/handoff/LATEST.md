# 작업 인수인계 — F-13 예약 내역 (마이페이지, SC-10)

## 1. 작업 목표
- 사용자(SC-10 마이페이지)가 본인의 산책 예약 내역과 보호소 승인 여부를 5개 탭(`ALL`, `PENDING`, `APPROVED`, `REJECTED`, `COMPLETED`)으로 조회하고, 실제 산책 시작 전(`Walk` 레코드 생성 전) 대기·승인 상태의 예약을 취소할 수 있도록 기존 산책 신청 API를 확장한다.
- 저장소는 기존 FastAPI + PostgreSQL(SQLAlchemy) 구조를 그대로 유지한다(Firestore 미사용, 신규 DB 마이그레이션 없음).

## 2. 변경 파일 목록
- `app/walk_request_schemas.py`: `ReservationTab`, `ReservationDisplayStatus` 타입 및 `WalkRequestResponse`에 `shelter_name`, `walk_id`, `walk_status`, `display_status`, `can_cancel` 필드 추가
- `app/walk_request_service.py`: `request_list_query`에 5개 탭(`ALL`/`PENDING`/`APPROVED`/`REJECTED`/`COMPLETED`) 필터링 및 `dog`·`shelter`·`walk` 조인 로드 추가, `ensure_not_started`에 `IN_PROGRESS`·`COMPLETED`·`INTERRUPTED` 산책 시작 여부 검증 반영, `get_request_detail`에 보호소명 및 연관 산책 상태 포함
- `app/routers/walk_requests.py`: `GET /walk-requests/my`에 `tab` 쿼리 파라미터 확장 (기존 `status` 필터 계약 유지)
- `tests/test_f13_walk_reservations.py`: F-13 완료 기준 검증 수용 테스트 4건 신규 추가
- `docs/TODO.md`, `docs/handoff/LATEST.md`: F-13 작업 현황 및 인수인계 기록

## 3. 현재 상태
- 테스트 실행 명령 및 결과:
  - `$env:PYTHONPATH="."; pytest tests/test_f13_walk_reservations.py tests/test_walk_requests.py -v` → **전체 통과 (36 passed)**
- 완료 기준 대조:
  | 완료 기준 | 테스트 항목 | 결과 |
  |---|---|---|
  | 5개 탭(`ALL`/`PENDING`/`APPROVED`/`REJECTED`/`COMPLETED`) 분류 및 보호소 승인·거절 반영 | `test_mypage_tabs_reflect_shelter_approval_rejection_and_walk_completion` | 통과 |
  | 산책 시작 전 대기(`PENDING`)·승인(`APPROVED`) 예약 취소(`CANCELLED`) | `test_user_can_cancel_pending_and_approved_requests_before_walk_starts` | 통과 |
  | 예외 처리: 실제 산책 시작 이후(`IN_PROGRESS`·`COMPLETED`·`INTERRUPTED`) 취소 차단(`409`) | `test_cannot_cancel_once_walk_has_started_or_finished` | 통과 |
  | 권한 분리: 타인의 예약 취소 요청 차단(`403`) | `test_other_user_cannot_cancel_reservation` | 통과 |
- 확인한 것:
  - SQLite 기반 API 통합 테스트에서 `GET /walk-requests/my`의 5개 탭 필터링, 보호소 승인·거절 상태 반영, `display_status`·`can_cancel` 계산, `PATCH /walk-requests/{request_id}` 취소 및 산책 시작 이후 `409` 차단, 기존 산책 신청 계약 테스트(`tests/test_walk_requests.py`) 회귀 없음
- 확인하지 못한 것:
  - 로컬 PostgreSQL 실서버 기동 상태에서 Swagger(`/docs`)를 통한 수동 엔드포인트 호출 검증
  - Android 앱(SC-10 마이페이지 `TabLayout` + `RecyclerView`, 승인 상태 뱃지 및 취소 버튼) 실제 UI 연동

## 4. 주의사항 및 규칙
- 비밀값은 `.env`로 분리하고 `.gitignore`에 포함 상태를 유지한다(API 키, 서비스 계정 키, 개인정보 커밋 금지).
- `main`, `dev` 브랜치에 직접 커밋/푸시하지 않고 `feature/f-13-mypage-reservations` 브랜치에서 `dev` 대상 PR로 병합한다.

## 5. 다음 할 일
- [x] `app/walk_request_schemas.py`, `app/walk_request_service.py`, `app/routers/walk_requests.py` 확장 및 `tests/test_f13_walk_reservations.py` 작성·통과 (`36 passed`)
- [ ] `feature/f-13-mypage-reservations` 브랜치 커밋·push 후 `dev` 대상 PR 생성
- [ ] (앱) SC-10 마이페이지 예약 내역(`GET /walk-requests/my?tab=...`) 및 취소(`PATCH /walk-requests/{request_id}`) UI 연동
