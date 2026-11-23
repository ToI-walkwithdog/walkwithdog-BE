# 인수인계: F-11 산책 시간 초과 경고 (SC-15)

- 날짜: 2026-11-23 (KST)
- 작성: 예지 + Gemini
- 브랜치: `feature/f-11-walk-overtime-alert` · 마지막 커밋: 없음 (dev 기준, 사용자가 패치 적용 후 커밋 예정)
- 이슈·기능: F-11 산책 시간 초과 경고 (SC-15)

## 1. 이번에 한 일
- 마이그레이션 추가: `walks` 테이블에 `planned_duration_minutes`, `expected_end_at` 컬럼 및 `ck_walk_planned_duration`, `ck_walk_expected_end_order` 제약 추가 (`migrations/018_f11_walk_overtime.sql`)
- `Walk` 모델에 컬럼·제약조건과 조회 시점 계산 프로퍼티 `is_overdue`, `overdue_minutes` 추가 (`app/walk_models.py`)
- `start_walk`에서 `DogWalkSetting.recommended_duration_minutes`를 조회해 `planned_duration_minutes`와 `expected_end_at` 저장 (미설정 시 `WalkRequest` 예약 소요시간으로 폴백) (`app/walk_service.py`)
- `get_walk_overdue_grace_minutes()`, `compute_walk_overdue()` 구현 및 `get_active_walk()` 응답 확장 (`app/walk_service.py`)
- 사용자 산책 스키마(`WalkResponse`, `ActiveWalkResponse`)와 보호소 실시간 스키마·서비스(`LiveWalkCardResponse`, `LiveWalkDetailResponse`, `LiveWalkUpdatesResponse`)에 `expected_end_at`, `is_overdue`, `overdue_minutes` 반영 (`app/walk_record_schemas.py`, `app/walk_live_schemas.py`, `app/walk_live_service.py`)
- `.env.example`에 `WALK_OVERDUE_GRACE_MINUTES=0` 추가
- 완료 기준 검증 테스트 `tests/test_f11_walk_overtime.py` 6건 작성 및 `tests/test_walk_domain.py` 007 DDL 프로젝션·계약 검증 보정

## 2. 결정 사항과 이유
- 결정: PostgreSQL 단독 사용 (Firestore 미도입)
  - 이유: F-06~F-10과 저장소를 일원화하고 보호소 실시간 조회(`walk_live`) 및 산책 상세(`GET /walks/{id}`) 응답 필드로 일관되게 제공하기 위함
  - 버린 대안: Firestore `users` 문서 이중 쓰기
- 결정: 예정 산책 소요시간은 `dog_walk_settings.recommended_duration_minutes`를 우선 조회하고, 미설정(`NULL`)인 경우 승인된 `walk_requests`의 예약 소요시간(`requested_end_at - requested_start_at`)으로 폴백
  - 이유: 강아지별 권장 산책 시간 정책을 따르면서 기존 테스트·미설정 강아지에서도 `expected_end_at`이 누락되지 않게 하기 위함
- 결정: 백그라운드 타이머/워커 대신 조회 시점 계산 방식 채택, `WALK_OVERDUE_GRACE_MINUTES`(기본값 `0`) 유예 시간 적용
  - 이유: 서버 재시작이나 스케줄러 누락 없이 `GET /shelters/me/live-walks`, `GET /walks/{id}`, `GET /walks/active` 호출 시점에 즉시 정확한 초과 여부와 초과 분(`overdue_minutes`)을 계산할 수 있음
- 결정: `walk.status != "IN_PROGRESS"`(종료·중단·취소)인 경우 `compute_walk_overdue()`가 즉시 `(False, 0)`을 반환하여 경고 자동 해제, 보호소 실시간 목록에서도 자동 제외

## 3. 현재 상태
- 테스트: 로컬 Windows 환경, Python 3.13.7, pytest 9.2.0, SQLite(`DATABASE_URL=sqlite://`)
  - `$env:PYTHONPATH="."; pytest tests/test_f11_walk_overtime.py tests/test_walk_domain.py tests/test_f08_live_walk.py -v` → 전체 통과 (F-11 수용 테스트 6건 + 도메인 계약 + F-08 실시간 산책 회귀)
