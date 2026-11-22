# 인수인계: F-08 실시간 산책 확인 (보호소, SC-15)

- 날짜: 2026-11-22 (KST)
- 브랜치: `feature/f-08-live-walk`
- 작업 요약: 보호소가 현재 산책 중(`IN_PROGRESS`)인 강아지별로 위치와 진행 상황을 확인하는 백엔드 API (카드 목록, 산책 1건 지도 상세, 경로/마커 증분 조회, stale 판정, 산책자 연락처 노출 제어) 구현 및 전체 회귀 테스트 검증 완료.

## 1. 변경 파일
- `migrations/014_f08_live_walk.sql` (신규): `users` 테이블에 산책자 연락처 저장을 위한 `phone VARCHAR(50)` 컬럼 추가
- `app/models.py`: `User` 모델에 `phone = Column(String(50), nullable=True)` 추가
- `app/walk_live_schemas.py` (신규):
  - `LiveWalkCardResponse`: 산책 중 강아지 카드 정보 (`walk_id`, `dog_id`, `dog_name`, `dog_image_url`, `walker_nickname`, `started_at`, `elapsed_seconds`, `scheduled_duration_minutes`, `scheduled_end_at`, `last_latitude`, `last_longitude`, `last_location_at`, `is_stale`)
  - `LiveWalkMarkerResponse`: 지도 마커 (`marker_type`: START | INCIDENT, `latitude`, `longitude`, `occurred_at`, `incident_type`, `description`)
  - `LiveWalkPointResponse`: 경로 좌표 (`sequence_no`, `latitude`, `longitude`, `recorded_at`)
  - `LiveWalkDetailResponse`: 카드 정보 + `route` 전체 경로 + `markers` 행동마커 + `walker_phone` (산책자 전화번호)
  - `LiveWalkUpdatesResponse`: 증분 갱신 정보 (`walk_id`, `is_stale`, `last_location_at`, `last_latitude`, `last_longitude`, `route`, `markers`)
- `app/walk_live_service.py` (신규):
  - `get_live_walk_stale_seconds()`: 환경 변수(`LIVE_WALK_STALE_SECONDS`, 기본값 60초) 로드
  - `compute_walk_live_status()`: 경과 시간 계산 및 60초 위치 미갱신 시 stale 판정
  - `build_live_walk_markers()`: 시작 지점(`START`) 마커 및 F-07 위험 신고(`INCIDENT`) 마커 7종 조립
  - `get_live_walks()`: 보호소 소속 `IN_PROGRESS` 산책 목록 조회 (관리자 `shelter_id` 파라미터 지원, 타 보호소/봉사자 403)
  - `get_live_walk_detail()`: 진행 중 산책 1건 상세 조회 (전화번호 포함, 타 보호소/봉사자 403, 종료 산책 404)
  - `get_live_walk_updates()`: `after_seq` 기준 경로 증분 조회 및 마커 전체 반환 (전화번호 제외)
- `app/routers/shelter_live_walks.py` (신규):
  - `GET /shelters/me/live-walks`: 산책 중 강아지 카드 목록 조회
  - `GET /shelters/me/live-walks/{walk_id}`: 지도용 1건 상세 조회
  - `GET /shelters/me/live-walks/{walk_id}/updates`: 경로 및 마커 증분 조회 (폴링용)
- `app/main.py`: `shelters.router`(`/{shelter_id}`) 앞에 `shelter_live_walks.router` 등록하여 라우트 충돌 방지
- `.env.example`: `LIVE_WALK_STALE_SECONDS=60` 환경 변수 추가
- `tests/test_f08_live_walk.py` (신규): F-08 완료 기준 8대 항목 검증 테스트 작성
- `docs/TODO.md`: `# F-08 실시간 산책 확인 TODO` 섹션 추가 및 작업 완료 체크
- `docs/handoff/LATEST.md`: F-08 인수인계 문서 작성

