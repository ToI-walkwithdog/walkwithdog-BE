# PostgreSQL 마이그레이션 관리

## 기준과 실행 경로

[manifest](../../migrations/manifest.json)는 매 기능 PR과 함께 갱신하는 관리 파일이다. 처음 등록한 SQL 21개는 `origin/dev`의 `0936933005c895947e256e735e9c4c9615c6c10f` 기준이다. `migrations` 배열에는 해당 Git 트리의 정식 SQL을 실행 순서대로 기록한다. `reservations`에는 아직 병합되지 않은 번호와 파일을 추적한다. `requires`는 선행 파일의 전체 이름이다. 기본 스키마처럼 이 저장소의 SQL이 만들지 않는 전제와 SQL이 직접 요구하지 않는 배포 순서는 아래에 명시한다. `environments`는 적용 대상 범위를 나타낸다.

**번호 예약, 정식 등록, DB 적용 이력은 별개다.** 예약은 병렬 작업의 번호를 확보한다. 정식 등록은 검수한 SQL이 해당 병합 후보의 적용 목록에 포함됐음을 나타낸다. 실제 적용은 대상 DB별 검증 증빙과 전체 파일명, 체크섬으로 기록한다. 기능 PR 병합, manifest 등록, 격리 PostgreSQL 테스트는 개발 DB의 적용 이력을 생성하지 않는다.

- [prepare_demo_db.py](../../scripts/prepare_demo_db.py)는 데모 DB의 `001_matching_snapshot.sql` 전후를 확인한다. [migrate_backend.py](../../scripts/migrate_backend.py)는 `002`부터 `008`까지만 파일명과 매핑한다. 기존 도구의 기본 점검도 DB에 연결하며, 적용 경로는 대상 스키마 검사, 잠금, 백업 절차를 사용한다. `007_walk_domain.schema.json`과 `008_gps_motion_analysis.schema.json`은 그 경로의 카탈로그 계약이다.
- [check_migrations.py](../../scripts/check_migrations.py)는 Python 표준 라이브러리로 로컬 파일만 읽는다. 배포 자동화나 기존 도구의 실행 범위를 늘리지 않는다.
- `001`은 기존 `matching_results`, `surveys`, `dogs`를 전제로 한다. 이어지는 파일들도 기존 `users`, `dogs`, `shelters` 등을 요구한다. 따라서 빈 PostgreSQL에 `001`부터 전부 적용하는 방식으로 신규 환경을 초기화할 수 없다. 최신 앱의 모델에서 직접 초기화하는 경로는 별도로 승인한 완전히 빈 환경에서만 검증한다. [app/main.py](../../app/main.py)는 `AUTO_CREATE_SCHEMA=true`일 때 `Base.metadata.create_all`을 호출한다. 이는 기존 테이블을 업데이트하거나 과거 SQL 실행을 입증하지 않는다. 최종 카탈로그와 통합 테스트를 검증하고 사용한 기준 커밋과 초기화 방식을 별도 기록한다.
- 과거 SQL 자체의 연속 적용을 시험할 때는 해당 릴리스의 기본 스키마를 먼저 준비한다. 예를 들어 [PostgreSQL 가입 테스트](../../tests/test_signup_postgres.py)는 이메일이 있는 `users` 등 구형 픽스처를 만든 뒤 실제 SQL을 적용한다. 지원되는 기존 DB와 신규 초기화 경로를 별개로 검증한다.

## 개발 중 검증과 최종 통합 적용

### 모든 기능 개발과 관련 PR 병합이 끝날 때까지

#44 이후에도 같은 절차를 계속한다. 기능별 SQL을 개별 파일로 작성하고 격리 PostgreSQL에서 선행 스키마, 적용 결과, 재실행 위험을 검증한다. 모든 신규 SQL은 예약 또는 정식 항목으로 추적하고, 기능 PR 병합 시 번호, 의존성, 실행 순서와 체크섬을 검토한다. 이 기간의 검증과 PR 병합은 개발 DB에 SQL을 적용하는 작업을 포함하지 않는다. 통합 실행 파일과 최종 적용 목록은 전체 개발과 병합이 끝난 뒤 확정한다.

### 모든 기능 개발과 관련 PR 병합이 끝난 뒤

