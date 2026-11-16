# 인수인계: F-06 산책 중 지도·경로 기록 (SC-07)

- 날짜: 2026-11-16 (KST)
- 브랜치: `feature/f-06-walk-tracking`
- 작업 요약: 산책 중 실시간 경로 좌표 저장, 약신호 좌표 저장 허용, 세션 만료 자동 종료 및 앱 비정상 종료 시 진행 중 산책 복원 API 구현.

## 1. 변경 파일
- `migrations/012_f06_walk_tracking.sql`: `walks` 테이블에 마지막 위치(위도, 경도, 정확도, 기록시각, 수신시각) 및 종료 사유(`end_reason`) 컬럼과 제약조건(`ck_walk_last_coordinate_pair`, `ck_walk_last_latitude`, `ck_walk_last_longitude`, `ck_walk_last_accuracy`, `ck_walk_end_reason`) 추가
- `app/models.py`: `DogWalkDateSlot`의 `Date, Time` 누락 import 보완
- `app/routers/dogs.py`: F-03 `File, Form, UploadFile, save_dog_image` 누락 import 보완
- `app/walk_models.py`: `Walk` 모델에 신규 컬럼 6개 및 제약조건 추가
- `app/walk_record_schemas.py`: `WalkResponse`에 신규 필드 추가 및 `ActiveWalkResponse` 정의
- `app/walk_service.py`: 정확도 50m 초과 좌표 409 거부 제거 및 저장 허용, `last_*` 위치/수신시각 갱신 로직 추가, 수동/자동 종료 공통 통계 함수(`finalize_walk_statistics`), `close_stale_walks`, `get_active_walk` 구현, 만료 검사 기준 초과(`>`) 조건 적용
- `app/routers/walks.py`: `GET /walks/active` 복원 라우트 신설 (기존 `/{walk_id}`보다 상단 배치), 수동 종료 시 `end_reason`(`USER`/`STAFF`) 기록
- `app/main.py`: FastAPI `lifespan` 기반 백그라운드 주기 만료 점검(`periodic_stale_walks_check`) 루프 등록
- `.env.example`: `AUTO_END_AFTER_SECONDS=3600`, `AUTO_END_CHECK_INTERVAL_SECONDS=300` 추가
- `tests/test_walk_recording.py`: 51m 정확도 좌표 409 거부 테스트를 200 저장 성공 검증으로 수정 (line 130)
- `tests/test_f06_walk_tracking.py`: F-06 완료 기준 10개 항목에 대한 단위/통합 테스트 신설
- `docs/TODO.md`: F-03, F-04, F-05, F-06 작업 목록 통합 및 완료 체크
- `docs/handoff/LATEST.md`: F-06 구현 및 검증 결과 인수인계 문서 작성

## 2. 결정 사항
- 마이그레이션 번호: `migrations/012_f06_walk_tracking.sql`로 지정 (F-04의 010, F-05의 011과 충돌 방지).
- `scripts/walk_schema.py` 및 `007_walk_domain.schema.json`은 007 과거 검증용 불변 계약이므로 갱신하지 않음.
- 정확도 50m 초과 좌표는 저장하되, 이동 거리(`total_distance`) 계산 시 신뢰도 필터(`accuracy_m <= 50m`)로 제외 유지.
- 묶음 내 신규 좌표가 있는 경우에만 `last_point_received_at`을 서버 현재 시각으로 갱신 (중복 좌표만 있는 묶음은 갱신 제외).
- 만료 기준: `now - coalesce(last_point_received_at, started_at) > AUTO_END_AFTER_SECONDS` (기본 3600초). 표준 1시간 예약 슬롯 경계와 레이스 컨디션을 방지하기 위해 초과(`>`) 조건 사용.
- 만료 시각이 지난 산책에 좌표 전송 시 409 Conflict ("산책 세션이 만료되었습니다.") 반환.
- 만료 자동 종료 시 `status='COMPLETED'`, `ended_at=coalesce(last_point_received_at, started_at)`, `end_reason='AUTO_TIMEOUT'`으로 기록하며 통계 계산 및 봉사 이력(`refresh_volunteer_profile`) 갱신 수행.
- 수동 종료 시 봉사자 직접 종료는 `USER`, 보호소/관리자 안전 중단은 `STAFF`로 `end_reason` 기록.
- `GET /walks/active`는 봉사자 본인의 진행 중 산책 정보(`walk_id`, 시작시각, 마지막 위치, `last_sequence_no`, 경과 초)를 반환하며, 없는 경우 404 응답. 조회 시점 및 `start_walk` 시점에도 해당 사용자의 만료 산책 자동 정리 수행.

