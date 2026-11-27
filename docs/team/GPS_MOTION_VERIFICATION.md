# Issue #5 구현 및 검증 보고

## 1. 기준과 Git

- 기준 `dev` / `origin/dev` / 작업 시작 HEAD: `ea0c4dbecebf91cdd9143083d45c894e222cec43`.
- 작업 브랜치: **`feature/gps-motion-analysis`**. 최신 `origin/dev`에서 생성했습니다.
- 작업 시작 시 미커밋 변경은 없었습니다. 기존 Issue #1~#4 구현을 보존했습니다.
- 사용자의 최신 지시에 따라 **Git commit, push, PR 생성, merge는 실행하지 않았습니다**. 변경은 모두 미커밋 상태입니다.

## 2. 변경 파일

신규 10개:

- `app/gps_geometry.py`: 기존 Haversine 거리 함수 공통화.
- `app/gps_motion_config.py`: V1 정책과 추가 연속성·방향성 보호 상수.
- `app/gps_motion_analysis.py`: DB에 의존하지 않는 GPS 구간 재생·집계·페이스.
- `migrations/008_gps_motion_analysis.sql`: nullable 컬럼 4개, 제약조건 2개.
- `migrations/008_gps_motion_analysis.schema.json`: PostgreSQL catalog 검증 계약.
- `tests/test_gps_motion_analysis.py`: 순수 분석 테스트 34개.
- `tests/test_gps_motion_postgres.py`: 실제 007→008 마이그레이션 테스트 7개.
- `tests/walk_motion_test_support.py`: SQLite/PostgreSQL에 공통 적용하는 API 테스트 8개.
- `GPS_MOTION_ANALYSIS.md`: 계산 정책, API 명세, 배포 순서, Issue #6 전달사항.
- `GPS_MOTION_VERIFICATION.md`: 이 보고서.

수정 9개:

- `app/walk_service.py`: 기존 종료 트랜잭션에 순수 분석 연결.
- `app/walk_models.py`: 새 지표와 정합성 제약조건.
- `app/walk_record_schemas.py`: 새 응답 필드 및 OpenAPI 설명.
- `app/routers/walks.py`: 종료·조회 Swagger 설명.
- `scripts/migrate_backend.py`: 백업·잠금·검증을 유지하는 008 지원.
- `scripts/walk_schema.py`: 007 원형을 보존하며 검증된 008 확장만 허용.
- `tests/test_walk_domain.py`: 기존 007 DDL 원형 및 확장 ORM 검증.
- `tests/test_walk_recording.py`: 새 API 테스트 연결, 변경된 페이스 기대값.
- `tests/test_walk_recording_postgres.py`: 동일 API 테스트를 PostgreSQL에서도 실행, 새 페이스 기대값.

기존 마이그레이션 **001~007 SQL 및 007 JSON 변경 없음**.

## 3. 알고리즘 및 시간 집계

정확도 20m 이내·인접 기록 공백 15초 이내·연속 순번의 GPS 구간을 사용합니다. 기존 업로드 정확도 50m, 최대 속도 5m/s, 기존 거리 공백 300초 정책은 유지했습니다. 최신 dev 정책과 요청에 적힌 이 수치들의 차이는 없었습니다.

- 속도 <0.5m/s 및 최초 위치 기준 반경 ≤15m의 연속 정지 조건을 축적합니다. 20초부터 STOP_CANDIDATE, 총 60초부터 STOPPED입니다.
- 정지 확정 시 후보 시작 시점으로 소급하여 한 번만 집계합니다. 미확정 후보는 UNKNOWN입니다.
- 이동은 >0.8m/s 연속 2개 이상 구간, 변위 >20m, 순변위/경로 길이 ≥0.5로 확인하고 해당 이동 후보를 소급 집계합니다. 왕복 단발 튐은 이동 근거에서 제외합니다.
- 0.5~0.8m/s는 이전 MOVING을 유지하거나 확정 정지 반경 안의 STOPPED를 유지하며, 근거가 불명확하면 UNKNOWN입니다.
- 좌표 없음/하나, 품질 불량, 공백, 순번 단절, 시각 중복, 확인 전·후 구간은 UNKNOWN입니다. UNKNOWN을 정지로 간주하지 않습니다.
- 정수 마이크로초로 각 구간을 한 번만 분류한 뒤 이동·정지 초를 각각 절삭하고, UNKNOWN에 잔여 초를 배정합니다. `duration = moving + stopped + unknown`을 보존합니다.