1. 최종 `dev` 커밋에서 전체 SQL 목록, 의존 순서, 체크섬과 트랜잭션 방식을 확정한다. 읽기 전용으로 승인받은 뒤 실제 개발 DB의 기존 적용 이력과 카탈로그를 대조한다.
2. 이미 적용된 파일과 미적용 파일, 부분 적용 및 증빙이 불확실한 파일을 구분한다. 미적용 파일의 선행 조건과 실행 순서를 정하고, 불확실한 항목은 대조를 마친다. 기존 데이터 보존 방법, 백업과 복원 절차를 확인한다.
3. 확정한 순서를 하나의 명령으로 실행할 도구를 완성한다. 도구는 확인된 적용 이력을 바탕으로 실행 대상을 정하고 파일별 트랜잭션과 검증 기록을 유지한다. 실패 시 다음 파일로 넘어가지 않고, 재개 전에 커밋 여부와 현재 상태를 대조한다. 아래의 개별 `psql` 명령은 파일별 동작의 기준이다.
4. 격리 PostgreSQL에서 기본 스키마 또는 확인된 기존 스키마로부터 전체 적용을 검증한다. 데이터 보존과 부팅 후 기능, 재실행 시 건너뛸 파일, 중간 실패와 부분 적용에서의 처리까지 검증한다. 개발 DB에는 통합 검증 결과와 복구 절차를 확인하고 별도의 적용 승인을 받은 뒤 실행한다.

### 최초 등록한 SQL의 순서와 검사

`file`은 파일 내부 `BEGIN/COMMIT`, `psql-single`은 적용 시 `psql --single-transaction`으로 파일 전체를 감싸는 방식이다. 재실행 방어는 일부 문장이 기존 객체를 허용하는 경우를 가리킨다. 객체가 같은 이름이어도 정의가 다르면 그대로 남을 수 있으므로 검사를 통과하기 전에는 재실행을 결정하지 않는다.

