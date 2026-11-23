# 인수인계: F-10 행동마커 저장 (SC-07)

- 날짜: 2026-11-23 (KST)
- 작성: 예지 + Claude
- 브랜치: `feature/f-10-behavior-marker` · 마지막 커밋: 없음 (dev `c45c3b3` 기준, 사용자가 커밋 예정)
- 이슈·기능: F-10 행동마커 저장 (SC-07)

## 1. 이번에 한 일
- 마이그레이션 추가: 유형 `CROSSWALK_WAIT`, `OTHER`, 컬럼 `location_source`, OTHER 메모 필수 제약 (`migrations/017_f10_behavior_marker.sql`)
- `WalkMarker` 모델에 같은 제약·컬럼 반영 (`app/walk_models.py`)
- 요청 스키마 `WalkMarkerCreate` 재작성: 좌표·`accuracy_m` 선택, OTHER 메모 필수·공백 제거·최대 100자, 좌표는 위도·경도 쌍 (`app/walk_record_schemas.py`)
- 응답 `WalkMarkerResponse`에 `location_source` 추가 (`GET /walks/{id}`, F-09 summary가 같은 모델 사용)
- `add_marker`: 위치 결정(GPS 약함 → 마지막 위치), 500개 상한, 환경변수 읽기 (`app/walk_service.py`)
- 보호소 실시간: `marker_type="BEHAVIOR"`, `behavior_type` 추가, 시각순 병합 (`app/walk_live_schemas.py`, `app/walk_live_service.py`)
- `.env.example`에 `WALK_MARKER_MAX_ACCURACY_M=50`, `WALK_MARKER_MAX_PER_WALK=500` 추가
- 테스트 `tests/test_f10_behavior_marker.py` 15건 작성, `tests/test_walk_domain.py` 007 DDL 프로젝션 보정

## 2. 결정 사항과 이유
- 결정: PostgreSQL만 사용, 기존 `walk_markers`·`POST /walks/{id}/markers` 확장 (새 테이블·엔드포인트 없음)
  - 이유: 산책 중 지도(`GET /walks/{id}`)와 완료 화면(F-09 summary)이 이미 같은 테이블과 응답 모델을 쓴다.
  - 버린 대안: Firestore 도입 (F-06~F-09와 저장소가 달라져 일관성이 깨짐)
- 결정: 기존 유형 3종(DOG_ENCOUNTER·PERSON_ENCOUNTER·BARKING)을 DB·API에서 유지
  - 이유: 기존 데이터와 기존 테스트 호환
  - 버린 대안: 제거 (기존 행이 제약에 걸릴 수 있음)
- 결정: GPS 약함 = 좌표 없음 또는 `accuracy_m` > 50m(`WALK_MARKER_MAX_ACCURACY_M`). 이때 `walks.last_latitude/longitude`로 저장하고 `location_source=LAST_KNOWN`. 50m는 약함이 아님.
  - 이유: 앱이 약한 좌표를 보내도 마커를 잃지 않게 하고, 대체 여부를 화면에서 구분할 수 있게 함
  - 버린 대안: 서버가 정확도 없이 판단 (좌표가 있고 정확도가 없으면 요청 좌표를 신뢰)
- 결정: 마지막 위치도 없으면 409, 산책당 500개 초과 시 409 (`locked_walk`로 잠근 안에서 셈)
- 결정: OTHER 메모는 공백 제거 후 저장, 100자 초과 422. 다른 유형의 공백뿐인 메모는 `null`로 저장
- 결정: 보호소 실시간은 START 먼저, INCIDENT·BEHAVIOR는 시각순 (동시각이면 INCIDENT가 먼저, 그다음 id)
  - 이유: 기존 START·INCIDENT 응답 형태와 순서를 바꾸지 않음
- 결정: `docs/TODO.md`, `docs/handoff/LATEST.md`는 F-10 내용만 새로 작성, `docs/team/`은 수정하지 않음
  - 이유: dev에는 두 경로가 따로 있고 `docs/team/`이 팀 이력 보관 위치임
- 결정: `007_walk_domain.schema.json`, `008_*.schema.json`은 수정하지 않음
  - 이유: 007 계약은 "역사적으로 고정"이며 012 이후 변경도 반영하지 않았음. 대신 `tests/test_walk_domain.py`의 후속 마이그레이션 제외 목록에 017 추가분을 넣음
- 결정: 연타 중복 방지(`client_marker_id`), 마커 수정·삭제, 오프라인 재전송은 제외

## 3. 현재 상태
- 테스트: 실행 환경은 Linux 샌드박스, Python 3.12.3, SQLite(`DATABASE_URL=sqlite://`)
  - `PYTHONPATH=. python3 -m pytest tests/test_f10_behavior_marker.py -q` → 15 passed
  - `PYTHONPATH=. python3 -m pytest tests -q` → 433 passed, 90 skipped, 2 failed
  - 실패 2건은 `tests/test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`(`difficulty: INTERMEDIATE`). F-10 변경을 빼고 dev(`c45c3b3`)에서도 같은 2건이 실패함 (F-10과 무관)
  - 90 skipped는 PostgreSQL 전용 테스트 (`*_postgres.py`)