자세한 경계 처리와 구현 선택은 `GPS_MOTION_ANALYSIS.md`에 기록했습니다.

## 4. 평균 이동 페이스 및 거리

`average_pace = moving_duration_seconds × 1000 / moving_distance_m` (초/km).

새 `moving_distance_m`는 확인된 이동 구간만 포함합니다. 기존 `total_distance_m`는 API 호환성을 위해 원래 의미로 유지합니다. 정지 흔들림은 기존 거리에는 남을 수 있지만 이동 페이스의 분모에는 들어가지 않습니다. Decimal 소수 둘째 자리 ROUND_HALF_UP을 유지합니다. 이동 시간/거리 0 또는 표현 가능한 페이스 범위 초과 시 null입니다.

실제 Haversine 거리로 2400초 산책 = 이동 1800초 + 정지 600초, 이동 2000m → **900.00초/km**를 검증했습니다.

## 5. DB 변경

- Integer nullable: `moving_duration_seconds`, `stopped_duration_seconds`, `unknown_duration_seconds`.
- Numeric(12,2) nullable: `moving_distance_m`.
- 기본값·기존 행 backfill 없음. 기존 시간·거리·페이스·timestamp는 그대로 보존합니다.
- 새 4개 값의 부분 입력, 음수, 시간 합계 불일치, 이동 거리 > 기존 거리, 이동 근거 없는 non-null 페이스를 거절합니다. 기존 pace 제약조건은 유지했습니다.
- 기존 데이터 보존, 백업, 재적용, 구조 drift 거절, SQL 실패 rollback, 호환 가능한 부분 추가 완료를 PostgreSQL에서 검증했습니다.
- calculation_version DB 컬럼이나 자동 과거 재계산은 추가하지 않았습니다. V1 코드 상수로 정책을 식별하며 향후 정책 변경/재계산 전에 별도 버전 보존 정책이 필요합니다.

## 6. API / Issue #6 공유사항

요청과 URL은 그대로입니다. 시작·종료 응답 및 상세의 `walk`에 새 4개 지표가 추가됩니다. 평균 페이스는 신규 종료 기록부터 이동 시간/거리 기준입니다. 새 필드가 null인 레거시 기록은 기존 전체 시간 페이스이므로 FE에서 구분해야 합니다. 시간은 정수, 거리는 기존 Decimal 문자열 형식이며 페이스도 문자열 또는 null입니다.

FE는 마지막 미전송 GPS까지 저장 성공을 확인한 **뒤에 종료**해야 합니다. 종료 후 좌표 저장은 기존대로 409입니다. 마커·신고 POST는 재시도 시 별도 이벤트가 생기므로 응답 유실 시 상세 조회로 확인해야 합니다. 새 실시간 이벤트·상태 API나 FE 기능은 없습니다.

## 7. 검증 결과

| 항목 | 결과 | 근거 |
| --- | --- | --- |
| GPS 상태·경계·시간 분할·Decimal 페이스 | PASS | 순수 함수 34개 |
| 종료/상세/시작 응답, 레거시 null, 품질 분리, 정지 흔들림 | PASS | 공통 API 8개 × SQLite/PostgreSQL = 16개 |
| 008 실제 마이그레이션·데이터 보존·제약조건·rollback | PASS | PostgreSQL 7개 |
| Issue #1~#4 전체 회귀 | PASS | 기존 293개 모두 실행; 새 페이스 의미에 맞춘 기존 기대값 2곳 수정 |
| 실제 PostgreSQL 통합 전체 | PASS | 기존 51개 + 신규 API 8개 + 신규 마이그레이션 7개 = 66개 |
| 권한·중복/충돌·잠금·동시성 회귀 | PASS | 기존 실제 PostgreSQL 시작/종료/전송/취소/설정 OFF 경합 테스트 포함 |
| 최종 전체 테스트 | PASS | **350개, 실패 0, 오류 0, skip 0** (최종 실행 16.206초) |
| Python 컴파일 | PASS | `python -m compileall -q app scripts tests` |
| Git 공백 오류 | PASS | `git diff --check` |
| 기존 001~007 미변경 | PASS | 기준 dev와 해당 파일 diff 없음 |
| Git commit / push / PR / merge | 미실행 | 사용자 지시 준수 |