## 2. 결정 사항
- **DB 스키마 마이그레이션 (`migrations/014_f08_live_walk.sql`)**:
  - 산책자 연락처 조회를 위해 `users` 테이블에 `phone VARCHAR(50)` 컬럼 추가.
  - 기존 walks, walk_points, walk_incidents 테이블 구조 및 인덱스는 그대로 재사용.
- **실시간 데이터 동기화 방식**:
  - WebSocket/SSE를 사용하지 않고 HTTP GET 기반 주기적 폴링 방식으로 구현.
  - 증분 조회(`GET /shelters/me/live-walks/{walk_id}/updates?after_seq=`)를 통해 클라이언트가 이미 수신한 `sequence_no` 이후의 경로 좌표만 전송하여 대역폭 절약.
  - 마커(`markers`)는 산책 1건당 발생 빈도가 낮으므로 동기화 정합성을 위해 전체 마커 목록을 반환.
- **행동 마커 구성**:
  - `START`: `sequence_no=0` (최초 기록 좌표) 위치와 시각을 시작 마커로 생성. (좌표가 1건도 수신되지 않은 경우 미노출)
  - `INCIDENT`: F-07 `walk_incidents` 테이블에서 발생한 7개 유형의 위험 신고 마커(유형, 설명, 좌표, 발생 시각)를 시간 순서대로 포함.
- **위치 지연(Stale) 판정**:
  - 환경 변수 `LIVE_WALK_STALE_SECONDS`(기본값 60초) 기준.
  - 마지막 수신 좌표(`walk.last_point_recorded_at`)가 있는 경우: `now - last_point_recorded_at >= 60초`이면 `is_stale=true`.
  - 산책 시작 후 아직 좌표가 수신되지 않은 경우: `now - started_at >= 60초`이면 `is_stale=true`.
  - 신규 좌표 업로드 시 즉시 `is_stale=false`로 정상 복구.
- **보안 및 개인정보 보호**:
  - 봉사자의 전화번호(`walker_phone`)는 해당 보호소의 **진행 중(`IN_PROGRESS`) 산책 상세 조회(`GET /shelters/me/live-walks/{walk_id}`) 응답에만 포함**.
  - 카드 목록(`live-walks`) 및 증분 갱신(`updates`) 응답에는 `walker_phone` 필드가 아예 포함되지 않도록 스키마를 분리.
  - 봉사자의 닉네임(`walker_nickname`)만 공통 노출.
- **권한 제어 및 라우트 충돌 방지**:
  - 보호소 계정(`UserRole.SHELTER`): 본인 소속 보호소(`actor.shelter_id`)의 산책만 조회 가능. 타 보호소 접근 시 403.
  - 봉사자 계정(`UserRole.USER`): 모든 실시간 산책 엔드포인트 접근 차단 (403).
  - 관리자 계정(`UserRole.ADMIN`): 카드 목록에서 `shelter_id` 쿼리 파라미터로 특정 보호소 지정 조회 허용 (미지정 시 400). 단건 상세/증분은 모든 산책 조회 가능.
  - `app/main.py`에서 `shelters.router`(`/{shelter_id}`)보다 상단에 `shelter_live_walks.router`를 등록하여 `/shelters/me/...` 경로가 경로 파라미터로 오인되는 문제 방지.

## 3. 실행한 테스트 명령과 결과
```powershell
$env:PYTHONPATH="."; pytest tests/test_f08_live_walk.py tests/test_f07_incident_report.py tests/test_walk_recording.py tests/test_f06_walk_tracking.py tests/test_f05_walk_approval.py tests/test_walk_domain.py -v
```

