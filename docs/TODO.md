# F-11 산책 시간 초과 경고 (SC-15) TODO

> F-03~F-09 이력은 `docs/team/TODO.md`를, F-10 이력은 이전 커밋을 본다. 이 파일은 F-11 브랜치(`feature/f-11-walk-overtime-alert`) 작업만 다룬다.

- [x] DB 마이그레이션 (`migrations/018_f11_walk_overtime.sql`): `walks` 테이블 `planned_duration_minutes`, `expected_end_at` 컬럼 및 제약조건 추가
- [x] `Walk` 모델 컬럼·제약조건 및 `is_overdue`, `overdue_minutes` 프로퍼티 반영 (`app/walk_models.py`)
- [x] 산책 시작(`start_walk`) 시 `DogWalkSetting.recommended_duration_minutes` 기반 `expected_end_at` 계산(미설정 시 예약 소요시간 폴백) (`app/walk_service.py`)
- [x] 조회 시점 초과 판정(`compute_walk_overdue`) 및 `WALK_OVERDUE_GRACE_MINUTES` 유예 시간 지원, 산책 종료 시 자동 해제 (`app/walk_service.py`)
- [x] 사용자 화면 응답(`WalkResponse`, `ActiveWalkResponse`) 및 보호소 실시간 응답(`LiveWalkCardResponse`, `LiveWalkDetailResponse`, `LiveWalkUpdatesResponse`)에 `expected_end_at`, `is_overdue`, `overdue_minutes` 반영 (`app/walk_record_schemas.py`, `app/walk_live_schemas.py`, `app/walk_live_service.py`)
- [x] `.env.example`에 `WALK_OVERDUE_GRACE_MINUTES=0` 추가
- [x] 완료 기준 검증 테스트 작성 (`tests/test_f11_walk_overtime.py` 6건)
- [x] `tests/test_walk_domain.py` 007 DDL 프로젝션 및 계약 스냅샷 보정 (018 추가분 반영)
- [x] (사용자) 로컬 `pytest` 실행 및 결과 확인
- [ ] (사용자) 로컬 PostgreSQL에 018 적용 (`handoff` 6번 참고)
- [ ] (사용자) 커밋·push 후 PR 생성 (feature → dev)
- [ ] (앱) 보호소 실시간 카드(SC-15) 빨간 테두리·'시간 초과' 뱃지 및 사용자 산책 화면 종료 안내 UI 연동

## F-12 산책 이력 (SC-09, SC-16)
- [x] 사용자 산책 이력 목록 API (`GET /walks/history`) 및 상세 연동 (`GET /walks/{walk_id}`, `GET /walks/{walk_id}/summary`)
- [x] 보호소 산책 이력 목록·상세 API (`GET /shelters/me/walks`, `GET /shelters/me/walks/{walk_id}`) 및 사용자 신고 탭 연동 (`GET /shelters/me/incidents`)
- [x] 인수 테스트 작성 및 통과 (`tests/test_f12_walk_history.py`, `tests/test_walk_domain.py` — 26 passed)
- [x] `docs/handoff/LATEST.md` 테스트 결과 및 인수인계 내용 반영
- [ ] `feature/f-12-walk-history` 커밋·push 후 PR 생성 (`feature/f-12-walk-history` → `dev`)
- [ ] (앱) SC-09 / SC-16 Android RecyclerView 목록·상세 UI 연동

## F-13 예약 내역 (마이페이지, SC-10)
- [x] 마이페이지 예약 내역 5개 탭(`ALL`/`PENDING`/`APPROVED`/`REJECTED`/`COMPLETED`) 조회 확장 (`GET /walk-requests/my`)
- [x] 응답 항목 보호소명(`shelter_name`), 화면 표시 상태(`display_status`), 취소 가능 여부(`can_cancel`) 및 산책 시작 이후 취소 차단(`409`) 검증
- [x] 인수 테스트 작성 및 통과 (`tests/test_f13_walk_reservations.py`, `tests/test_walk_requests.py` — 36 passed)
- [x] `docs/handoff/LATEST.md` 테스트 결과 및 인수인계 내용 반영
- [ ] `feature/f-13-mypage-reservations` 커밋·push 후 PR 생성 (`feature/f-13-mypage-reservations` → `dev`)
- [ ] (앱) SC-10 마이페이지 `TabLayout` + `RecyclerView` 및 예약 취소 UI 연동

## F-14 첫 산책 가이드 (SC-03 사용자 홈, SC-04 첫 산책 가이드)
- [x] `User.first_walk_guide_completed_at` 컬럼 및 `migrations/019_first_walk_guide.sql` 작성
- [x] 첫 산책 가이드 4단계 핵심 수칙 슬라이드 및 열람·권유 상태 조회 API 구현 (`GET /walk-guides/first-walk`, `GET /walk-guides/first-walk/status`)
- [x] 마지막 장 ‘확인했어요’ 열람 완료 처리 API 구현 (`POST /walk-guides/first-walk/complete`, 멱등 지원)
- [x] 반려견 무경험자(`Survey.dog_experience`) 첫 예약 전 권유 조건 판별(`should_recommend_guide`) 및 예약 비차단 검증
- [x] 인수 테스트 작성 및 통과 (`tests/test_f14_first_walk_guide.py`, `tests/test_walk_requests.py` — 36 passed)
- [x] `docs/handoff/LATEST.md` 테스트 결과 및 인수인계 내용 반영
- [ ] `feature/f-14-first-walk-guide` 커밋·push 후 PR 생성 (`feature/f-14-first-walk-guide` → `dev`)
- [ ] (앱) SC-03 홈 카드·권유 안내 및 SC-04 `ViewPager` 슬라이드 UI 연동
