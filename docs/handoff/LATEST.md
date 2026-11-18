# 인수인계: F-07 위험 상황 신고 (SC-07·SC-15)

- 날짜: 2026-11-18 (KST)
- 브랜치: `feature/f-07-danger-report`
- 작업 요약: 산책 중 및 종료 후 위험 상황 신고 접수(7개 유형, 좌표 대체, 3분 중복 판정, 멱등 처리) 및 보호소 알림 폴링/확인/이력 관리 API 구현 및 검증 완료.

## 1. 변경 파일
- `migrations/013_f07_incident_report.sql`: `walk_incidents` 테이블에 `client_report_id`, `acknowledged_at`, `acknowledged_by` 컬럼 추가, 유형 7종 체크 제약(`ck_incident_type`), 확인 쌍 제약(`ck_incident_acknowledgement`), 유니크 부분 인덱스(`uq_walk_incidents_reported_client_report_id`) 및 미확인 조회 부분 인덱스(`ix_walk_incidents_unacknowledged`) 추가
- `app/models.py`: `User.walk_incidents`에 `foreign_keys="WalkIncident.reported_by"` 관계 명시
- `app/walk_models.py`: `WalkIncident` 모델에 신규 컬럼 3종, 제약조건, 부분 인덱스 2종 및 `reporter` 관계 반영
- `app/walk_record_schemas.py`: `WalkIncidentCreate` (7종 유형, 선택 설명, `client_report_id`, `confirm_duplicate`), `WalkIncidentResponse` (`client_report_id`, `acknowledged_at` 추가), `ShelterIncidentDog`, `ShelterIncidentItem` 정의
- `app/walk_service.py`: `report_incident`, `locked_walk_for_incident` (종료 후 24시간 이내 산책 신고 허용, 3분 이내 동일 유형 중복 감지 및 `confirm_duplicate` 처리, `client_report_id` 동일 내용 200/상이 내용 409 멱등성, 산책 마지막 위치 대체, 한글 라벨 자동 채움)
- `app/routers/walks.py`: `POST /walks/{walk_id}/incidents` 멱등 반환 시 200 상태코드 지원
- `app/routers/shelter_incidents.py` (신규):
  - `GET /shelters/me/incident-alerts`: 미확인 신고 알림 조회 (`after_id` 지원, 보호소 계정 전용, id 오름차순)
  - `POST /shelters/me/incident-alerts/{incident_id}/ack`: 알림 확인 처리 (멱등 유지, 타 보호소 403)
  - `GET /shelters/me/incidents`: 보호소 신고 이력 목록 조회 (기간, 유형, 확인여부 필터, 관리자 `shelter_id` 지정)
- `app/main.py`: `shelters.router`보다 앞에 `shelter_incidents.router` 등록
- `.env.example`: `INCIDENT_DUPLICATE_WINDOW_SECONDS=180`, `INCIDENT_LATE_REPORT_HOURS=24` 추가
- `tests/test_f07_incident_report.py` (신규): F-07 완료 기준 12개 검증 테스트 작성
- `tests/test_walk_recording.py`: 설명 공백 시 422 기대치에서 201 한글 라벨 저장으로 변경된 부분 수정 및 동일 산책 신고 중복 방지 반영
- `tests/test_walk_domain.py`: 7종 제약조건(`ck_incident_type`) 검증 추가 및 후속 마이그레이션(011~013) DDL/스키마 프로젝션 호환 처리
- `docs/TODO.md`: F-07 전체 작업 목록 완료 체크
- `docs/handoff/LATEST.md`: F-07 인수인계 문서 작성

## 2. 결정 사항
- 마이그레이션 번호: `migrations/013_f07_incident_report.sql`로 확정.
- 신고 유형: 기존 4종(`INJURY`, `LOST_DOG`, `SAFETY_THREAT`, `OTHER`)에 3종(`POOP_NO_BAG`, `ATE_FOREIGN_OBJECT`, `ABNORMAL_BEHAVIOR`) 추가(총 7종).
- 멱등성: `client_report_id`로 동일 요청 재전송 시 200 반환, 상이한 내용 전송 시 409 반환.
- 중복 윈도우: 동일 산책, 동일 유형에 대해 발생 시각 차이 180초 이내인 경우 `confirm_duplicate=false`이면 `DUPLICATE_RECENT` 409 반환, `true`이면 201 저장.
- 종료 후 신고: `COMPLETED`, `INTERRUPTED` 산책은 종료 시각으로부터 24시간 이내 및 산책 기간 내 발생 시각인 경우 정상 신고 접수.
- 좌표 대체: 요청 좌표가 없으면 `walks.last_latitude/longitude` 사용, 없으면 NULL 저장.
- 보호소 알림 격리: `GET /shelters/me/incident-alerts`는 보호소 계정(`UserRole.SHELTER`) 전용이며, 봉사자 및 관리자는 403 반환. 타 보호소 알림은 보이지 않음.
- 라우트 충돌 방지: `app/main.py`에서 `shelter_incidents.router`(`/shelters/me/...`)를 `shelters.router`(`/{shelter_id}`)보다 상단에 등록.