## 3. 실행한 테스트 명령과 결과
```powershell
$env:PYTHONPATH="."
pytest tests/test_f06_walk_tracking.py tests/test_f05_walk_approval.py tests/test_walk_recording.py tests/test_walk_requests.py -v
```

### 테스트 결과 요약
- `tests/test_f06_walk_tracking.py`: 10 passed (100%)
- `tests/test_f05_walk_approval.py`: 11 passed (100%)
- `tests/test_walk_recording.py`: 39 passed (100%)
- `tests/test_walk_requests.py`: 32 passed (100%)
- **총합: 92 passed in ~12s (전체 성공)**

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 좌표 묶음 저장 후 순서대로 경로 조회 | `test_path_retrieval_in_order` | 통과 (PASSED) |
| 시간 공백 후 이어서 전송 시 last_* 정상 갱신 | `test_batch_continuation_with_gap` | 통과 (PASSED) |
| 50m 초과 좌표 정상 저장 및 거리 계산 제외 | `test_accuracy_over_50m_accepted_and_excluded_from_distance` | 통과 (PASSED) |
| 추가 좌표 미전송 시 마지막 위치 및 수신시각 보존 | `test_last_position_and_received_at_retained` | 통과 (PASSED) |
| GET /walks/active 복원 정보 반환 및 다음 순번 전송 성공 | `test_active_walk_restoration_and_next_sequence` | 통과 (PASSED) |
| 1시간 경과 산책 점검 시 AUTO_TIMEOUT 완료 및 통계 반영 | `test_auto_end_stale_walk_after_1_hour` | 통과 (PASSED) |
| 1시간 미만 시 미종료 및 좌표 없는 산책 시작시각 기준 만료 | `test_not_expired_under_1_hour_and_no_points_start_basis` | 통과 (PASSED) |
| 만료 점검 중복 실행 멱등성 (idempotent) | `test_close_stale_walks_idempotency` | 통과 (PASSED) |
| 자동 종료 산책 좌표 전송 시 409 및 수동/안전중단 사유 기록 | `test_send_points_to_auto_ended_walk_409_and_reasons` | 통과 (PASSED) |
| 회귀 검증: 예약시간대 시작, 동시 진행 1개 제한, 권한 검사 | `test_regression_guard_rules` | 통과 (PASSED) |

## 5. 로컬 실행 준비
1. PostgreSQL 마이그레이션 실행:
   ```bash
   psql -d <DB_NAME> -f migrations/011_f05_walk_approval.sql
   psql -d <DB_NAME> -f migrations/012_f06_walk_tracking.sql
   ```
2. 환경 변수 확인 (`.env`):
   ```env
   AUTO_END_AFTER_SECONDS=3600
   AUTO_END_CHECK_INTERVAL_SECONDS=300
   ```

## 6. 확인하지 못한 것
- **Windows Sandbox 환경 내 TestClient 실행 제약**: Windows 격리 샌드박스 내부에서는 `asyncio` 기반 self-pipe socketpair가 차단되어 `TestClient` 통신이 대기 상태에 머무릅니다. 따라서 테스트 실행은 샌드박스 네트워크 격리가 해제된 PowerShell 환경(`BypassSandbox: true` / 로컬 개발 터미널)에서 검증을 완료하였습니다.
- **실제 PostgreSQL 통합 테스트(`tests/test_walk_recording_postgres.py`)**: 구동 중인 외부 PostgreSQL 인스턴스 접속 정보(`DATABASE_URL`)가 필요한 테스트로, 현재 로컬 SQLite 인메모리 테스트로 전체 로직을 검증하였으며 실 PostgreSQL 환경에서의 마이그레이션 DDL 실제 실행은 배포 환경 파이프라인에서 확인이 필요합니다.

## 7. 다음 할 일 및 주의사항
- 본 머지 및 충돌 해결 완료 후 `feature/f-06-walk-tracking` 브랜치를 원격에 push하고 `dev` 대상 Pull Request(PR) 생성/확인.
- 프론트엔드 연동 참고사항:
  - 산책 진행 중 `POST /walks/{id}/points`를 주기적으로 호출하여 좌표 업로드.
  - 앱이 꺼지거나 비정상 종료된 경우, 재기동 시 `GET /walks/active`를 호출해 진행 중 산책 여부를 확인하고, 반환된 `last_sequence_no`의 다음 순번(`last_sequence_no + 1`)부터 좌표를 이어 전송.
  - 만약 409 ("산책 세션이 만료되었습니다.") 에러를 받으면 세션이 타임아웃 종료되었으므로 산책 UI를 종료하고 메인 화면으로 전환.