### 테스트 결과 요약
- `tests/test_f08_live_walk.py`: 8 passed (100%)
- `tests/test_f07_incident_report.py`: 12 passed (100%)
- `tests/test_walk_recording.py`: 38 passed (100%)
- `tests/test_f06_walk_tracking.py`: 11 passed (100%)
- `tests/test_f05_walk_approval.py`: 12 passed (100%)
- `tests/test_walk_domain.py`: 20 passed (79 subtests passed) (100%)
- **총합: 101 passed, 11 warnings, 79 subtests passed in 21.50s (전체 성공)**

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 위치 갱신 시 최신 좌표 반환 및 after_seq 증분 조회 | `test_live_walk_location_updates_and_incremental_after_seq` | 통과 (PASSED) |
| 산책 종료·취소 시 목록에서 사라지고 상세/증분 404 | `test_walk_disappears_on_end_or_cancel` | 통과 (PASSED) |
| 산책 중인 강아지가 없으면 빈 목록([]) 반환 | `test_empty_list_when_no_active_walks` | 통과 (PASSED) |
| 60초 이상 위치 갱신 없으면 is_stale=true 및 갱신 시 복구 | `test_is_stale_after_60_seconds_without_update` | 통과 (PASSED) |
| 상세에 START 마커 및 F-07 위험 신고(INCIDENT) 마커 7종 포함 | `test_detail_contains_start_and_incident_markers` | 통과 (PASSED) |
| 타 보호소 403, 봉사자 403, 관리자 shelter_id 지정 조회 가능 | `test_authorization_shelter_isolation_and_admin_access` | 통과 (PASSED) |
| 산책자 전화번호는 상세 응답에만 포함 (목록/증분 미포함) | `test_walker_phone_only_in_detail_response` | 통과 (PASSED) |
| 카드 목록 응답 필수 필드 및 경과 시간 계산 검증 | `test_live_walk_card_fields_and_elapsed_time` | 통과 (PASSED) |
| 기존 F-05~F-07 및 도메인 전체 회귀 테스트 통과 | 전체 101개 테스트 통과 | 통과 (PASSED) |

## 5. 로컬 실행 준비
1. PostgreSQL 마이그레이션 실행:
   ```bash
   psql -d <DB_NAME> -f migrations/014_f08_live_walk.sql
   ```
2. 환경 변수 확인 (`.env`):
   ```env
   LIVE_WALK_STALE_SECONDS=60
   ```

## 6. 확인하지 못한 것
- **실제 PostgreSQL 통합 테스트**: 구동 중인 외부 PostgreSQL 인스턴스 접속 정보(`DATABASE_URL`)가 필요한 테스트 환경은 분리되어 있어, SQLite 인메모리 테스트 101건으로 전체 비즈니스 로직 및 회귀 검증을 완료하였습니다.
- **모바일 단말 GPS 실기기 송수신**: 실기기 GPS 센서 송수신 및 지도 SDK 렌더링은 앱(프론트엔드) 영역이므로, 백엔드 API 명세에 따른 HTTP 단위/통합 테스트를 통해 데이터 정합성을 검증하였습니다.

## 7. 다음 할 일 및 주의사항 (프론트 연동 참고)
- **프론트엔드(보호소 관리자 웹/앱, SC-15) 연동 가이드**:
  1. **산책 목록 화면**: `GET /shelters/me/live-walks`를 약 5~10초 간격으로 폴링하여 현재 산책 중인 강아지 카드 목록을 표시합니다.
     - `is_stale: true`인 카드는 UI 상에서 "위치 수신 지연" 또는 주황색 경고 뱃지를 표시합니다.
     - `elapsed_seconds`는 서버 시각 기준 경과 초이므로 클라이언트 타이머와 동기화할 수 있습니다.
  2. **지도 상세 화면**: 특정 강아지 카드를 클릭하여 지도에 진입할 때 `GET /shelters/me/live-walks/{walk_id}`를 1회 호출하여 초기 전체 경로(`route`)와 `markers` (START, INCIDENT) 및 산책자 연락처(`walker_phone`)를 표시합니다.
  3. **실시간 위치 갱신(폴링)**: 이후에는 `GET /shelters/me/live-walks/{walk_id}/updates?after_seq={마지막_수신_sequence_no}`를 약 3~5초 주기로 폴링하여 신규 좌표만 기존 폴리라인에 이어 붙입니다.
  4. **산책자 전화 걸기**: 상세 응답의 `walker_phone`을 이용하여 "산책자에게 전화하기" 버튼을 활성화합니다. (목록이나 증분 응답에는 전화번호가 제공되지 않습니다.)