## 3. 실행한 테스트 명령과 결과
```powershell
$env:PYTHONPATH="."; pytest tests/test_f07_incident_report.py tests/test_walk_recording.py tests/test_f06_walk_tracking.py tests/test_f05_walk_approval.py tests/test_walk_domain.py -v
```

### 테스트 결과 요약
- `tests/test_f07_incident_report.py`: 12 passed (100%)
- `tests/test_walk_recording.py`: 38 passed (100%)
- `tests/test_f06_walk_tracking.py`: 10 passed (100%)
- `tests/test_f05_walk_approval.py`: 11 passed (100%)
- `tests/test_walk_domain.py`: 22 passed (79 subtests passed) (100%)
- **총합: 93 passed, 11 warnings, 79 subtests passed in 11.33s (전체 성공)**

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 신고 5종 저장, status OPEN, 상세 공백 시 한글 라벨 자동 저장 | `test_report_incident_success_and_default_korean_label` | 통과 (PASSED) |
| 필수 유형 누락 및 미지원 유형 422 | `test_invalid_incident_type_returns_422` | 통과 (PASSED) |
| 보호소 알림 조회 (미확인 목록, after_id 지원, 보호소 격리) | `test_shelter_incident_alerts_polling` | 통과 (PASSED) |
| 타 보호소 알림 격리 및 일반 사용자/관리자 접근 제한 (403) | `test_incident_alerts_authorization` | 통과 (PASSED) |
| 알림 확인 처리 멱등성 및 이력 보존 | `test_acknowledge_incident_alert_idempotent` | 통과 (PASSED) |
| 3분 이내 동일 유형 중복 시 409 및 confirm_duplicate 재전송 시 201 | `test_duplicate_incident_window_and_confirmation` | 통과 (PASSED) |
| client_report_id 멱등 재전송 200 반환 및 상이 내용 409 | `test_client_report_id_idempotency` | 통과 (PASSED) |
| 종료 24시간 이내 산책 신고 201 및 초과/CANCELLED 409 | `test_late_report_for_ended_walk` | 통과 (PASSED) |
| 좌표 누락 시 산책 마지막 위치 대체 및 둘 다 없을 시 NULL | `test_fallback_to_walk_last_coordinates` | 통과 (PASSED) |
| 보호소 신고 이력 필터링 및 관리자 shelter_id 지정 조회 | `test_shelter_incidents_history_filters` | 통과 (PASSED) |
| 동일 산책 동일 유형 동시 신고 레이스 컨디션 방어 | `test_concurrent_incident_race_condition` | 통과 (PASSED) |
| 회귀 검증: 산책 권한, GET /walks/{id} incidents 조회, F-06 통과 | `test_regression_rules` | 통과 (PASSED) |

## 5. 로컬 실행 준비
1. PostgreSQL 마이그레이션 실행:
   ```bash
   psql -d <DB_NAME> -f migrations/013_f07_incident_report.sql
   ```
2. 환경 변수 확인 (`.env`):
   ```env
   INCIDENT_DUPLICATE_WINDOW_SECONDS=180
   INCIDENT_LATE_REPORT_HOURS=24
   ```

## 6. 확인하지 못한 것
- **Windows Sandbox 환경 내 TestClient 실행 제약**: Windows 격리 샌드박스 내부에서는 `asyncio` self-pipe socketpair가 차단되어 `TestClient` 실행 시 블로킹이 발생하므로, 샌드박스 네트워크 격리가 해제된 PowerShell 환경(`BypassSandbox: true` / 로컬 개발 터미널)에서 검증을 완료하였습니다.
- **실제 PostgreSQL 통합 테스트(`tests/test_walk_recording_postgres.py`)**: 구동 중인 외부 PostgreSQL 인스턴스 접속 정보(`DATABASE_URL`)가 필요한 테스트로, 본 환경에서는 SQLite 인메모리 테스트 93건으로 전체 비즈니스 로직을 검증하였습니다.

## 7. 다음 할 일 및 주의사항
- 본 기능 구현 완료 후 `feature/f-07-danger-report` 브랜치를 원격에 push하고 `dev` 대상 Pull Request(PR) 생성.
- 프론트엔드 연동 참고사항:
  - 위험 상황 신고 요청 시 네트워크 재전송에 대비하여 클라이언트 UUID를 `client_report_id` 필드에 포함하여 전송 권장 (동일 신고 재전송 시 200 반환).
  - 3분 이내 동일 유형 신고 시 409 (`DUPLICATE_RECENT`) 에러 발생 시, 사용자에게 "최근 동일한 유형의 신고가 접수되었습니다. 추가로 접수하시겠습니까?" 팝업 표시 후 사용자가 확인하면 `confirm_duplicate=true`를 포함하여 재요청.
  - 보호소 웹/앱 클라이언트는 `GET /shelters/me/incident-alerts?after_id={last_seen_id}`를 폴링하여 신규 미확인 위험 신고를 수신하고, 확인 버튼 클릭 시 `POST /shelters/me/incident-alerts/{id}/ack` 호출.