| 순서와 파일 | 선행 조건 및 적용 전 확인 | 적용 후 확인 및 재실행 위험 | 트랜잭션 |
| --- | --- | --- | --- |
| 1. `001_matching_snapshot.sql` | 기본 `matching_results`, `surveys`, `dogs`. 새 고유 키에 맞지 않는 중복과 기존 인덱스 확인 | `survey_id` FK, `analysis_snapshot`, `uq_matching_survey_dog`의 정의와 조건 확인. 같은 이름의 기존 객체가 정의까지 일치하는지 확인 | file |
| 2. `002_auth_sessions.sql` | 기본 `users`. 기존 세션 테이블이나 충돌 인덱스 확인 | `auth_sessions`의 토큰 해시 PK, 사용자 FK, 만료 컬럼, 사용자 인덱스 확인. 기존 테이블은 자동 교정되지 않음 | file |
| 3. `003_public_data.sql` | 기본 `shelters`와 `dogs`. 기존 외부 코드 중복과 타입 확인 | `shelters.external_code` 및 고유 인덱스, `dogs.external_data`, `synced_at`, `process_state`, `neuter_status` 확인. 기존 정의는 자동 교정되지 않음 | file |
| 4. `004_chat.sql` | 기본 `users`, `dogs`, `surveys`. 기존 채팅 테이블 형상 확인 | `chat_sessions`, `chat_messages`의 FK와 조회 인덱스 확인. 테이블이 일부만 있거나 정의가 다른 경우 대조 | file |
| 5. `005_user_roles.sql` | 기본 `users`. 기존 `userrole` 타입과 enum 값 확인 | `USER`, `SHELTER`, `ADMIN` 값 및 `users.role`의 타입, NOT NULL, 기본값 `USER` 확인. 중단 시 타입만 남았는지 확인 | psql-single |
| 6. `006_user_shelters.sql` | 005, 기본 `shelters`. 기존 `shelter_id` 정의, 고아 참조 확인 | nullable `users.shelter_id`와 정확한 보호소 FK, `ix_users_shelter_id` 확인. FK 존재 검사만으로 참조 대상이나 정의가 일치한다고 볼 수 없음 | psql-single |
| 7. `007_walk_domain.sql` | 006, 기본 `users`, `dogs`, `shelters`. 일부 테이블이 이미 있으면 007 계약과 비교 | `dog_trait_summaries`, `dog_walk_settings`, `volunteer_profiles`, `walk_requests`, `dog_walk_slots`, `walks`, `walk_incidents`, `walk_markers`, `walk_points`, `walk_reviews`의 컬럼, FK, 제약, 인덱스와 활성 신청 조건부 고유 인덱스 확인 | file |
| 8. `008_gps_motion_analysis.sql` | 007의 `public.walks`. 기존 부분 적용과 거리, 시간, 상태 일관성 확인. 파일이 `public`을 명시함 | 이동, 정지, 미분류 시간 및 이동 거리 4개 컬럼과 `ck_walk_motion_partition`, `ck_walk_motion_pace` 확인. 기존 행을 0으로 채우지 않음 | psql-single |
| 9. `009_add_f03_walk_register.sql` | 007 및 `dogs`. 나이 컬럼, 날짜 슬롯, 난이도 제약이 이미 있는지 확인 | `dogs.age_months`, `dog_walk_date_slots`의 FK와 인덱스, 적용 시점 난이도 제약 확인. 가드 없는 ADD, CREATE가 있어 재실행 금지. 난이도 제약은 이후 011이 대체함 | psql-single |
| 10. `011_f05_walk_approval.sql` | 009. 기존 난이도 값이 새 제약을 만족하는지 확인 | `dog_walk_settings.ck_difficulty`와 기존 제약 제거, `walk_requests.rejection_reason_code` 확인. 제약을 다시 만드는 파일이라 성공 이력 확인 전 재실행 금지 | psql-single |
| 11. `012_f06_walk_tracking.sql` | 007. 종료 사유, 좌표 쌍과 범위, 정확도에 위반 행이 없는지 확인 | `walks`의 마지막 위치와 시각, 정확도, `end_reason` 컬럼과 대응 CHECK 확인. 중단 시 추가 컬럼과 교체된 제약을 함께 대조 | psql-single |
| 12. `013_f07_incident_report.sql` | 007 및 `users`. 신고 유형과 확인 필드 조합, 재시도 키 중복 검사 | `walk_incidents.client_report_id`, 확인자 FK와 확인 시각, 유형 및 확인 CHECK, 재시도 고유 인덱스와 미확인 인덱스 확인. 중단 시 제약 제거만 남았는지 검사 | psql-single |
| 13. `014_f08_live_walk.sql` | 기본 `users`. 기존 `phone`이 있으면 타입 검사 | `users.phone VARCHAR(50)` 확인. 기존 전화번호를 채우거나 정의를 교정하지 않음 | psql-single |
| 14. `015_f09_walk_review.sql` | 007. 평가, 관찰 값과 기존 태그 테이블 확인 | `walk_reviews.rating`, `comment`, 평가 및 관찰 CHECK, `walk_review_tags`의 FK와 조회 인덱스 확인. 중단 시 컬럼 추가와 제약 교체를 모두 확인 | psql-single |
| 15. `016_onboarding_signup.sql` | 기본 `users`의 `email`, `nickname`, `password`; 사전 검사에 필요한 005의 `role`. `users`가 비어 있어야 직접 적용 가능 | `login_id VARCHAR(20) NOT NULL`, 닉네임 길이, 비밀번호 NOT NULL, `login_id/nickname/email`의 lower 고유 인덱스 확인. 계정이 있거나 이메일 제거 이후에는 재실행 금지 | file |
| 16. `017_f10_behavior_marker.sql` | 007의 `walk_markers`. 기존 유형, 위치 출처, OTHER 메모의 CHECK 위반 집계 | `location_source VARCHAR(12) NOT NULL DEFAULT 'CURRENT'`, `ck_marker_type`, `ck_location_source`, `ck_marker_other_memo` 확인. 제약 교체 중 실패 시 제약 누락 가능 | psql-single |
| 17. `017_remove_user_email.sql` | 016 또는 승인된 기존 계정 전환 증빙, 올바른 `login_id`와 고유 인덱스, 이메일 의존 객체, 복원 검증된 백업 | `users.email`, `users_email_key`, `uq_users_email_lower` 제거 및 로그인 ID 무결성 확인. 이메일 값 삭제는 커밋 후 복구 계획 필요 | file |
| 18. `018_f11_walk_overtime.sql` | 007의 `walks`. 기존 예상 종료 시각과 예정 산책 시간의 CHECK 위반 집계 | nullable `planned_duration_minutes`, `expected_end_at` 및 `ck_walk_planned_duration`, `ck_walk_expected_end_order` 확인. 제약 교체가 끝났는지 함께 확인 | psql-single |
| 19. `018_user_consents.sql` | 앞선 이메일 제거까지 완료하는 배포 순서. SQL의 직접 전제는 기본 `users` | `users.age_14_plus_confirmed_at`, `user_consents`의 사용자 FK, 유형 및 행동 CHECK, 조회 인덱스 확인. 과거 계정의 자기확인 시각을 채우지 않음 | file |
| 20. `019_first_walk_guide.sql` | 기본 `users`. 기존 완료 시각 컬럼 정의 확인 | nullable `first_walk_guide_completed_at TIMESTAMPTZ` 확인. 기존 완료 시각을 채우지 않음 | psql-single |
| 21. `020_shelter_volunteer_reviews.sql` | 007의 `walks`, 기본 `shelters`, `users`. 기존 평가 또는 태그 테이블, 고유성, 참조와 평점 범위 확인 | `shelter_volunteer_reviews`, `shelter_volunteer_review_tags`의 산책당 단일 평가, 사용자 및 보호소 FK, 평점과 태그 CHECK, 조회 인덱스 확인. 중단 시 평가 테이블만 남았는지 검사 | psql-single |

