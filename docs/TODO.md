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
- [x] 본인 전용 산책 개인 메모를 저장한다(#95).
- [x] 자동 레벨업에서 인계 행의 평가 대기 시각을 사용한다(#97).
- [ ] 보호소 직원 수락과 문제 해결, 인계 정정·취소·보존 정책은 후속으로 정한다.

## 산책 개인 메모 (#95)

- [ ] 별도 승인 후 개발 DB에서 032 적용을 확인하고 `033_walk_private_memos.sql`을 적용한다.
- [ ] FE 개인 메모 입력을 본인 전용 요약과 연결한다. 기존 버전을 사용해 저장하고, 409 또는 응답 유실 시 같은 산책의 서버 메모를 다시 조회한다.

## 전체 서비스 검증

- [ ] #93 마무리 시 기준 `dev`와 초기 구현에서 재현된 `tests/test_walk_recording_postgres.py`, `tests/test_walk_maps_postgres.py`의 보조 PostgreSQL 경합 실패 13건을 다시 확인한다.
- [ ] `tests/test_walk_postgres.py`, `tests/test_gps_motion_postgres.py`의 007 및 008 보조 테스트에서 기존 `walk_reviews` 초기화 오류를 확인한다.

## 개발 DB 및 외부 서비스 구성

- [ ] #95의 개인 메모 본문이 운영 PostgreSQL 오류 및 SQL 문장 로그나 외부 로그 수집에 남지 않는지 합성 메모로 검증한다.
- [ ] 별도 승인 후 033 적용을 확인하고 `034_volunteer_level_up.sql`을 적용한다. 승인된 정책 버전과 실제 적용 시작 시각(UTC)을 설정한 뒤 처리 상태를 확인한다.
- [ ] 별도 승인 후 034 적용을 확인하고 `035_profile_phone_change.sql`을 적용한다. 기존 사용자는 `profile_version` 0, 기존 인증 요청은 `ONBOARDING`이 된다.
- [x] 실제 SMS 발송사 설정: SOLAPI를 연동하지 않고 Mock SMS를 쓰기로 확정했다(#124). 온보딩과 프로필 번호 변경 인증이 같은 정책을 쓴다.

## 자동 레벨업 (#97)

- [ ] FE가 본인 산책 요약의 실제 승급 이벤트와 평가 상태를 사용하도록 연동한다. 평가 결과 대기와 실패는 완료 후 미승급과 구분한다.

## 본인 산책 이력 및 누적 통계 (#99)

- [ ] FE 산책 이력에서 예약 기준 보호소명을 표시하고, 산책 이력과 마이페이지에서 본인 누적 이력 통계를 사용한다. 거리 누락 시 확인된 거리의 부분합임을 표시한다.
- [ ] 운영 조회량과 실행 계획을 확인한 뒤 집계 인덱스와 커서 페이지네이션이 필요한지 검토한다.
- [ ] `FINISHED_WALK_STATUSES` 상수가 두 모듈에 정의되어 있다. 한 곳으로 모으고 이력과 통계 범위가 같은지 테스트로 유지한다.

## 프로필 수정과 휴대폰 재인증 (#103)

- [ ] FE에 인증번호 입력 UI가 없다. 17단계 연동에서 프로필 번호 변경의 발송, 인증번호 입력과 확인, `PATCH /users/me/profile` 저장 흐름을 추가한다. 확인만으로는 번호가 바뀌지 않는다.
- [ ] FE 내 정보 수정은 조회한 `profile_version`을 `expected_profile_version`으로 보내고, 409 `PROFILE_VERSION_MISMATCH`이면 다시 조회해 사용자에게 확인받는다.
- [ ] `users.phone`은 인증되지 않은 이전 필드로 읽거나 쓰지 않는다. 사용처를 확인한 뒤 정리 여부를 별도 Issue로 결정한다.
- [x] 16단계 공개 전 필수(보안, 개인정보): 온보딩과 프로필 인증 발송이 재발송 간격과 일일 한도보다 먼저 409 `PHONE_ALREADY_REGISTERED`를 반환해 번호 등록 여부를 횟수 제한 없이 확인할 수 있다(온보딩 발송의 기존 동작). 별도 보안 Issue로 응답 순서나 시도 제한을 정하고, 16단계에서 실제 개발 서버와 외부 서비스를 공개하기 전에 해결한다. #110에서 해결: 발송 응답은 등록 여부와 무관하고, 등록된 번호의 요청은 `SUPPRESSED`로 한도에 포함된다.
- [ ] 개발 DB에 035를 적용할 때 `phone_verifications` 크기를 확인한다. 크면 CHECK를 `NOT VALID` 추가 후 `VALIDATE`로 나눈다(16단계).
- [ ] 20단계: `app/account_status.py`의 잠금 설명(Step 1 저장의 FOR SHARE)을 FOR UPDATE로 고친다.
- [ ] 20단계: 같은 사용자의 Step 1 저장, 프로필 PATCH, 산책 선호 PATCH(#105)와 겹칠 때 NOWAIT 잠금(보호소 코드 검증, 예약)이 즉시 실패할 수 있는지 18단계 검증에서 확인하고 필요하면 재시도 안내를 정한다.

## 산책 선호 설정 (#105)

- [ ] 17단계: FE `/10-3`을 `GET/PATCH /users/me/walk-preferences`에 연결한다. 조회한 `version`을 `expected_version`으로 보내고, 409 `PREFERENCE_VERSION_MISMATCH`이면 다시 조회한다. 저장 응답을 받은 뒤에만 화면을 닫는다.
- [ ] 17단계: `/7-2` 초기 크기 필터는 목록에서 직접 고른 값, `effective_preferences.size_code`, 전체 순서로 정한다. 목록 API는 요청받은 `size_code`만 적용한다. 초기 지역은 온보딩 Step 3의 지역 1~3개를 쓴다.
- [ ] 16단계: 별도 승인 후 035 적용을 확인하고 `036_walk_preferences.sql`을 적용한다(새 테이블만 추가).
- [ ] FE에 지역 선택 UI가 생기면 선호 지역 저장과 초기 검색 우선 적용을 별도 Issue로 정한다(Notion 조건부 정책).
- [ ] `docs/dev-automation/FEATURE_PLAN.md`의 20단계 로드맵 반영은 게이트 보호 경로라 #105에서 분리했다. 사람이 검토하고 병합하는 별도 유지보수 PR로 진행한다.

## 16-4 통합 DB 마이그레이션 도구 (#113)

- [x] `scripts/apply_migrations.py` 신규: 정적 검증 + 증거 기반 계획 계산 + dry-run 출력 (1-B, check_migrations import, 보호 경로 미변경)
- [x] `tests/test_apply_migrations.py` 신규: 상태별 계획 계산, 체크섬 불일치 중단, requires 누락 중단 (1-B)
- [x] `--apply` 경로 구현 (1-C): 대상 DB 식별(`current_database()`, `pg_control_system()`), `pg_restore --list` 헤더 `dbname` 대조 기반 백업 검증, 메타 명령(`\i` 등) 차단, 가드 통과 후 `apply-<UTC>-<pid>.json` 배타 생성 및 파일별 `started`/`applied`/`failed` 기록, `VERBOSITY=sqlstate` 추출(원문 미기록), 트랜잭션 방식별 실행, 재개/손상 기록 중단, 체크섬 재대조, `psql` 없을 때 종료 코드 3
- [x] 1-D 구현: `Pg16PublicV1SnapshotProvider`(`deployment_common.sh` 동등 `pg16-public-v1` 지문), `build_schema_record()` 및 `--schema-record-out`, `model-init-evidence`(`init-*.json`) 구분, 카탈로그 동등성 비교 유틸리티, `tests/test_apply_migrations_postgres.py` (`IsolatedSchema` 기반 5개 시나리오 작성)
- [x] 전체 테스트 스위트 실행: `.venv\Scripts\python.exe -m pytest -q` → `915 passed, 231 skipped, 47 warnings, 1490 subtests passed in 258.21s (0:04:18)`
- [x] `dev_test_report --collect-only` + `dev_gate inventory`로 `docs/dev-automation/test-baseline.json` 갱신 (1146 tests collected)
- [x] `docs/migrations/README.md` 사용 문서 보강 (`scripts/apply_migrations.py` dry-run, `--apply` 필수 인자 및 가드, 종료 코드 0/1/2/3, `apply-*.json` vs `init-*.json` 기록 구분, 복구/재개 절차)
- [ ] 로컬/CI 격리 PostgreSQL(`SIGNUP_TEST_DATABASE_URL`, `psql`) 환경에서 `tests/test_apply_migrations_postgres.py` 실실행 및 실제 개발 DB 계정의 `pg_control_system()` 실행 권한 확인 (16-5/16-6 단계)

## 16-7 공식 데이터 수집과 초기 데이터 구성 (#116)

- [x] `scripts/seed_dev_data.py`에서 `current_pledge_version`을 `app.pledge_service`에서 가져오고, 세 정책 버전이 미설정일 때 시연용 기본값을 적용한 뒤 원래 환경을 복구한다.
- [x] 동의 기록과 지역 레벨 비교를 실제 DB 계약에 맞추고, 보호소 주소 매칭은 후보 지역을 보고한다. 공공 데이터 수집은 모의 응답으로만 검증했으며 실데이터 API/DB 적용은 수행하지 않았다.
- [x] SQLite 테스트에서 가상 시드의 목록 조회부터 예약, 승인, 보호소 코드, 산책 종료와 인계까지 검증한다. 테스트용 DB 의존성, 지역 필터, 응답 필드 및 시간 제어를 실제 API 계약에 맞췄다.
- [x] `python -m pytest -v tests/test_seed_dev_data.py tests/test_seed_dev_data_postgres.py` → 5 passed, 1 skipped.
- [x] `python -m pytest` (`DATABASE_URL=sqlite://`) → 973 passed, 235 skipped, 47 warnings. 1,208개 테스트를 수집했다.
- [x] collection-only 보고서와 `python -m scripts.dev_gate inventory <report>`로 `docs/dev-automation/test-baseline.json`을 1,208개 테스트 기준으로 갱신했다.
- [ ] `SIGNUP_TEST_DATABASE_URL`에 격리 PostgreSQL이 없어 `tests/test_seed_dev_data_postgres.py`를 실제 DB에서 실행하지 못했다. DB 연결 설정 후 스키마 격리 테스트를 실행한다.

## 인증번호 발송의 등록 여부 비노출 (#110)

- [ ] 17단계 FE 연동: 발송 200은 서버의 접수일 뿐 문자 수신이 아니다. 문자가 오지 않으면 60초 뒤 다시 요청하게 안내한다. 503 `SMS_DELIVERY_UNAVAILABLE`은 공급자 미설정(고정 인증번호 설정 오류 포함, #124)이나 발송 대기열 포화일 때만 온다. 발송 409 `PHONE_ALREADY_REGISTERED`와 확인 409 `VERIFICATION_NOT_SENT`는 더 이상 오지 않는다. 비동기 발송은 프로세스 종료 시 유실될 수 있고, 재요청으로 복구한다.
- [ ] 16-10 외부 공개 전 위험도 검토: 다른 사람이 한 번호의 하루 5회 발송 한도를 소진해 소유자의 인증을 막을 수 있다(기존 정책과 같음). 필요하면 별도 Issue로 정책을 정한다.
- [ ] 16-10 외부 공개 전 검증: `SUPPRESSED`는 발송 슬롯을 즉시 반납하고 실제 발송은 공급자 호출이 끝난 뒤 반납한다. 서버 전체 슬롯을 포화시키며 503 시점을 관찰하는 간접 노출 가능성을 실제 공급자 지연과 함께 확인한다. 실제 발송 요청은 응답 직후 발송 작업이 자기 계정의 `users` 행을 FOR SHARE로 잠시 잡으므로, 같은 계정이 곧바로 `users` FOR UPDATE가 필요한 요청을 보내면 미등록 번호일 때만 짧은 대기가 생길 수 있다. 이 타이밍 차이도 함께 확인한다.
- [x] 16-2: SOLAPI 어댑터 대신 Mock SMS 테스트 환경을 구성했다(#124). SOLAPI placeholder 코드 삭제는 20단계로 보류한다. `UNKNOWN` 정산 lookup은 confirm에서 제거됐으므로, 필요하면 운영 관측 용도로만 검토한다.

## Mock SMS 테스트 환경 (#124)

- [ ] 16-3 (#128): 환경변수 템플릿 3개, 비밀값 관리 절차, 실제 설정 점검 스크립트 및 인증 테스트 환경 분리를 검증하고 dev 병합한다. Mock SMS 설정 4개를 반영하고 SOLAPI 환경변수는 제거한다. 실제 설정값은 기록하지 않는다.
- [ ] 최초 시연 서버 배포 전: #116~#118의 승인된 설정을 EC2 `.env.api`에 반영하고 `.env`와 분리한 뒤 실제 설정 검증, Mock SMS 동작, 서버 접근 제한, #110 남은 보안 위험(위 항목)을 확인한다([EC2 설치](deployment/EC2_SETUP.md), [HANDOFF.md 8절](issues/110/HANDOFF.md#8-sms-정책-변경mock-sms)). 실제 적용은 별도 승인이다.
- [ ] 16-10: 역방향 프록시 및 Docker 경계를 지나도 가입, 로그인 요청 제한이 올바른 클라이언트 IP에 적용되는지 검증한다. 신뢰할 프록시 주소와 `FORWARDED_ALLOW_IPS` 설정을 함께 결정하며 임의의 전달 헤더나 전체 주소를 신뢰하지 않는다.
- [ ] 17단계 FE 연동: 고정 인증번호를 앱 안에서 비교하지 않고 BE 확인 API를 호출한다. 확인을 자동으로 반복하지 않는다.
- [ ] #116 Seed 데이터의 전화번호는 내부 합성 테스트 번호 9000~9999 대역을 권장한다.

## 서비스 정책 설정 (#118, 16-9)

- [x] 시연용 약관·개인정보 처리방침 문서와 manifest를 등록했다. 문서는 실제 법률 문구가 아니며, SHA-256은 UTF-8 본문의 CRLF를 LF로 정규화해 계산한다.
- [x] 환경 변수 전달 목록과 보호소 HMAC 키 생성·보관 절차를 `docs/16-3-environment-variables.md`에 기록했다. 비밀 값과 실서버 설정은 저장소에 두지 않는다.
- [ ] 실제 정책 문서는 법률 승인본으로 교체하고, EC2 `.env.api`의 비밀 설정과 레벨 정책 시작 시각을 별도 승인 후 적용·검증한다. 개발 DB 변경과 서버 배포는 이 작업에 포함하지 않는다.
- [ ] 20단계: 두 발송 라우터의 OpenAPI 503 설명과 `docs/issues/40/HANDOFF.md`의 SOLAPI 503 서술을 현재 동작(고정 인증번호 설정 오류 포함, SOLAPI 설정이 모두 있으면 200 접수 후 `FAILED`)에 맞춘다.
- [ ] 20단계 테스트 안정성 후보: 기존 `test_037`의 제약조건 조회를 대상 스키마로 한정하는 방안을 별도 승인 후 검토한다. 같은 PostgreSQL DB의 다른 테스트 실행과 겹치면 제약조건 조회가 충돌할 수 있다. #128에서는 이 테스트를 수정하지 않는다.

## 16-8 Kakao 위치 판별 설정 (#117)

- [x] 운영 결정과 설정 절차를 `docs/issues/46/TODO.md` 및 `docs/handoff/LATEST.md`에 기록: 시연 기본은 `LOCATION_GEOCODING_PROVIDER=mock`; 16-3 배포 담당자가 키를 배포 호스트 `.env.api`에 주입하고, 위치 화면(2-6)의 외부 전송·이용 목적 고지를 확인한 뒤에만 `kakao`와 `LOCATION_EXTERNAL_TRANSFER_ENABLED=true`로 전환
- [x] 고지 선행 조건 및 비밀정보 취급 명시: 현재 좌표의 Kakao 외부 전송과 이용 목적을 2-6 화면에서 고지하기 전에는 외부 전송을 활성화하지 않는다. 키는 저장소·Issue·로그에 기록하지 않는다.
- [x] 16-3 담당자 전달 환경변수 목록 문서화: `LOCATION_GEOCODING_PROVIDER`, `LOCATION_EXTERNAL_TRANSFER_ENABLED`, `KAKAO_REST_API_KEY`
- [x] Mock HTTP 검증: 정상/활성 지역 변환, 미지원 지역 422, timeout·외부 오류 503, 키 누락·`transfer_disabled`, 로그·응답 캐시 제어 및 좌표 비노출 확인 (`tests/test_location_resolution.py`: 53 passed)
- [x] 전체 회귀 및 기준 목록 갱신: 973 passed, 235 skipped, 47 warnings (1208 collected); `docs/dev-automation/test-baseline.json` 1208건
- [ ] 실제 Kakao 호출은 2-6 고지와 배포 호스트 `.env.api` 키 주입을 확인한 뒤에만 사용자가 별도 수동 검증한다. 기본 무료 쿼터는 일 100,000건이며 백엔드 timeout 3초와 64KB 응답 제한을 유지한다.
