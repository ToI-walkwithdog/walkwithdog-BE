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

## F-15 보호소의 산책자 평가 (SC-16, SC-09, SC-10)
- [x] PostgreSQL 단독 테이블(`shelter_volunteer_reviews`, `shelter_volunteer_review_tags`) 모델 및 `migrations/020_shelter_volunteer_reviews.sql` 작성
- [x] 보호소 담당자의 산책자 평가 1회 작성 및 단건 조회 API 구현 (`POST /walks/{walk_id}/volunteer-review`, `GET /walks/{walk_id}/volunteer-review`)
- [x] `Walk.status == "COMPLETED"` 상태 검증, 0.5 단위 별점(`0.5~5.0`), 태그 4종 복수 선택(`PUNCTUALITY`, `SAFETY_CARE`, `CONSIDERATE`, `DISAPPOINTING`), 최대 500자 선택 코멘트, 중복 작성(`409`) 및 수정·삭제(`405`) 차단 검증
- [x] SC-09 산책 이력(`GET /walks/history`) 및 SC-16 보호소 산책 이력 응답에 `shelter_review` 필드 추가
- [x] SC-10 마이페이지(`GET /volunteers/me`) `review_summary`(평균 별점, 총 평가 수, 태그별 횟수) 추가 및 받은 평가 전체 목록(`GET /volunteers/me/reviews`) API 구현
- [x] 인수 테스트 작성 및 통과 (`tests/test_f15_shelter_volunteer_reviews.py`, `tests/test_walk_requests.py` — 36 passed)
- [x] `docs/handoff/LATEST.md` 테스트 결과 및 인수인계 내용 반영
- [ ] `feature/f-15-shelter-volunteer-review` 커밋·push 후 PR 생성 (`feature/f-15-shelter-volunteer-review` → `dev`)
- [ ] (앱) SC-16 보호소 산책 이력 평가 작성 모달 및 SC-09/SC-10 마이페이지 받은 평가 요약·목록 UI 연동
# 날짜별 산책 일정 및 예약

- [ ] 개발 DB의 실제 적용 이력을 확인하고 승인 후 `028_date_slot_booking.sql`을 적용한다. 확인된 보호소 지역 코드만 입력하고, 크기·숫자 난이도·최대 산책 시간은 보호소가 등록한다.
- [ ] FE 연동에서 동일한 `Idempotency-Key`와 날짜 슬롯의 원래 시각을 보존해 응답 유실, 일정 변경과 점유 충돌을 검증한다.
- [ ] 장기적인 잠금 순서 정리와 조회 최적화는 별도로 진행한다.

## 예약 관리 (#84)

- [ ] 승인 후 개발 DB에 `028_date_slot_booking.sql` 적용을 확인하고 `029_reservation_cancellation.sql`을 적용한다.
- [ ] FE 예약 내역과 홈에서 확정 필터, 거절·취소 상세, 선택 취소 사유, 같은 예약 ID로 취소 결과 복구 및 시작 후보 조회를 연동한다.
- [ ] 보호소 코드 인증과 산책 시작을 연동한다. 전체 예약 응답 형식 통일 및 추가 조회 최적화는 후속 작업으로 다룬다.

## 보호소 코드 인증과 산책 시작 (#87)

- [ ] 승인 후 개발 DB에 `029_reservation_cancellation.sql` 다음으로 `030_shelter_code_verification.sql`을 적용하고, 배포 환경에 `SHELTER_CODE_HMAC_KEY`를 설정한다.
- [ ] 보호소 웹 예약 화면에 코드 발급과 재발급을 연동하고, 앱 `/4-1`~`/4-4`에서 검증, `WALK_START_*` 분기, 시작 응답 유실 복구를 연동한다.
- [x] 시작 실패가 같은 요청 안의 자동 종료 정산(`close_stale_walks`)을 롤백하지 않도록 커밋 경계를 나눈다(#89에서 해결).
- [ ] 발급, 검증, 시작, 취소의 공통 잠금 그래프를 정리하고, 코드 시도 제한의 운영값과 오래된 발급과 시도 기록의 정리 주기를 정한다.
- [ ] 자동 개발 게이트: 저장된 Codex 승인을 확인할 때 현재 `CODEX_THREAD_ID`도 대조하도록 Claude Code 승인 확인(`CLAUDE_CODE_SESSION_ID`)과 맞춘다(#84 후속). 게이트 변경은 별도 유지보수 PR로 진행한다.

## GPS, 행동 마커, 안전 신고, 산책 종료 (#89)

- [ ] 승인 후 개발 DB에 `030_shelter_code_verification.sql` 다음으로 `031_walk_marker_idempotency.sql`을 적용한다.
- [ ] FE `/5-1`~`/5-7`, `/5-5-other` 연동: 마커 `client_marker_id`와 `position_recorded_at` 전송, 409 `MARKER_LOCATION_UNAVAILABLE` 안내, 기타 신고 본문 1~1,000자, 활성 응답 `auto_end_at` 기반 장시간 일시정지 안내.
- [ ] 위치 기준(마커 30초와 2분, 신고 30초, 정확도 50m)의 운영값을 현장 검증 후 정한다.
- [ ] 신고 좌표의 출처와 관측 시각 필드, 신고 최초 요청 스냅샷 해시로 200과 409 판정 강화.
- [ ] 마커 키 충돌과 종료 상태 409의 `detail.code` 등 산책 API 오류 형식 통일, 공통 잠금 그래프 정리.
- [ ] 급이동 원본 좌표가 `walk.last_*`(보호소 실시간 화면)와 마커 `LAST_KNOWN`, 신고 대체 위치 후보가 될 수 있다. 정확도 외에 이전 점 대비 속도도 보는 후보 필터를 검토한다.
- [ ] 신고 대체 위치의 정확도 기준이 마커 설정(`WALK_MARKER_MAX_ACCURACY_M`, 기본 50)을 함께 쓴다. 운영값을 정할 때 분리 여부를 정한다.

## 산책 종료 후 보호소 인계 (#93)

- [ ] 별도 승인 후 개발 DB에서 031 적용을 확인하고 `032_walk_handovers.sql`을 적용한다.
- [ ] FE에서 산책 종료 확정 후 같은 산책 ID로 인계 API를 연동한다. 자동 종료 확인, 문제 접수 안내, 동일 요청 재전송, 본인 이력의 미접수 필터를 연결한다.
- [ ] 다음 단계에서 본인 전용 산책 개인 메모를 저장한다. 이후 자동 레벨업에서 인계 행의 평가 대기 시각을 사용한다.
- [ ] 보호소 직원 수락과 문제 해결, 인계 정정·취소·보존 정책은 후속으로 정한다.