- 완료 기준 대조:
  | 완료 기준 | 테스트 항목 | 결과 |
  |---|---|---|
  | 행동 선택 4종 저장 | `test_four_behavior_types_are_saved_with_current_location` | 통과 |
  | 기타 내용 비면 저장 불가 | `test_other_requires_non_blank_content`, `test_database_rejects_blank_other_and_unknown_location_source` | 통과 |
  | GPS 약하면 마지막 위치에 저장 | `test_missing_coordinates_use_last_known_location`, `test_weak_accuracy_uses_last_known_and_boundary_keeps_current`, `test_accuracy_threshold_comes_from_environment`, `test_no_location_to_fall_back_to_is_conflict` | 통과 |
  | 산책 중 지도에 표시 | `test_saved_markers_appear_in_walk_detail_and_completion_summary` (`GET /walks/{id}`) | 통과 |
  | 완료 화면에 표시 | 같은 테스트 (`GET /walks/{id}/summary`의 `markers`, `marker_count`) | 통과 |
  | 보호소 실시간 화면에 표시 | `test_shelter_live_walk_shows_behavior_markers_in_time_order`, `test_live_walk_without_behavior_markers_is_unchanged_and_other_shelter_is_blocked` | 통과 |
  | 상한·권한·상태·시각 | `test_marker_limit_per_walk`, `test_owner_state_and_time_rules_are_kept` | 통과 |
- 마이그레이션: `017_f10_behavior_marker.sql` 작성. PostgreSQL 문법 파싱(`pglast`)만 통과, 실제 DB 적용은 하지 못함
- 확인한 것:
  - ORM(SQLite) 제약 동작: OTHER 공백 메모와 잘못된 `location_source`를 DB가 거부함
  - 기존 F-05~F-09, 산책 기록·도메인 테스트가 수정 없이 통과 (도메인 테스트만 007 프로젝션 보정)
- 확인하지 못한 것:
  - 실제 PostgreSQL에서 017 적용 (`psql`)과 기존 데이터에 대한 적용 결과
  - PostgreSQL 전용 테스트 90건 (`WALK_RECORDS_TEST_DATABASE_URL` 등이 필요해 건너뜀)
  - 동시 요청에서 500개 상한이 지켜지는지 (SQLite는 행 잠금이 없어 `locked_walk` 잠금 효과를 검증하지 못함)
  - `scripts/walk_schema.py`의 카탈로그 검증이 017 이후 DB에서 어떻게 동작하는지
  - 앱 화면(BottomSheetDialog, 지도 Marker)과 실제 단말 GPS 동작

## 4. 알려진 문제·주의점
- `LAST_KNOWN` 마커의 시각(`recorded_at`)은 마커를 남긴 시각이고 위치만 마지막 점의 좌표다. 앱이 "마지막 위치에 저장됨" 표시가 필요하면 `location_source`를 쓴다.
- `walks.last_*`는 경로 점 업로드(`POST /walks/{id}/points`)로 갱신된다. 점이 한 번도 올라오지 않은 산책에서 좌표 없이 마커를 보내면 409다.
- `.env.example`의 새 값 두 개를 로컬 `.env`에도 추가해야 기본값(50, 500) 외 값을 쓸 수 있다. 없어도 기본값으로 동작한다.
- 응답에 `location_source`가 추가되었다. 기존 앱은 모르는 필드를 무시하면 영향이 없다. 보호소 실시간 응답의 `marker_type`에 `BEHAVIOR`가 새로 나오므로 앱이 모르는 값을 처리하는지 확인이 필요하다.
- dev 기준 `test_walk_settings_api` 2건 실패는 이번 변경과 무관한 기존 문제다.
- 비밀값·개인정보: 커밋하거나 문서에 쓴 것 없음 (`.env.example`에는 값 이름과 기본값만 있음)

## 5. 다음 할 일
- [ ] 로컬 PostgreSQL에 017 적용 후 확인 (아래 6번)
- [ ] 커밋·push, PR 생성 (feature → dev)
- [ ] 앱: BottomSheetDialog, 지도 Marker, `LAST_KNOWN` 표시 방식, 보호소 화면의 `BEHAVIOR` 처리

## 6. 이어서 시작하는 법
- 먼저 읽을 파일:
  - `docs/handoff/LATEST.md` (이 문서)
  - `docs/TODO.md` (다음 할 일을 고를 때), 이전 기능 이력은 `docs/team/TODO.md`
- 환경:
  - 가상환경에 `requirements.txt` 설치, 비밀값은 `.env` (예시는 `.env.example`, 실제 값은 저장소에 없음)
  - DB: 로컬 PostgreSQL (`DATABASE_URL`)
- 실행 명령:
  - 마이그레이션: `psql -d <DB_NAME> -f migrations/017_f10_behavior_marker.sql`
  - F-10 테스트: `$env:PYTHONPATH="."; pytest tests/test_f10_behavior_marker.py -v`
  - 관련 회귀: `$env:PYTHONPATH="."; pytest tests/test_walk_recording.py tests/test_walk_domain.py tests/test_f08_live_walk.py tests/test_f09_walk_review.py -v`
