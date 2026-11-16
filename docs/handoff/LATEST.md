# 인수인계: F-06 산책 중 지도·경로 기록 (SC-07)

- 날짜: 2026-11-16 (KST)
- 브랜치: feature/f-06-walk-tracking
- 작업 요약: 산책 중 실시간 경로 좌표 저장, 약신호 좌표 저장 허용, 세션 만료 자동 종료 및 앱 비정상 종료 시 진행 중 산책 복원 API 구현.

## 1. 변경 파일
- `migrations/012_f06_walk_tracking.sql`: `walks` 테이블에 마지막 위치(위도, 경도, 정확도, 기록시각, 수신시각) 및 종료 사유(`end_reason`) 컬럼과 제약조건 추가
- `app/models.py`: `DogWalkDateSlot`의 `Date, Time` 누락 import 보완
- `app/routers/dogs.py`: F-03 `File, Form, UploadFile, save_dog_image` 누락 import 보완
- `app/walk_models.py`: `Walk` 모델에 신규 컬럼 6개 및 제약조건 추가
- `app/walk_record_schemas.py`: `WalkResponse`에 신규 필드 추가 및 `ActiveWalkResponse` 정의
- `app/walk_service.py`: 정확도 50m 초과 좌표 409 거부 제거 및 저장 허용, `last_*` 위치/수신시각 갱신 로직 추가
- `docs/TODO.md`, `docs/handoff/LATEST.md`: F-06 작업 목록 및 인수인계 문서 갱신

## 2. 결정 사항
- 마이그레이션 번호: `migrations/012_f06_walk_tracking.sql`로 지정 (F-04의 010, F-05의 011과 충돌 방지).
- `scripts/walk_schema.py` 및 `007_walk_domain.schema.json`은 007 과거 검증용 불변 계약이므로 갱신하지 않음.
- 정확도 50m 초과 좌표는 저장하되, 이동 거리(`total_distance`) 계산 시 신뢰도 필터(accuracy <= 50m)로 제외 유지.
- 묶음 내 신규 좌표가 있는 경우에만 `last_point_received_at`을 서버 현재 시각으로 갱신 (중복 좌표만 있는 묶음은 갱신 제외).
- 만료 시각이 지난 산책에 좌표 전송 시 409("산책 세션이 만료되었습니다.") 반환.

## 3. 실행한 테스트 명령과 결과
- (구현 진행 중)

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 좌표 묶음 저장 후 순서대로 경로 조회 | `test_path_retrieval_in_order` | - |
| 시간 공백 후 이어서 전송 시 last_* 정상 갱신 | `test_batch_continuation_with_gap` | - |
| 50m 초과 좌표 정상 저장 및 거리 계산 제외 | `test_accuracy_over_50m_accepted_and_excluded_from_distance` | - |
| 추가 좌표 미전송 시 마지막 위치 및 수신시각 보존 | `test_last_position_and_received_at_retained` | - |
| GET /walks/active 복원 정보 반환 및 다음 순번 전송 성공 | `test_active_walk_restoration_and_next_sequence` | - |
| 1시간 경과 산책 점검 시 AUTO_TIMEOUT 완료 및 통계 반영 | `test_auto_end_stale_walk_after_1_hour` | - |
| 1시간 미만 시 미종료 및 좌표 없는 산책 시작시각 기준 만료 | `test_not_expired_under_1_hour_and_no_points_start_basis` | - |
| 만료 점검 중복 실행 멱등성 (idempotent) | `test_close_stale_walks_idempotency` | - |
| 자동 종료 산책 좌표 전송 시 409 및 수동/안전중단 사유 기록 | `test_send_points_to_auto_ended_walk_409_and_reasons` | - |
| 회귀 검증: 예약시간대 시작, 동시 진행 1개 제한, 권한 검사 | `test_regression_guard_rules` | - |

## 5. 로컬 실행 준비
- PostgreSQL 마이그레이션 실행: `migrations/012_f06_walk_tracking.sql`

## 6. 확인하지 못한 것
- (구현 진행 중)

## 7. 다음 할 일 및 주의사항
- 다음 단계: 좌표 저장 변경(정확도 50m 초과 허용, `last_*` 갱신) 및 스키마 반영
