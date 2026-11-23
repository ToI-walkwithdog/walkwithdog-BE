# 작업 인수인계 — F-12 산책 이력 (SC-09, SC-16)

## 1. 작업 목표
- 사용자(SC-09)와 보호소(SC-16)가 각자의 종료된 산책(`COMPLETED`, `INTERRUPTED`, `AUTO_TIMEOUT`) 이력을 시작 시각 최신순(`started_at DESC`) 목록과 상세(경로 지도, 행동마커, 평가, 사용자 신고 내역)로 조회할 수 있도록 백엔드 API를 구현한다.
- 저장소는 기존 FastAPI + PostgreSQL(SQLAlchemy) 구조를 그대로 유지한다(Firestore 미사용, 신규 DB 마이그레이션 없음).

## 2. 변경 파일 목록
- `app/walk_map_schemas.py`: `WalkEndReason`, `ShelterWalkDog`, `ShelterWalkWalker`, `ShelterWalkHistoryItem`, `ShelterWalkHistoryResponse`, `ShelterWalkHistoryDetailResponse` 스키마 추가 및 `UserWalkItem.end_reason` 보강
- `app/walk_map_service.py`: `user_walk_history`, `shelter_walk_history`, `shelter_walk_history_detail` 서비스 로직 구현
- `app/routers/walks.py`: `GET /walks/history` 엔드포인트 추가 (사용자 상세는 기존 `GET /walks/{walk_id}`, `GET /walks/{walk_id}/summary` 연동)
- `app/routers/shelter_live_walks.py`: `GET /shelters/me/walks`, `GET /shelters/me/walks/{walk_id}` 엔드포인트 추가 (보호소 사용자 신고 탭은 기존 `GET /shelters/me/incidents` 연동)
- `tests/test_f12_walk_history.py`: F-12 완료 기준 검증 수용 테스트 4건 추가
- `docs/TODO.md`, `docs/handoff/LATEST.md`: F-12 작업 현황 및 인수인계 기록

## 3. 현재 상태
- 테스트 실행 명령 및 결과:
  - `$env:PYTHONPATH="."; pytest tests/test_f12_walk_history.py tests/test_walk_domain.py -v` → **전체 통과 (26 passed, 79 subtests passed)**
- 완료 기준 대조:
  | 완료 기준 | 테스트 항목 | 결과 |
  |---|---|---|
  | 이력 없음 예외 처리 (`200 OK`, 빈 목록) | `test_empty_history_returns_empty_list_for_user_and_shelter` | 통과 |
  | `COMPLETED`·`INTERRUPTED`·`AUTO_TIMEOUT` 포함 및 `started_at DESC` 정렬 (`IN_PROGRESS` 제외) | `test_history_includes_completed_interrupted_auto_timeout_and_orders_by_started_at_desc` | 통과 |
  | 상세 조회(경로 지도, 행동마커, 평가) 및 보호소 '사용자 신고' 탭 연동 | `test_walk_history_detail_shows_route_markers_review_and_shelter_incidents` | 통과 |
  | 사용자/보호소 역할 분리 및 무단 접근 차단 (`403`) | `test_role_isolation_blocks_unauthorized_history_access` | 통과 |
- 확인한 것:
  - SQLite 기반 API 통합 테스트에서 사용자 이력 목록(`GET /walks/history`), 사용자 상세·요약(`GET /walks/{walk_id}`, `GET /walks/{walk_id}/summary`), 보호소 이력 목록·상세(`GET /shelters/me/walks`, `GET /shelters/me/walks/{walk_id}`), 보호소 신고 목록(`GET /shelters/me/incidents`)의 응답 구조와 권한 검증 정상 동작
  - 도메인 계약 테스트(`tests/test_walk_domain.py`) 회귀 없음
- 확인하지 못한 것:
  - 로컬 PostgreSQL 실서버 기동 상태에서 Swagger(`/docs`)를 통한 수동 엔드포인트 호출 검증
  - Android 앱(SC-09 `RecyclerView` 이력 화면 및 SC-16 보호소 산책 이력·사용자 신고 탭) 실제 UI 연동 및 `"아직 산책 기록이 없어요"` 빈 상태 문구 렌더링

## 4. 주의사항 및 규칙
- 비밀값은 `.env`로 분리하고 `.gitignore`에 포함 상태를 유지한다(API 키, 서비스 계정 키, 개인정보 커밋 금지).
- `main`, `dev` 브랜치에 직접 커밋/푸시하지 않고 `feature/f-12-walk-history` 브랜치에서 `dev` 대상 PR로 병합한다.

## 5. 다음 할 일
- [x] `f12.patch` 적용 및 `$env:PYTHONPATH="."; pytest tests/test_f12_walk_history.py tests/test_walk_domain.py -v` 실행 완료
- [ ] `feature/f-12-walk-history` 브랜치 커밋·push 후 `dev` 대상 PR 생성
- [ ] (앱) SC-09 산책 이력 목록(`GET /walks/history`)·상세(`GET /walks/{walk_id}/summary`) 및 SC-16 보호소 이력(`GET /shelters/me/walks`, `GET /shelters/me/incidents`) 연동