`016`의 사용자 역할은 적용 SQL 자체의 의존성이 아니고 사전 검사 스크립트의 전제다. 카탈로그에 기본 테이블이 없거나 005보다 앞선 스키마라면 표적과 기초 상태를 확인하고 017 전용 사전 검사를 보류한다. `018_user_consents.sql`과 이후 온보딩의 순서는 쌓인 앱 릴리스 의존성도 포함한다.

테스트 참조는 [데모 DB](../../tests/test_demo_tools.py)와 [인증](../../tests/test_auth.py), [역할](../../tests/test_role_migration.py), [보호소](../../tests/test_shelter_migration.py), [산책 도메인 PostgreSQL](../../tests/test_walk_postgres.py), [GPS PostgreSQL](../../tests/test_gps_motion_postgres.py), [가입](../../tests/test_signup.py)과 [가입 PostgreSQL](../../tests/test_signup_postgres.py)을 확인한다. 가입 PostgreSQL 테스트는 `002`, `005~007`, `014~016`, 이메일 제거용 `017`, 동의용 `018`을 파일 단위로 읽는다. `009`, `011~015`의 해당 API 검증은 `tests/test_walk_settings_api.py`, `tests/test_walk_requests.py`, `tests/test_f05_walk_approval.py`부터 `tests/test_f09_walk_review.py`까지 볼 수 있다. `tests/test_f10_behavior_marker.py`, `tests/test_f11_walk_overtime.py`, `tests/test_f14_first_walk_guide.py`, `tests/test_f15_shelter_volunteer_reviews.py`는 해당 기능의 동작을 확인한다. API 테스트 통과를 전체 SQL 적용 검증으로 취급하지 않는다.

## 번호 충돌과 예약

이미 병합된 파일명을 보존하는 **방식 A**를 사용한다. 파일을 지정하는 `psql -f`는 숫자 접두사를 해석하지 않는다. 반면 번호로만 이력을 기록하거나 파일 글로브와 정렬로 적용 대상을 결정하면 한 파일을 생략하거나 승인하지 않은 파일을 적용할 수 있다. 파일명 변경은 Git 이력, 테스트, 과거 수동 실행 기록을 대조하게 만들고 이미 적용된 파일을 새 파일로 오인할 수 있어 **방식 B**는 적용하지 않는다.

