# PR #15 수정 및 Issue #5 통합 검증

대상: `ToI-walkwithdog/walkwithdog-BE` PR #15, `feature/map-volunteer-history` → `dev`.

## 기준과 충돌 해결

- 작업 전 origin/dev: `833e4bb953da4e67cc6b8a6888e0cd0d6396be7a` (Issue #5 PR #16 병합).
- 작업 전 origin/feature/map-volunteer-history: `adf3eb7feaabaf8b0c0f3217f25782248bd20254`.
- 시작 시 작업 트리는 깨끗했습니다. 최신 origin/dev를 feature에 병합했습니다. rebase/force push는 사용하지 않습니다.
- 충돌 파일: `app/walk_service.py`, `tests/test_walk_recording.py`.
- 서비스는 원본 WalkPoint → 기존 거리/Issue #5 analyze_motion → 지표 저장 → 종료 상태 → COMPLETED만 봉사 통계 재집계 순서로 합쳤습니다. 라우터의 기존 단일 commit/rollback 경계를 유지합니다.
- 테스트는 이동 페이스 899.28초/km와 완료 통계(거리 22.24m, 전체 시간 120초) 양쪽 검증을 보존했습니다.
- GPS 분석 모듈, geometry, 설정, 모델, 마이그레이션은 origin/dev의 Issue #5 구현과 동일합니다. 기존 001~008 마이그레이션을 수정하지 않았습니다.

## PR 코멘트 항목별 반영

| 코멘트 | 반영 결과 |
| --- | --- |
| Issue #5 GPS 분석 연동 | 이동·정지·UNKNOWN 및 이동 거리 유지. 페이스는 이동 초 ×1000 / 이동 거리. 개인 지도·산책 이력에도 nullable 지표 제공. |
| 지도 GPS 처리 | 개인 지도 시작/끝 원본 보존 및 기존 간소화 유지. 공개 지도는 150m 절단·소수 3자리 정책 유지, 출발/도착 주변 재진입과 반올림/연결선 노출 보완. |
| 레벨 시스템 제거 | level_for/승급 상수/자동 level 변경/난이도별 레벨 제한 제거. DB 컬럼과 응답 level 및 기존 값 보존. 보호소 승인·권한·예약·설정·시작 재검증 유지. |
| 기존 완료 산책 통계 | 기본 dry-run인 멱등 백필 스크립트 추가. COMPLETED만 집계. 기존 level 및 GPS 지표 변경 없음. 사용자별 잠금으로 종료와 직렬화. |

## 발견한 문제 및 선택

기존 공개 지도는 처음/마지막 150m를 **경로 길이**로만 잘라, 이후 출발점 주변을 재통과하는 좌표를 반환할 수 있었습니다. 좌표 반올림도 보호 반경 안으로 좌표를 이동시킬 수 있었습니다.

공개용 간소화 경로의 실제 시작/끝을 기준으로 반올림 후에도 반경 150m 바깥인 좌표만 후보로 삼습니다. 경로 내부가 보호 영역으로 재진입하면 전체 route를 빈 배열로 반환합니다. 조각을 임의로 이어 붙이지 않습니다. 반환 좌표의 연결선도 삼각부등식으로 얻는 보수적 거리 하한을 검사하고 안전성을 확인할 수 없으면 경로를 숨깁니다. 이 때문에 긴/굽은 경로도 일부 숨겨질 수 있습니다. 짧은 경로는 계속 숨기고 통계만 유지합니다.

공개 보호소 위치 사용에 대한 명시적 요구가 없으므로 공개 시작·종료 마커는 추가하지 않았습니다. 원본 GPS를 변경·삭제하지 않으며 지도 좌표는 분석에 사용하지 않습니다. 거리 함수는 `app.gps_geometry.distance_m`을 직접 공유합니다.

## API 호환성

기존 URL과 응답 필드를 유지합니다:

- `GET /users/{user_id}/map`: 본인만. 새 시간 3개·moving_distance_m·average_pace 추가.
- `GET /users/{user_id}/walks`: 본인만. 새 시간 3개·moving_distance_m 추가. 기존 average_pace 유지.
- `GET /users/{user_id}/profile`, `GET /volunteers/me`: 기존 level 응답과 통계 필드 유지.
- `GET /dogs/{dog_id}/map`: 로그인 사용자용 비식별 응답 유지. 봉사자/산책 식별자 및 실제 시각 비노출.

신규 이동 지표는 레거시 기록에서 null이며 GET은 재계산하지 않습니다. 신규 average_pace는 실제 이동 기준 초/km, 새 지표가 null인 레거시 average_pace는 기존 전체 시간 기준 값입니다. 이 차이를 OpenAPI에 명시했습니다.

봉사 통계의 `total_duration_seconds`는 COMPLETED의 **전체 duration_seconds 합계**입니다. 이동 시간으로 바꾸지 않았습니다. level은 기존 호환용 값이며 자동 승급하거나 자격 판단에 사용하지 않습니다. BEGINNER/EXPERIENCED 정보 자체는 유지합니다.

## 백필 운영 방법

```sh
python -m scripts.backfill_volunteer_stats
python -m scripts.backfill_volunteer_stats --user-id 123
python -m scripts.backfill_volunteer_stats --apply
python -m scripts.backfill_volunteer_stats --apply --user-id 123
```

기본은 읽기 전용 계획이며 `--apply`에서만 변경합니다. 실제 대상은 설정된 DATABASE_URL이므로 실행 전 대상 확인이 필요합니다. **작업 중 개발/운영 DB에서 백필을 실행하지 않았습니다.** 격리 테스트에서만 dry-run/apply를 실행했습니다.

- 대상: COMPLETED 소유자와 기존 volunteer_profiles. 기록이 없는 기존 프로필의 잘못된 통계도 0으로 바로잡습니다. level은 유지합니다.
- COMPLETED만 거리·전체 시간·횟수·서로 다른 강아지 수를 다시 계산하여 대입합니다. INTERRUPTED/CANCELLED/IN_PROGRESS 제외. 누적 증가 방식이 아니므로 여러 번 실행해도 같은 결과입니다.
- 사용자마다 한 트랜잭션. User → Profile의 NOWAIT 잠금. 실시간 종료가 이미 User를 잠갔으면 백필은 해당 사용자 rollback 후 중단하고, 반대 경합에서는 종료 API가 409 후 재시도할 수 있습니다.
- 실패 전 사용자들의 적용은 이미 완료되었을 수 있습니다. dry-run/상태 확인 후 재실행하면 안전하게 이어집니다. dry-run은 잠금을 잡지 않는 참고 스냅샷입니다.
- level 및 Walk/GPS/기타 프로필 필드를 임의로 갱신하지 않습니다. 통계 변경 시 updated_at은 기존 ORM 정책을 따릅니다.
- 스키마 변경·초기화·삭제 없음. Issue #5의 008이 적용된 DB에서 새 코드를 실행합니다.

## 검증

Python 3.12.14 / PostgreSQL 18.4. localhost:55476의 작업 전용 클러스터에서 실행마다 새 빈 `toi_walk_test_*` DB 6개를 생성했습니다. 개발/운영 DB에는 접속·마이그레이션·초기화를 수행하지 않았습니다.

| 항목 | 결과 |
| --- | --- |
| 전체 회귀 및 신규 테스트 | PASS: 396개, 성공 396, 실패/오류 0, skip 0 |
| 실제 PostgreSQL | PASS: 82개 (전체 396개에 포함) |
| 일반/SQLite/순수 함수 | PASS: 314개 |
| GPS 시간 분할·이동 페이스·900초/km 사례·레거시 null | PASS |
| 통계 실패 시 GPS 지표·종료·프로필 전체 rollback | PASS |
| 개인 권한·시작/끝 보존·간소화·공개 비식별화 | PASS |
| 짧은/반복 경로·반올림·공개 연결선 보호 | PASS |
| 완료만 집계·중복 종료·동일 사용자 서로 다른 산책 종료 경합 | PASS |
| 백필 dry-run·멱등성·실패 rollback·기존 level 보존 | PASS |
| 백필 우선/종료 우선 양방향 PostgreSQL 경합 및 재시도 | PASS |
| 레벨 무관 신청·승인·시작, 보호소 승인·기타 안전 정책 | PASS |
| 008 적용·제약조건·기존 데이터 보존 회귀 | PASS |
| Python compileall / git diff --check | PASS |

실행:

```sh
# 새 빈 테스트 DB 6개와 아래 opt-in 변수를 설정한 뒤
DATABASE_URL=sqlite:// python -m unittest discover -s tests -q
python -m compileall -q app scripts tests
git diff --check
```

PostgreSQL opt-in 변수: WALK_TEST_DATABASE_URL, WALK_SETTINGS_TEST_DATABASE_URL, WALK_REQUESTS_TEST_DATABASE_URL, WALK_RECORDS_TEST_DATABASE_URL, GPS_MOTION_TEST_DATABASE_URL, WALK_MAPS_TEST_DATABASE_URL. pg_dump/pg_restore가 PATH에 있어야 마이그레이션 백업 테스트도 실행됩니다. 변수가 없어 skip된 실행은 PostgreSQL 검증 성공으로 계산하지 않았습니다.

초기 일반 실행에서 이전 기획의 레벨 기반 난이도 거절을 기대하던 테스트 1개가 실패했습니다. 해당 기대를 제거하고 산책 가능·운영 시간·계정 역할·보호소 승인 검증을 유지했으며, 이후 전체 실행은 통과했습니다.

## 남은 사항

실측 GPS 보정과 대규모 부하 측정은 이 PR 범위 밖입니다. 공개 지도는 보수적으로 숨겨지는 경로가 늘 수 있습니다. 실데이터 백필은 배포 담당자가 대상 DB와 dry-run 결과를 확인한 뒤 별도로 수행해야 합니다.

필수 리뷰/체크/브랜치 정책은 Push 후 최신 HEAD에서 다시 확인하며, 검증된 HEAD만 일반 merge 방식으로 PR #15를 병합합니다. 강제 Push·보호 정책 변경·우회·다른 브랜치 삭제는 하지 않습니다. 최종 commit/push/merge SHA는 작업 완료 응답에 실제 결과로 보고합니다.

## 최종 PR 변경 파일 (origin/dev 대비)

- `PR15_INTEGRATION.md`
- `app/main.py`
- `app/routers/walk_maps.py`
- `app/routers/walks.py`
- `app/volunteer_stats.py`
- `app/walk_map_schemas.py`
- `app/walk_map_service.py`
- `app/walk_request_schemas.py`
- `app/walk_request_service.py`
- `app/walk_service.py`
- `scripts/backfill_volunteer_stats.py`
- `tests/test_walk_maps.py`
- `tests/test_walk_maps_postgres.py`
- `tests/test_walk_recording.py`
- `tests/test_walk_requests.py`
- `tests/test_walk_requests_postgres.py`
- `tests/walk_map_integration_support.py`
- `tests/walk_map_test_support.py`