- 완료 기준 대조:
  | 완료 기준 | 테스트 항목 | 결과 |
  |---|---|---|
  | 산책 시작 시 예정 종료 시각 계산 | `test_expected_end_at_calculated_from_dog_walk_setting`, `test_expected_end_at_falls_back_to_request_duration_when_setting_missing` | 통과 |
  | 예정 시간 초과 시 보호소 카드 경고 표시 | `test_overdue_warning_appears_on_shelter_and_user_views_after_expected_end` | 통과 |
  | 유예 시간(`WALK_OVERDUE_GRACE_MINUTES`) 동작 | `test_grace_minutes_environment_variable_delays_overdue` | 통과 |
  | 산책 종료 시 경고 자동 해제 | `test_overdue_warning_clears_automatically_when_walk_ends` | 통과 |
  | DB 제약조건 및 도메인 계약 정합성 | `test_walk_model_constraints_reject_invalid_planned_duration_or_expected_end`, `tests/test_walk_domain.py` | 통과 |
- 확인한 것:
  - ORM(SQLite) 제약 동작: `planned_duration_minutes <= 0` 및 `expected_end_at <= started_at` 입력을 DB가 거부함
  - `compute_walk_overdue`의 경계값(`now == deadline` → `False`, `now > deadline` → `True`), 유예 시간(`WALK_OVERDUE_GRACE_MINUTES`), 잘못된 환경변수 폴백(`0`), 산책 종료(`COMPLETED`) 시 경고 자동 해제
  - `LiveWalkCardResponse`의 기존 F-08 필드(`scheduled_duration_minutes`, `scheduled_end_at`) 하위 호환 유지
- 확인하지 못한 것:
  - 로컬 PostgreSQL에 `migrations/018_f11_walk_overtime.sql` 실제 `psql` 적용 결과 (F-10의 `017`과 동일하게 배포·DB 반영 시 적용)
  - 앱 화면(SC-15 보호소 카드 빨간 테두리·'시간 초과' 뱃지, 사용자 화면 종료 안내) 렌더링

## 4. 알려진 문제·주의점
- 기존 `walks` 행은 `planned_duration_minutes`와 `expected_end_at`이 `NULL`이므로 마이그레이션 018은 `NULL` 허용으로 추가했다. `expected_end_at`이 `NULL`인 기존 레코드는 `is_overdue=False`, `overdue_minutes=0`으로 안전하게 처리된다.
- `.env.example`에 추가된 `WALK_OVERDUE_GRACE_MINUTES=0`을 로컬 `.env`에 반영하지 않아도 기본값 `0`으로 동작한다.
- 보호소 실시간 카드(`LiveWalkCardResponse`)의 기존 `scheduled_duration_minutes`/`scheduled_end_at`(예약 시간 기준)은 F-08 하위 호환을 위해 유지하고, F-11 권장 시간 기준 예정 종료 시각은 `expected_end_at` 필드로 별도 제공한다.
- 비밀값·개인정보: 커밋하거나 문서에 쓴 것 없음 (`.env.example`에는 키 이름과 기본값만 포함)

## 5. 다음 할 일
- [x] 로컬에서 `f11.patch` 적용 및 `pytest` 실행 완료
- [ ] 로컬 PostgreSQL에 `018_f11_walk_overtime.sql` 적용 (아래 6번)
- [ ] 커밋·push 및 PR 생성 (`feature/f-11-walk-overtime-alert` → `dev`)
- [ ] 앱(Android): SC-15 보호소 실시간 카드에 `is_overdue` 기반 빨간 테두리·'시간 초과' 뱃지 표시 및 사용자 화면 종료 안내 연동

## 6. 이어서 시작하는 법
- 먼저 읽을 파일:
  - `docs/handoff/LATEST.md` (이 문서)
  - `docs/TODO.md` (F-11 체크리스트)
- 환경:
  - 가상환경에 `requirements.txt` 설치, 비밀값은 `.env` (예시는 `.env.example`, 실제 값은 저장소에 없음)
  - DB: 로컬 PostgreSQL (`DATABASE_URL`)
- 실행 명령:
  - 패치 검증 및 적용: `git apply --check f11.patch` 후 `git apply f11.patch`
  - 마이그레이션: `psql -d <DB_NAME> -f migrations/018_f11_walk_overtime.sql`
  - F-11 테스트: `$env:PYTHONPATH="."; pytest tests/test_f11_walk_overtime.py -v`
  - 관련 회귀: `$env:PYTHONPATH="."; pytest tests/test_walk_recording.py tests/test_walk_domain.py tests/test_f08_live_walk.py tests/test_f09_walk_review.py -v`