- `017_f10_behavior_marker.sql`은 산책 마커를 바꾸고 `017_remove_user_email.sql`은 사용자 이메일을 제거한다. 두 파일 간 직접 스키마 의존성이 없다. 이메일 제거는 `016_onboarding_signup.sql` 또는 검증한 계정 전환에 의존한다. [PR #47](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/47)
- `018_f11_walk_overtime.sql`은 `walks`에 시간 컬럼과 CHECK를 더하고 `018_user_consents.sql`은 사용자 자기확인 시각과 동의 테이블을 추가한다. 동의용 SQL은 출시 순서상 이메일 제거 뒤에 둔다. F-11은 동의 적용의 선행 SQL이 아니다. [PR #48](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/48)
- 기존 번호 예외는 위 두 쌍의 전체 파일명에만 적용한다. 이미 병합된 019, 020과 022는 보존하고, 온보딩용으로 023~026을 배정했다.

| 번호와 파일 | 대상 | SQL 범위 |
| --- | --- | --- |
| 010, `010_add_f04_walk_booking.sql` | 원격 기능 브랜치, 실행 목록 제외 | 009 날짜 슬롯을 `walk_requests.date_slot_id`에 연결한다. 자체 트랜잭션과 재실행 가드가 없다. |
| 022, `022_regions.sql` | #64, `dev` | 지역 조회용 `regions`를 생성한다. 선행 SQL은 없다. |
| 023, `023_user_pledge_acknowledgements.sql` | #50 | 018 동의 다음에 적용한다. 서약 기록의 사용자 FK와 버전별 고유성을 검증한다. |
| 024, `024_onboarding_basic_info.sql` | #52 | 023 다음에 적용한다. 계정 상태와 연령 확인 시각, 기본 정보와 휴대전화 인증 테이블을 추가한다. 새 로그인 코드는 이 컬럼을 조회한다. |
| 추가 SQL 없음 | #53 | 서약과 기본 정보 SQL을 상속한다. #57 통합 후보에서는 가입 종료 시 반려견 경험 행도 정리되는지 검증한다. |
| 025, `025_onboarding_dog_experience.sql` | #57 | 024 다음에 적용한다. 반려견 경험과 어려웠던 상황 선택을 저장하는 두 테이블을 추가한다. |
| 026, `026_onboarding_walking_routine.sql` | #44 | 022와 025 다음에 적용한다. 산책 루틴과 지역, 시간대 연결 테이블 세 개를 추가한다. 지역 연결은 `regions` FK를 쓴다. |

#50 → #52 → #53 → #57 순서로 병합한다. 부모를 병합한 뒤 자식의 Base와 변경 범위를 다시 확인한다. 각 PR은 자신의 SQL을 정식 목록에 등록하고, 후속 PR의 SQL은 해당 PR까지 예약으로 둔다. #44는 #42와 #43을 합친 DB에서 지역 선택과 온보딩 흐름을 검증한다. [Issue #44](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/44)

### 신규 번호를 배정할 때

1. 최신 원격 브랜치와 열린 PR의 전체 파일명을 조회한다. 필요 시 PR head의 저장소가 포크인지 확인하고 해당 PR의 파일 목록도 확인한다. 아래 예시의 `<remote-ref>`, `<number>`는 확인한 값으로 바꾼다.

   ```sh
   git fetch origin
   git for-each-ref --format="%(refname:short)" refs/remotes/origin
   git ls-tree -r --name-only <remote-ref> migrations
   gh pr list --state open --limit 100 --json number,headRefName,headRepository,baseRefName
   gh pr diff <number> --name-only
   ```

2. 이미 사용한 번호와 미병합 예약을 모두 보존한다. `010` 같은 이력상의 빈자리를 재사용하지 않는다. 병렬 작업에서는 번호와 전체 파일명을 이슈나 PR에 남기고, 배정과 병합 직전에 각각 최신 원격을 다시 확인한다.
3. 미병합 파일은 전체 파일명과 출처, 알려진 선행 파일을 `reservations`에 등록한다. 예약 항목에 개발 DB 적용 상태를 적지 않는다.
4. 파일을 변경하기 전에 병합 또는 적용 여부를 확인한다. 병합된 파일은 이름과 내용을 보존하고, 변경이 필요한 스키마는 새 번호의 후속 SQL로 처리한다.

### 기능 PR을 dev에 병합할 때

1. 최신 `dev`와 합친 후보에서 추가되거나 바뀐 SQL과 예약을 확인한다. 충돌을 해소할 때 예약 파일명의 변경과 후속 항목의 `requires` 참조를 같이 검토한다.
2. 함께 병합하는 SQL을 같은 PR의 `migrations` 배열에 정식 등록한다. 예약했던 파일은 `reservations`에서 제거하되 아직 미병합인 다른 예약은 유지한다. 의존 파일의 전체 이름과 위치, 트랜잭션 방식, 적용 대상 환경, 사전 검사, 최종 SQL의 SHA-256을 검토한다.
3. 병합 전에 후보에서 아래 기본 검사를 실행하고 추가 및 변경한 SQL과 예약 이름을 모두 `--check-reservations-for`로 검사한다. 반복 옵션으로 각 파일을 지정한다. 병합 후보에서 새 SQL이 예약에만 있거나 정식 목록에서 빠졌거나 두 곳에 중복되면 실패한다. 해당 PR의 번호 충돌도 해소한 뒤 병합한다.
4. 실제 병합 뒤 최신 `dev` 체크아웃에서 기본 검사를 다시 실행한다. 검증기는 `migrations/`의 SQL을 모두 열거해 manifest와 양방향으로 대조한다. 전후 검사가 모두 성공해야 병합 과정에서 파일이나 등록 정보가 누락되지 않았음을 확인할 수 있다. 다른 기능 PR이 이 관리 기준보다 먼저 병합되면 관리 PR을 최신 `dev`에 맞춰 누락 SQL을 등록한 뒤 검증한다.

## DB 연결 없이 하는 사전 검증

저장소 루트에서 Python 3.10 이상으로 실행한다.

```sh
python scripts/check_migrations.py
python scripts/check_migrations.py --check-reservations-for 023_example.sql
python scripts/check_migrations.py --check-reservations
python scripts/check_migrations.py --propose 023_example.sql
python -m pytest -q tests/test_check_migrations.py
```

기본 명령은 SQL 누락, 미등록 파일, 허용되지 않은 중복, 체크섬 불일치, 검사 스크립트 누락, 선행 SQL 누락, 순환과 순서 오류에 종료 코드 1을 반환한다. 미병합 예약의 번호 충돌은 `BLOCKED FUTURE MIGRATION`으로 표시한다.

- `--check-reservations-for`의 예시 파일명은 병합 후보에서 추가하거나 변경한 실제 이름으로 바꾼다. 정식 항목 또는 예약에 없는 이름과 선택한 파일의 예약 충돌은 종료 코드 1이다. 선택한 파일과 관계없는 예약 충돌은 출력되며 해당 PR의 검사를 실패시키지 않는다. 기본 검사가 찾는 정식 목록 오류는 이 옵션을 사용해도 모두 실패한다.
- `--check-reservations`는 전체 예약을 점검하며 충돌이 있으면 종료 코드 1을 반환한다.
- `--propose`는 목록에 등록하기 전에 다음 번호를 확인하는 명령이다. 최신 원격 브랜치와 PR도 함께 조회한다.

SHA-256은 SQL 바이트에서 CRLF만 LF로 통일해 계산한다. BOM, 공백, 내용 변경은 해시를 바꾼다. 후보의 해시가 다른 경우 배포를 멈추고, 검수한 Git 커밋에서 파일을 다시 가져와 차이를 확인한다. 새 파일의 체크섬을 확인하는 예시다.

```sh
python -c "from pathlib import Path; from scripts.check_migrations import checksum; print(checksum(Path('migrations/023_example.sql')))"
```

검토한 기록의 초안을 별도 파일로 갖고 있다면 `python scripts/check_migrations.py --evidence path/to/evidence.json`으로 적용 후보와 재실행 위험을 검토한다. 예시는 항목 구조만 보여준다. 실제 증빙 위치와 해시를 입력하고 파일은 접근이 제한된 배포 기록에 둔다.

```json
{
  "environment": "dev",
  "files": {
    "001_matching_snapshot.sql": {"state": "schema-match", "proof": "검토한 카탈로그 비교 기록의 위치"},
    "002_auth_sessions.sql": {"state": "unknown"}
  }
}
```

- `applied`에는 해당 전체 파일의 실행 증빙을 가리키는 `proof`와 일치하는 `sha256`이 필요하다. 검증된 선행 파일까지 확인되면 `SKIP_RECORDED`로 표시한다. 도구는 증빙 참조의 내용이나 실제 DB에 접속해 확인하지 않는다.
- `absent`에는 전체 사전 상태를 검토한 증빙을 입력한다. 선행 파일 이력이 검증됐을 때 `MANUAL_PRECHECK_REQUIRED`가 나온다. 사람이 현재 상태와 승인을 확인하기 전에는 실행하지 않는다.
- 스키마는 맞지만 실행 증빙이 없으면 `schema-match`, 일부만 일치하면 `partial`, 증거가 부족하거나 미기재면 `unknown`이다. 이들 상태와 체크섬 불일치, 선행 이력 부족은 별도 대조가 필요하며 종료 코드 1이다. 자동 실행 프로그램을 도입하기 전에 개발 DB에서 이 대조를 마친다.

## 최종 통합 적용 전 DB 이력 대조

모든 기능과 관련 PR 병합이 끝난 뒤 읽기 전용 접속 승인을 받아 이 절차를 진행한다. 개발과 운영은 각각 승인된 접속 프로필과 별도 실행 기록을 사용한다. 시작할 때 접속 대상, 역할, 대상 스키마와 `search_path`를 확인한다. 읽기 전용 프로필로 다음 유형의 카탈로그 조회를 같은 스냅샷에서 실행한다. 예시는 검증 대상이 `public`으로 확정된 경우다. 세션은 `ROLLBACK`으로 종료한다.

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT current_database(), current_user, current_schema(), current_setting('search_path');

SELECT name, to_regclass(format('%I.%I', 'public', name)) AS object
FROM (VALUES ('users'), ('walks'), ('walk_markers'), ('user_consents')) AS targets(name);

SELECT table_name, column_name, data_type, character_maximum_length,
       numeric_precision, numeric_scale, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name IN ('users', 'walks', 'walk_markers', 'user_consents')
ORDER BY table_name, ordinal_position;

SELECT c.conrelid::regclass AS object, c.conname, c.contype, c.convalidated,
       pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint AS c
WHERE c.conrelid IN (to_regclass('public.users'), to_regclass('public.walks'),
                    to_regclass('public.walk_markers'), to_regclass('public.user_consents'))
ORDER BY 1, 2;

SELECT i.indrelid::regclass AS object, i.indexrelid::regclass AS index_name,
       i.indisunique, i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index AS i
WHERE i.indrelid IN (to_regclass('public.users'), to_regclass('public.walks'),
                      to_regclass('public.walk_markers'), to_regclass('public.user_consents'))
ORDER BY 1, 2;

ROLLBACK;
```

1. 각 파일이 기대하는 사전 및 사후 상태를 표와 원본 SQL로 확인한다. 테이블 하나가 있거나 관련 컬럼 하나가 없다는 이유만으로 SQL 전체가 적용됐다고 판단하지 않는다. 이름에서 끝나지 않고 타입, NULL, 기본값, enum, FK 대상과 삭제 정책, CHECK 정의와 검증 상태, 고유 인덱스의 표현식과 조건, 인덱스 유효성을 확인한다. 이후 SQL이 바꾼 정의도 기록한다. 예를 들어 011이 009의 난이도 CHECK를 대체하고 이메일 제거용 017이 016의 이메일 인덱스를 제거한다.
2. 값의 변경이 중요한 파일은 승인된 전후 집계와 실행 증빙, 백업 검증 기록을 대조한다. 계정 수, 역할별 수, 예외 수와 참조 무결성 등만 남긴다. 각 값, 이메일, 비밀번호, 해시나 연결 문자열을 공유 기록에 적지 않는다. 과거 값이나 증빙이 없으면 스키마 일치와 실제 데이터 변경 여부를 분리해 기록한다.
3. 이메일 제거 후보는 아래 사전 검사를 별도 읽기 세션에서 실행한다. 시작 전에 DB, 역할, 스키마 범위가 맞는지 확인하고 `psql` 종료 코드와 마지막 `ROLLBACK`을 확인한다. 스크립트의 마커와 의존 객체 조회를 나머지 카탈로그 검사와 대조한다. 검색으로 나온 뷰와 함수는 실제 이메일 의존 여부를 별도로 확인한다.

   ```sh
   psql -X -v ON_ERROR_STOP=1 "service=approved_dev_readonly" -f scripts/precheck_017_users.sql
   ```

   - 016의 전체 요건을 충족하고 이력이 확인되면 016을 건너뛴다. 구형 스키마이고 `users`가 비어 있으면 쓰기를 멈춘 후 016 적용을 검토한다. `users`가 비어 있지 않고 016 요건을 충족하지 않으면 별도 승인된 계정 전환이 필요하다. 017이 이미 반영됐거나 일부 객체와 기록이 어긋나면 적용 이력을 대조하고 016을 다시 실행하지 않는다.
   - `users.email` 및 이메일 제약과 인덱스, `walk_markers.location_source`와 제약, `walks`의 F-11 시간 컬럼과 제약, `users.age_14_plus_confirmed_at`와 `user_consents`를 따로 확인한다. 한 쌍 중 하나의 결과로 다른 파일의 상태를 추정하지 않는다.
4. 승인된 작업 로그에서 **DB별 전체 파일명, Git 커밋, 체크섬, 실행 시각, 적용 계정, 작업 승인, 사전 및 사후 검증 결과**를 찾는다. 애플리케이션 부팅, 최종 모델을 이용한 초기화, 수동 보정, 실행이 끝나기 전 실패는 별도로 적는다. 증빙이 없거나 부분 적용이면 실제 실행 기록을 소급해서 만들지 않는다.
5. 추후 이력 테이블을 도입할 때는 DB별로 전체 파일명을 키로 쓰고 체크섬과 출처를 기록한다. 실행 완료와 승인된 기준 스키마를 다른 증빙 유형으로 관리한다. 먼저 환경별 기존 이력을 확인하고, 검수를 마친 적용에 한해 가능한 경우 실행과 이력 기록을 같은 트랜잭션에 넣는다. 외부 트랜잭션이 필요한 작업은 검증 완료 후 이력을 기록한다. 이번 변경에서는 이력 테이블을 만들지 않는다.

## 개발 및 운영 적용 점검표

| 시점 | 개발 DB | 운영 DB |
| --- | --- | --- |
| 대상 확정 | 개발 환경, 승인된 DB와 역할, 스키마, Git 커밋, 적용 파일 순서와 예약 확인 | 운영 전용 프로필과 변경 승인, 배포 시간 및 담당자 확인. 개발에서 같은 커밋과 초기화 또는 업그레이드 경로를 검증 |
| 쓰기 전 | 승인된 읽기 전용 사전 검사, 환경별 이력 대조, 누락과 부분 적용 해결, 검수한 백업과 복구 확인 | 운영 스냅샷 시점과 복구 가능 범위를 확인하고 복원 연습, DB 잠금에 따른 영향, API 쓰기 중지 범위와 서비스 복구 기준 검토 |
| 실행 | 승인된 통합 명령으로 파일별 적용과 사후 검사를 수행하고 기록 | 운영 승인자와 적용자가 대상, 파일, SHA-256을 다시 확인. 중단 기준과 실패 시 담당자를 공유 |
| 사후 | 카탈로그 재검증과 합성 계정 스모크 테스트. 증빙 저장 | 카탈로그와 서비스 헬스, 오류율 및 승인된 스모크 항목 확인. 기록 정리 후 쓰기를 재개 |

### 파일별 적용과 실패 복구 기준

전체 기능 개발 중에는 아래 명령을 격리 PostgreSQL에서 검증할 때 사용한다. 개발 DB의 통합 적용 도구는 최종 단계에서 이 파일별 트랜잭션, 중단 및 사후 검사 기준을 따른다.

1. 적용 전 `python scripts/check_migrations.py`로 대상 커밋의 파일과 체크섬을 확인한다. 미병합 번호 충돌이 해당 배포 후보에 포함됐으면 멈춘다. 쓰기 작업 승인과 검증된 백업을 확보한 뒤 승인된 쓰기 프로필에서 DB와 역할, 스키마를 다시 확인한다. `016`의 빈 사용자 조건, `017`의 이메일 삭제, 코드가 새 컬럼을 읽는 `018_user_consents.sql`과 온보딩 변경은 앱 중지와 기동 순서를 함께 확정한다. 현재 `User` 모델은 `019_first_walk_guide.sql`이 만드는 `users.first_walk_guide_completed_at`도 매핑하므로, 016~019를 적용한 뒤 새 코드를 기동한다. 019가 없으면 사용자 조회가 있는 가입과 로그인이 `UndefinedColumn`(SQLSTATE 42703)으로 실패해 500이 된다([#65](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/65)). #44 이후 코드는 다음 온보딩 단계를 계산할 때 `volunteer_onboarding_routines`를 읽는다. 따라서 022와 026까지 적용한 뒤 기동한다. 026이 없으면 Step 2를 마친 계정의 Step 2 조회와 재저장, Step 3 API가 500이 된다. 기존 `002~008`의 전용 적용 절차를 쓸 경우 도구의 계약과 백업 검사를 유지한다.
2. 전체 이름을 명시해 한 파일씩 실행한다. 아래 명령은 형식 예시다. 각각 파일 내 트랜잭션과 외부 단일 트랜잭션을 구분한다. `-X`는 사용자 psql 시작 파일을 무시하고, `ON_ERROR_STOP=1`은 스크립트 오류에서 실행을 멈춘다. `--single-transaction`을 파일 내부에 `BEGIN/COMMIT`이 있는 SQL에 겹쳐 쓰지 않는다. [PostgreSQL 16 psql 문서](https://www.postgresql.org/docs/16/app-psql.html)

   ```sh
   psql -X -v ON_ERROR_STOP=1 --single-transaction "service=approved_isolated_test" -f migrations/018_f11_walk_overtime.sql
   psql -X -v ON_ERROR_STOP=1 "service=approved_isolated_test" -f migrations/018_user_consents.sql
   ```

   두 파일이 모두 적용 대상이면 위 순서를 따른다. 다음 파일은 앞 파일의 커밋과 사후 검사를 확인한 뒤 시작한다.
3. F-10과 F-11에는 파일 단위 `BEGIN/COMMIT`이 없다. 기본 autocommit으로 실행하면 선행 `ADD COLUMN`이나 `DROP CONSTRAINT`만 남고 새 제약 부착에 실패할 수 있다. 기존 데이터의 위반 건수를 먼저 확인하고 파일당 단일 트랜잭션으로 적용한다. 신규 SQL도 가능한 한 파일당 하나의 트랜잭션으로 관리한다. `CREATE INDEX CONCURRENTLY`처럼 트랜잭션 블록 밖에서 실행해야 하는 SQL은 `outside`로 별도 검토하고, 중간 실패 후 무효 인덱스 등 남은 객체를 확인할 복구 절차를 함께 승인한다. [PostgreSQL 16 CREATE INDEX 문서](https://www.postgresql.org/docs/16/sql-createindex.html)
4. 파일마다 종료 코드, 커밋 여부, 대상 객체와 검증 상태를 기록한다. 새 코드 기동 후 스키마 범위에 맞춰 가입과 `identifier` 로그인, `/users/me`, 만 14세 이상 자기확인 시각과 동의 저장, 필수 동의 또는 문서 버전 검증 실패 시 미저장, F-10 마커, F-11 산책 시간, 산책 안내와 보호소 평가를 합성 데이터로 검증한다. 온보딩 후속 후보에서는 서약, 기본 정보와 연령 상태, Step 2 조회 및 저장, 가입 종료를 더 확인한다. 지역 후보는 적재 후 행 수, 고유 키와 참조 무결성 및 재실행 동작을 확인한다.
5. 오류나 연결 끊김이 발생하면 다음 파일과 서비스 기동을 멈춘다. 실제 커밋 여부를 확인하고 읽기 전용으로 사전 및 사후 상태의 차이를 다시 분류한다. 일부만 남았거나 스키마와 이력이 다르면 예방 가드로 덮거나 같은 파일을 반복 실행하지 않는다. 데이터 손실이 없는 승인된 보정 또는 검증된 백업으로 복구하고 카탈로그, 데이터 집계 및 서비스 검증을 다시 수행한다. 이메일 삭제가 커밋된 상태에서 구 버전 서비스만 재기동하는 방식은 복구가 되지 않는다. 복원 시 백업 이후 변경의 보존 방법도 함께 결정한다.
