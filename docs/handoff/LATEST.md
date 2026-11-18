# 인수인계: F-07 위험 상황 신고 (SC-07·SC-15)

- 날짜: 2026-11-18 (KST)
- 브랜치: `feature/f-07-danger-report`
- 작업 요약: 산책 중 및 종료 후 위험 상황 신고 접수(7개 유형, 좌표 대체, 3분 중복 판정, 멱등 처리) 및 보호소 알림/확인/이력 관리 기능 구현 중.

## 1. 변경 파일
- `migrations/013_f07_incident_report.sql`: `walk_incidents` 테이블에 `client_report_id`, `acknowledged_at`, `acknowledged_by` 컬럼 추가, 유형 7종 체크 제약(`ck_incident_type`), 확인 쌍 제약(`ck_incident_acknowledgement`), 유니크 부분 인덱스 및 미확인 조회 인덱스 추가
- `app/models.py`: `User.walk_incidents`에 `foreign_keys="WalkIncident.reported_by"` 관계 명시
- `app/walk_models.py`: `WalkIncident` 모델에 신규 컬럼 3종, 제약조건, 부분 인덱스 2종 및 관계(`reporter`, `acknowledger`) 반영
- `docs/TODO.md`: F-07 작업 목록 추가 및 마이그레이션/모델 항목 완료 체크
- `docs/handoff/LATEST.md`: F-07 인수인계 문서 작성

## 2. 결정 사항
- 마이그레이션 번호: `migrations/013_f07_incident_report.sql`로 확정 (F-06의 012에 이은 신규 번호).
- 신고 유형: 기존 4종(`INJURY`, `LOST_DOG`, `SAFETY_THREAT`, `OTHER`)에서 3종(`POOP_NO_BAG`, `ATE_FOREIGN_OBJECT`, `ABNORMAL_BEHAVIOR`)을 추가하여 총 7종으로 확장.
- 확인 쌍 제약: `acknowledged_at`과 `acknowledged_by`는 둘 다 NULL이거나 둘 다 NOT NULL이어야 함.
- 부분 인덱스: SQLite와 PostgreSQL 호환을 위해 `postgresql_where`와 `sqlite_where` 동시 지정.

## 3. 실행한 테스트 명령과 결과
- (2단계 모델/마이그레이션 추가 완료, 3~5단계 구현 및 테스트 예정)

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 신고 5종 저장, status OPEN, 상세 공백 시 한글 라벨 자동 저장 | `test_report_incident_success_and_default_korean_label` | (대기) |
| 필수 유형 누락 및 미지원 유형 422 | `test_invalid_incident_type_returns_422` | (대기) |
| 보호소 알림 조회 (미확인 목록, after_id 지원, 보호소 격리) | `test_shelter_incident_alerts_polling` | (대기) |
| 타 보호소 알림 격리 및 일반 사용자/관리자 접근 제한 (403) | `test_incident_alerts_authorization` | (대기) |
| 알림 확인 처리 멱등성 및 이력 보존 | `test_acknowledge_incident_alert_idempotent` | (대기) |
| 3분 이내 동일 유형 중복 시 409 및 confirm_duplicate 재전송 시 201 | `test_duplicate_incident_window_and_confirmation` | (대기) |
| client_report_id 멱등 재전송 200 반환 및 상이 내용 409 | `test_client_report_id_idempotency` | (대기) |
| 종료 24시간 이내 산책 신고 201 및 초과/CANCELLED 409 | `test_late_report_for_ended_walk` | (대기) |
| 좌표 누락 시 산책 마지막 위치 대체 및 둘 다 없을 시 NULL | `test_fallback_to_walk_last_coordinates` | (대기) |
| 보호소 신고 이력 필터링 및 관리자 shelter_id 지정 조회 | `test_shelter_incidents_history_filters` | (대기) |
| 동일 산책 동일 유형 동시 신고 레이스 컨디션 방어 | `test_concurrent_incident_race_condition` | (대기) |
| 회귀 검증: 산책 권한, GET /walks/{id} incidents 조회, F-06 통과 | `test_regression_rules` | (대기) |

## 5. 로컬 실행 준비
1. PostgreSQL 마이그레이션 실행:
   ```bash
   psql -d <DB_NAME> -f migrations/013_f07_incident_report.sql
   ```

## 6. 확인하지 못한 것
- (구현 진행 중)

## 7. 다음 할 일 및 주의사항
- 다음 단계: 신고 접수 스키마 확장(`WalkIncidentCreate`, `WalkIncidentResponse`) 및 `app/walk_service.py`의 `report_incident` 로직 확장.