기준 코드의 사전 실행은 293개 중 일반 테스트 242개 통과, PostgreSQL opt-in 51개 skip이었습니다. 최종 실행에서는 별도로 준비한 실제 PostgreSQL에서 350개를 전부 실행했습니다.

첫 통합 실행에서 테스트 fixture의 미커밋 ALTER와 외부 pg_dump가 서로 대기해 1건이 실패했습니다. 부분 추가 SQL 테스트에서 불필요한 백업 호출을 제거해 테스트 자체의 잠금 충돌을 해결했습니다. 별도의 커밋된 007 데이터로 검증하는 실제 백업/마이그레이션 테스트는 유지했습니다. 수정 후 전체가 통과했습니다.

## 8. 실행 환경과 재현

- Python 3.12.14, PostgreSQL 18.4, 기존 프로젝트 venv 및 의존성 사용.
- `/private/tmp/toi-motion-pg.RrYQF8`에 만든 작업 전용 클러스터, `127.0.0.1:55476` 사용.
- 일반 개발/운영 DB를 초기화하거나 이행하지 않았습니다. 매 실행마다 `toi_walk_test_motion_<새 식별자>_*` 이름의 빈 DB 5개를 생성했습니다. DB/table drop, truncate로 초기화하지 않았습니다.
- 전체 실행: `/Users/ijaein/Documents/ToI/venv/bin/python /private/tmp/toi_motion_check.py`. 이 임시 래퍼는 DB 5개와 다음 opt-in 변수를 준비하고 `python -m unittest discover -s tests -q`를 실행합니다.

```text
DATABASE_URL=sqlite://
WALK_TEST_DATABASE_URL=<새 빈 로컬 toi_walk_test_* DB>
WALK_SETTINGS_TEST_DATABASE_URL=<다른 새 빈 DB>
WALK_REQUESTS_TEST_DATABASE_URL=<다른 새 빈 DB>
WALK_RECORDS_TEST_DATABASE_URL=<다른 새 빈 DB>
GPS_MOTION_TEST_DATABASE_URL=<다른 새 빈 DB>
PATH=/opt/homebrew/opt/postgresql@18/bin:$PATH
```

미설정 상태의 일반 실행은 PostgreSQL 테스트를 skip하므로 전체 PostgreSQL 검증으로 간주하면 안 됩니다. 새 마이그레이션 테스트와 기존 통합 테스트는 비어 있지 않은 DB/허용하지 않은 DB 이름을 거절합니다.

## 9. 남은 위험 및 병합 판단

**현재 구현 범위에서 테스트 실패나 확인된 미해결 결함은 없습니다. 병합 검토 가능한 상태입니다.**

실측 GPS 정답 데이터 기반 보정과 대량 기록 부하 측정은 하지 않았습니다. 매우 느린 이동과 작은 반경 내 정지는 GPS만으로 구분하기 어렵고, 품질 불량이나 긴 수집 간격에서는 UNKNOWN 또는 null 페이스가 늘어납니다. 정지 반경 안의 출발 구간을 보수적으로 UNKNOWN에 남길 수 있습니다. 이러한 선택과 경계를 API 문서에 공개했습니다.

배포 시 008을 먼저 적용해야 합니다. 레거시 페이스와 새 이동 페이스의 구분, nullable 처리 및 `moving_distance_m` 필드에 대한 Issue #6 담당자의 연동 검토가 필요합니다. 코드 병합이나 배포는 실행하지 않았습니다.
