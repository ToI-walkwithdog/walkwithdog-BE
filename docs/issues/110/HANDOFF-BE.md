# BE 팀원 개발 가이드 (16-4~16-9, 18~20단계)

다른 BE 팀원이 이 문서와 각 Issue만으로 독립적으로 작업할 수 있도록 정리했다. 전체 순서와 충돌 방지는 [HANDOFF-ROADMAP.md](HANDOFF-ROADMAP.md), #110 결과는 [HANDOFF.md](HANDOFF.md)를 본다.

- 조사 기준: BE `dev` `f5b230e696f3b7461eda5cf36f5266990e5f65df`
- 표기: **확정**(사용자 승인), **권장**(제안), **미확정**(결정 필요), **미확인**(이번 조사에서 확인하지 못함)

## 목차

0. [공통 작업 규칙](#0-공통-작업-규칙)
1. [16-4 통합 DB 마이그레이션 도구](#1-16-4-통합-db-마이그레이션-도구)
2. [16-5 실제 개발 DB 읽기 전용 조사](#2-16-5-실제-개발-db-읽기-전용-조사)
3. [16-6 실제 개발 DB 마이그레이션 적용](#3-16-6-실제-개발-db-마이그레이션-적용)
4. [16-7 공식 데이터 수집과 초기 데이터 구성](#4-16-7-공식-데이터-수집과-초기-데이터-구성)
5. [16-8 Kakao 위치 판별 설정](#5-16-8-kakao-위치-판별-설정)
6. [16-9 서비스 정책 구성](#6-16-9-서비스-정책-구성)
7. [18~20단계 담당 범위](#7-1820단계-담당-범위)

## 0. 공통 작업 규칙

- **Issue 전달 절차**: 착수는 [docs/dev-automation/RUNBOOK.md](../../dev-automation/RUNBOOK.md)의 승인 절차를 따른다. 브랜치는 `feature/`, `fix/`, `chore/`, `test/`, `docs/` 소문자 kebab-case이고 AI 도구 이름을 쓰지 않는다.
- **보호 경로**: `.github/`, `docs/dev-automation/`(기준 목록 `test-baseline.json` 제외), `scripts/dev_*`, `scripts/issue_delivery.py`, `scripts/check_migrations.py`, `tests/test_dev_delivery.py`, `CLAUDE.md`, `AGENTS.md`, `.gitignore`는 기능 PR로 바꿀 수 없다. 필요하면 사람이 검토하고 병합하는 별도 유지보수 PR로 분리한다.
- **DB 안전**
  - 실제 개발 DB 접속 정보는 추측하지 않고, 비밀 값과 연결 문자열을 Issue, PR, 로그, 문서에 남기지 않는다.
  - 테스트는 `toi_walk_test_*` 이름의 빈 로컬 PostgreSQL에서 격리 스키마로 한다(예: `tests/test_user_profile_postgres.py`의 `IsolatedSchema`).
  - `ON DELETE CASCADE`를 새로 추가하지 않고, 기존 데이터를 무조건 삭제하지 않는다.
- **테스트 데이터**: 합성 데이터만 쓰고, 실제 사용자 데이터를 쓰지 않는다.
- **새 테스트를 추가하면** `python -m scripts.dev_gate inventory <report>`로 기준 목록을 갱신한다. 테스트 ID를 지우거나 이름을 바꾸지 않는다.

## 1. 16-4 통합 DB 마이그레이션 도구

관련 Issue: [#113](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/113)

### 현재 상태(확인함)

- **`migrations/manifest.json`**(version 1): 정식 37개(001~037, 010 제외, 017과 018은 각각 두 파일), 예약 1개(`010_add_f04_walk_booking.sql`, branch-only). 항목마다 `file`, `sha256`(CRLF만 LF로 바꾼 바이트의 SHA-256), `requires`, `transaction`(`file` 또는 `psql-single`), `environments`, `prechecks`가 있다.
- **`scripts/check_migrations.py`**(보호 경로, 수정 금지): DB 없이 다음을 검사한다.
  - 기본: 파일과 manifest의 양방향 대조, 체크섬, 선행 SQL, 순환, 순서
  - `--evidence`: 파일별 기록 상태(`applied`, `absent`, `schema-match`, `partial`, `unknown`)로 적용 후보를 계획한다(`evidence_plan()`).
  - 재사용 가능한 함수: `checksum()`, `read_json()`, `validate_inventory()`, `validate_order()`. 가져다 쓰는 것은 가능하고 수정은 유지보수 PR로만 한다.
- **`scripts/migrate_backend.py`**: 002~008만 다루는 구 도구다. dry-run과 백업 필수(`--apply`).
- **`scripts/prepare_demo_db.py`**
  - `backup_database(engine, directory)`: `pg_dump`, 0600 권한, 자격 증명을 인자에 넣지 않는다.
  - `validate_local_engine()`: **localhost만 허용**한다. 실제 개발 DB에는 쓸 수 없다.
- **`scripts/walk_schema.py`**: 007의 카탈로그 계약을 읽기 전용으로 검증한다.
- **`scripts/deployment_common.sh`의 `catalog_snapshot()`**: 배포 호환성 지문 `pg16-public-v1`을 만든다(`public` 스키마의 관계, 열, 제약, 인덱스, enum, 시퀀스, 함수, 뷰, 트리거 정의를 정규화한 SHA-256).
- **[docs/migrations/README.md](../../migrations/README.md)**: "모든 기능 개발 후" 절차 1~4, 파일별 사전과 사후 확인표, `psql` 단일 파일 실행 기준, 실패 복구 기준을 정의한다.

### 요구사항

- 새 도구 `scripts/apply_migrations.py`(권장 이름)를 만든다. 기존 도구는 바꾸지 않는다.
  - manifest와 SQL 체크섬, `requires` 순서를 `check_migrations`의 함수로 검증한다.
  - 대상 DB의 **증빙 파일**(`--evidence` 형식)을 입력받아 실행 대상을 정한다. 증빙 없이 실제 DB 상태를 추측해 적용하지 않는다.
  - 파일별로 실행한다. `transaction`이 `file`이면 파일 내부 `BEGIN/COMMIT`, `psql-single`이면 외부 단일 트랜잭션으로 감싼다.
  - 실패하면 즉시 멈추고 다음 파일로 넘어가지 않는다. 재개 전에 커밋 여부와 현재 상태를 다시 대조한다.
  - 실행마다 기록을 남긴다(파일명, SHA-256, Git 커밋, 시작과 종료 UTC, 결과, 사전과 사후 검증). 값, 비밀번호, 연결 문자열은 남기지 않는다.
  - 기본은 dry-run(계획만 출력)이다. `--apply`는 검증된 백업과 대상 확인 문자열이 있을 때만 동작한다.
  - 대상 DB 가드: localhost 전용인 `validate_local_engine()` 대신, 명시적 대상 식별(DB 이름, `system_identifier`)을 입력받아 실제 연결과 대조한다.
- **빈 DB 초기화와 기존 DB 마이그레이션을 구분한다.**
  - 001은 기존 `matching_results`, `surveys`, `dogs`를 전제로 한다. 그래서 빈 DB에 001부터 전체를 적용할 수 없다(README).
  - 빈 DB는 모델 초기화(`AUTO_CREATE_SCHEMA=true` 또는 `Base.metadata.create_all`)로 만들고, 그 결과가 최종 SQL 적용 결과와 **동등한지** 카탈로그로 검증한다.
  - 초기화 증빙(모델 커밋, 카탈로그 지문)은 "SQL 실행 이력"과 다른 유형으로 기록한다.
- **격리 PostgreSQL 검증**
  - 확인된 기존 스키마(또는 기본 스키마)에서 전체 적용
  - 재실행 시 건너뛰기
  - 중간 실패와 부분 적용의 처리
  - 모델 초기화 결과와의 동등성
  - 적용 후 앱 부팅과 핵심 API 스모크
- **배포 연계**: 적용 결과에서 `schema-record.json`의 `migrations` 배열(실제 적용 순서, 파일명, SHA-256)을 만들 수 있어야 한다. 지문은 `catalog_snapshot()`과 같은 알고리즘(`pg16-public-v1`)으로 만든다([docs/deployment/EC2_SETUP.md](../../deployment/EC2_SETUP.md)).

### 확정 정책

- 전체 SQL을 무조건 재실행하지 않는다. 실제 DB 적용 상태를 임의로 판단하지 않는다.
- 037 관련 조건은 [HANDOFF.md 4절](HANDOFF.md#4-마이그레이션-037과-배포-선행-조건)을 그대로 따른다.

### 테스트와 완료 조건

- **단위 테스트**: 계획 계산(상태별), 체크섬 불일치 중단, `requires` 누락 중단, 트랜잭션 방식별 실행, 실패 시 중단과 기록, 재개 시 대조
- **격리 PG 테스트**: 위 "격리 PostgreSQL 검증" 전 항목. CI 전용 PG 그룹과 겹치지 않게 `SIGNUP_TEST_DATABASE_URL` 기반 격리 스키마를 쓴다.
- **완료**: 도구와 테스트, 사용 문서(README 보강은 `docs/migrations/README.md`에, 보호 경로가 아님)가 dev에 병합되어 있다.

## 2. 16-5 실제 개발 DB 읽기 전용 조사

관련 Issue: [#114](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/114)

### 요구사항

- 사용자 승인과 접근 방법(채팅에 자격 증명을 남기지 않는 방식)을 받은 뒤 **읽기 전용 세션**(`SET TRANSACTION READ ONLY`, 마지막 `ROLLBACK`)으로만 조사한다.
- 조사 대상
  - DB 식별: `system_identifier`, DB 이름, OID, PostgreSQL 버전
  - 카탈로그: README의 파일별 "적용 후 확인"과 대조할 객체(테이블, 열, 제약, 인덱스, enum)
  - 적용 이력 증빙: 작업 로그, 기존 기록, 백업 기록(README "최종 통합 적용 전 DB 이력 대조" 4번)
  - 데이터 집계: 계정 수, 역할별 수, 예외 수, 참조 무결성(값은 남기지 않음)
- 결과
  - 파일별 상태를 `applied`, `absent`, `schema-match`, `partial`, `unknown`으로 분류한 **증빙 JSON**(접근 제한 저장소에 보관, 저장소에 커밋하지 않음)
  - 저장소에는 비밀이 없는 요약만 남긴다.
- `python scripts/check_migrations.py --evidence <file>`로 계획을 검토하고, 종료 코드 1인 항목(불확실, 부분 적용, 체크섬 불일치)을 모두 대조한다.

### 완료 조건

- 024 이후 모든 파일(특히 035, 036, 037)의 상태가 증빙과 함께 분류되어 있다.
- 백업과 복원 절차가 확인되어 있다(실제 복원 리허설은 격리 환경).
- 16-6의 적용 계획(대상 파일, 순서, 예상 영향, 중단 기준)이 사용자 승인을 받을 수 있는 형태로 정리되어 있다.

## 3. 16-6 실제 개발 DB 마이그레이션 적용

관련 Issue: [#115](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/115)

### 선행 조건

16-4 도구 병합, 16-5 증빙과 계획 승인, 검증된 백업, 사용자의 쓰기 승인

### 요구사항

- 16-4 도구로 승인된 계획만 적용한다. 파일별 결과를 기록하고, 실패 시 멈추고 보고한다.
- **037 조건**(확정, [HANDOFF.md 4절](HANDOFF.md#4-마이그레이션-037과-배포-선행-조건))
  1. 037을 먼저 적용한 뒤 #110 이후 이미지를 배포한다.
  2. 호환성 기록과 이미지별 승인의 `migrations`에 037 파일명과 SHA-256을 넣는다.
  3. 카탈로그 지문은 037 적용 이후에 만든다.
  4. 모델 초기화 DB는 동등 스키마를 검증하고, 적용 이력과 초기화 증빙을 구분한다.
  5. 배포 스크립트가 순서를 강제하지 않음을 승인 체크리스트에 명시한다.
  6. 이전 이미지로 롤백할 때 DB를 되돌리지 않는다.
- 적용 후 `schema-record.json`과 이미지별 승인 파일을 [EC2_SETUP.md](../../deployment/EC2_SETUP.md) 형식으로 발급할 수 있게 자료를 넘긴다. 발급과 배포는 배포 담당(사용자)이 한다.
- 적용 후 스모크: 앱 부팅, `/db-test`, 로그인, `/users/me`, 정책 문서 조회, 발송 접수(Mock), 예약 목록

### 완료 조건

- 승인된 파일이 모두 적용되고 기록이 남아 있다.
- 사후 카탈로그가 기대 스키마와 일치한다.
- 백업에서 복원 가능함을 확인했다.
- 호환성 기록 자료가 배포 담당에게 전달되었다.

## 4. 16-7 공식 데이터 수집과 초기 데이터 구성

관련 Issue: [#116](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/116)

16-7은 시드 생성만이 아니라, 이미 구현된 공식 데이터 수집과 적재 기능을 재사용해 실제 기초 데이터를 넣고, 시연용 가상 데이터를 따로 구성하는 단계다. 새로운 수집 인프라(스케줄러, 큐, 별도 서버)는 만들지 않는다.

### 4.1 기존 코드(확인함)

| 코드 | 역할 | 실행 조건과 제한 |
| --- | --- | --- |
| `scripts/sync_regions.py`, `app/region_master.py` | 행정안전부 법정동코드 전체자료로 지역 마스터(`regions`)를 적재하고 갱신한다. 기본 preview(계산 후 롤백), `--apply`는 단일 트랜잭션, `--offline`은 DB 없이 변환만, `--report`로 JSON 보고서 | `DATABASE_URL`의 DB가 대상이다(localhost 제한 없음). 022가 먼저 적용되어야 한다. 활성 행의 10% 넘게 비활성화하면 `--allow-large-change` 없이는 거부한다. |
| `data/regions/legal_dong_code_full_20261123.zip`, `.json` | 공식 원본 zip(변경하지 않음)과 메타데이터. zip SHA-256 `44a3fe4c...6bacd`, 현존 20,560행. 변환 결과는 시도 16, 선택 가능한 시군구 255, 세종 전체 1 | 인증키 없이 받은 공개 자료다. |
| `app/public_data.py` | 농림축산검역본부 유기동물 API(APMS v2, `abandonmentPublic_v2`, 개 `upkind=417000`) | `fetch_page()`, `fetch_pages()`(최대 10페이지 x 100행), `synchronize(db, items, dry_run)`. `PUBLIC_API_KEY`가 없으면 503이다. 키와 원격 본문은 오류 메시지에 노출하지 않는다. |
| `scripts/sync_public_data.py` | 위 수집을 CLI로 실행한다. 기본 preview, `--apply`는 `pg_dump` 백업 후 적용 | `validate_local_engine()` 때문에 **localhost DB만** 가능하다. |
| `POST /public-api/fetch-dogs`, `GET /public-api/test`(`app/routers/public_api.py`) | 같은 수집을 API로 실행한다(`dry_run` 기본 true, `max_pages` 1~10, `rows` 1~100). | `X-Import-Key`(`PUBLIC_IMPORT_KEY`) 또는 ADMIN Bearer 토큰이 필요하다. 원격 DB에도 쓸 수 있는 기존 경로다. |
| `scripts/seed_demo.py` | `[DEMO]` 사용자 1명, 보호소 1개, 매칭 시연용 강아지. 자연 키(`desertion_no`)로 중복 방지, 기존 기록 미변경, preview 기본 | 지역, 담당자, 산책 설정, 일정은 없다. 바꾸지 않고 패턴만 따른다. |

### 4.2 공공데이터로 얻는 정보와 보호소가 직접 정하는 정보

| 구분 | 공공데이터에서 적재(현재 `synchronize()`) | 보호소 담당자나 운영자가 직접 설정 |
| --- | --- | --- |
| 보호소 | `name`(`careNm`), `external_code`(`careRegNo`), `phone`(`careTel`), `address`(`careAddr`) | `region_code`(활성 시군구, `PATCH /shelters/{id}/region`), 담당자 계정 연결(`PATCH /users/{id}/shelter`, ADMIN) |
| 강아지 | `desertion_no`, `breed`(`kindNm`), `age`(나이 문자열에서 계산), `gender`(`sexCd`), `image_url`(`popfile1`), `weight`(문자열), `process_state`, `neuter_status`, `external_data`(원본) | 산책 가능 여부(`walkable`), 크기(`size_code`), 숫자 난이도(`difficulty_score` 1~5, 0.5 단위)와 난이도 등급, 권장 시간(`recommended_duration_minutes`), 최대 시간(`max_walk_minutes`), 주의사항, 날짜별 일정 |
| 판단하지 않음 | 공공데이터에 없는 성향이나 산책 적합성은 추측하지 않는다(`trait_source="unknown"` 유지). 무게 문자열로 크기를 자동 단정하지 않는다. | |

### 4.3 작업 구분

1. **공식 지역 데이터 적재**: `sync_regions --input data/regions/legal_dong_code_full_20261123.zip`으로 preview → 보고서 확인 → `--apply`. 재실행하면 변경 0건이어야 한다.
2. **공공데이터 API 키 설정과 수집 검증**
   - `PUBLIC_API_KEY`(공공데이터포털 서비스 키)와 관리용 `PUBLIC_IMPORT_KEY`를 배포 호스트 `.env.api`에만 둔다.
   - 발급 담당과 호출 한도는 사용자와 정한다(미확정).
   - `GET /public-api/test`와 `dry_run=true` 수집으로 응답과 매핑을 확인한다.
3. **보호소와 강아지 기본 정보 수집과 DB 적재**
   - 로컬은 `sync_public_data.py`(백업 포함)로 한다.
   - 개발 DB는 `POST /public-api/fetch-dogs`를 `dry_run`으로 먼저 실행한 뒤 승인 후 적용한다. 개발 DB 백업은 16-6과 같은 절차로 먼저 확보한다.
   - 기존 충돌(보호소 식별 충돌, 코드 충돌, 고유번호 충돌 409)은 자동 병합하지 않고 보고한다.
4. **보호소 주소와 활성 지역 코드 연결**
   - 현재 주소를 지역 코드로 바꾸는 기능은 없다.
   - 권장: 시드 도구에서 `address`의 시도와 시군구 이름을 지역 마스터(`regions`)의 이름과 대조한다. 정확히 하나만 일치하면 연결 후보로 보고한다. 모호하거나 일치하지 않으면 목록만 남기고 사람이 `PATCH /shelters/{id}/region`으로 정한다.
   - 외부 주소 검색 API는 쓰지 않는다.
5. **가상 보호소와 강아지 테스트 데이터 구성**
   - 시연용 보호소와 강아지를 이름 접두어 `[TEST]`와 고유 키(예: `desertion_no`, `external_code`가 `TEST-`로 시작)로 실제 데이터와 구분한다.
6. **크기, 난이도, 권장과 최대 산책 시간 설정**
   - 가상 강아지에만 `PATCH /dogs/{id}/walk-settings` 규칙(또는 같은 서비스 함수 `app/dog_walk_settings.py`)으로 설정한다.
7. **날짜별 산책 가능 일정 생성**
   - 가상 강아지에만 미래 날짜 일정을 만든다(`slot_date`, `start_time`, `end_time`은 KST 현지 값, `duration_minutes`는 30, 60, 90 중 하나).
   - 과거 일정과 겹치는 일정은 만들지 않는다.
8. **계정과 권한 구성**
   - ADMIN 1명: 생성 API가 없으므로 시드 도구가 직접 만든다.
   - 보호소 담당자(SHELTER): 가상 보호소에 연결한다.
   - 일반 사용자: 온보딩 완료, 미완료, 나이 확인 대기 등 상태별로 만든다. Step 1 전화번호는 합성 번호이며, Mock SMS 정책(16-2)과 조율한다.
   - 비밀번호는 출력하거나 저장소에 남기지 않는다. 전달 방법은 미확정이다.
9. **정합성, 중복 방지, 재실행 검증**
   - FK 순서: 지역 → 보호소 → 담당자 → 강아지 → 설정 → 일정 → 계정 상태
   - 자연 키로 재실행 시 변경 0건
   - 제약 위반 없음
   - 실제 데이터 미변경(실제 보호소와 강아지의 `walkable`, 일정 행 수가 시드 전후로 같음)

### 4.4 확정 정책

- **실제 보호소와 강아지에는 산책 가능 여부나 일정을 임의로 부여하지 않는다.** 공공데이터로 들어온 강아지는 산책 설정이 없으므로 산책 가능 목록에 나타나지 않는다. 이 상태를 유지한다.
- 시연용 가상 데이터와 실제 데이터를 명확히 구분한다(접두어, 키, 별도 보호소).
- 합성 데이터만 테스트에 쓰고, 실제 사용자 데이터를 쓰지 않는다.
- 운영 환경(`APP_ENV=production`)에서는 가상 데이터 시드를 거부한다.

### 4.5 구현 방향

- 새 도구 `scripts/seed_dev_data.py`(권장 이름). 지역과 공공데이터 적재는 기존 도구와 API를 그대로 호출하는 단계로 안내하고, 이 도구는 주소와 지역 대조 보고, 가상 데이터, 계정만 다룬다.
- 기본 preview, `--apply`, 대상 DB 식별 확인, 운영 거부, 기존 기록 미변경(`seed_demo.py` 패턴)
- 서비스 함수(산책 설정, 일정, 담당자 연결)의 검증을 재사용해 정책을 우회하지 않는다.

### 4.6 테스트와 완료 조건

- **격리 PG**
  - 지역 적재 후 시드 preview, apply, 재실행 무변경, FK와 제약 위반 없음
  - 실제 데이터 미변경(공공데이터 형식의 합성 행을 넣고 시드 전후 비교)
  - 주소와 지역 대조의 일치, 모호, 불일치 보고
- **스모크**(가상 데이터): 목록 조회 → 신청 → 승인 → 코드 발급 → 시작 → 종료 → 인계
- **개발 DB 적용**은 16-6 이후 사용자 승인으로 한다. 지역, 공공데이터, 가상 데이터 순서로 적용하고 결과를 요약한다.

## 5. 16-8 Kakao 위치 판별 설정

관련 Issue: [#117](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/117)

### 현재 상태(확인함)

- **`app/location_geocoder.py`**
  - `KakaoLocationGeocoder`: `coord2regioncode` 호출, 3초 timeout, 응답 64KB 제한, 프록시와 리다이렉트 미사용
  - `MockLocationGeocoder`: `APP_ENV`가 local, development, test일 때만 쓸 수 있다.
  - `get_location_geocoder()`
- **API**: `POST /regions/resolve-location`(`app/routers/regions.py`의 `location_router`)
  - 미지원 지역은 422 `REGION_NOT_SUPPORTED`
  - 공급자 오류는 503 `GEOCODING_UNAVAILABLE`
- **설정**
  - `LOCATION_GEOCODING_PROVIDER=kakao`
  - `LOCATION_EXTERNAL_TRANSFER_ENABLED=true`(아니면 `transfer_disabled`)
  - `KAKAO_REST_API_KEY`
- **선행 결정**: [docs/issues/46/TODO.md](../46/TODO.md)에 위치정보 고지, 외부 제공 동의 범위, 키 발급 담당, 호출 한도가 미확정으로 남아 있다.

### 요구사항

- 사용자와 다음을 결정하고 기록한다(미확정): 시연 환경에서 Kakao 실제 호출 여부, 고지와 동의 처리, 키 발급 담당, 호출 한도
- 키는 배포 호스트의 `.env.api`(0600)에만 둔다. 저장소, Issue, 로그에 남기지 않는다.
- **검증**: 정상 조회(활성 지역 코드로 변환), 미지원 지역(422), timeout과 외부 오류(503), 키 누락과 `transfer_disabled`(503), 좌표를 로그나 캐시에 남기지 않음
- 코드 변경이 필요하면(예: 오류 분류 보강) 기존 Protocol을 유지하고 테스트를 추가한다. 실제 키를 쓰는 수동 검증은 사용자 승인 후 한다.

### 완료 조건

- 결정 사항과 설정 절차가 문서화되어 있다.
- 자동 테스트가 위 경우를 덮는다.
- 승인된 경우 테스트 키로 수동 검증을 했다.
- 16-3(환경변수 템플릿, 사용자 담당)에 필요한 변수 목록을 전달했다.

## 6. 16-9 서비스 정책 구성

관련 Issue: [#118](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/118)

### 현재 상태(확인함)

- **보호소 코드**(`app/shelter_code_service.py`, #87)
  - `SHELTER_CODE_HMAC_KEY`(32자 이상)가 필요하다.
  - 6자리 코드를 발급하고, 만료는 발급 10분 뒤와 예약 종료 중 빠른 시각이다.
  - 연속 5회 실패하면 5분 차단된다.
  - 발급은 `POST /walk-requests/{id}/shelter-code`(보호소 담당자와 관리자), 검증은 `.../verify`
- **레벨업**(`app/volunteer_level_service.py`, #97)
  - `VOLUNTEER_LEVEL_POLICY_VERSION`(64자 이하)과 `VOLUNTEER_LEVEL_POLICY_START_AT`(UTC ISO 8601)을 함께 설정해야 평가가 켜진다. 둘 다 비면 꺼진다.
  - 임계값은 `LEVEL_THRESHOLDS = (0, 3, 10, 20, 40)`이다.
  - 시작 시각 이후 시작하고 정상 인계를 자가 확인한 `COMPLETED/USER` 산책만 인정한다.
- **정책 문서**(`app/privacy_policy.py`, `policies/`, #76, #78)
  - **`policies/manifest.json`의 `documents`가 비어 있다.** 그래서 정책 문서 조회와 Step 3 필수 동의가 완료될 수 없고, **온보딩을 끝낼 수 없다**(핵심 흐름 차단).
  - 버전 설정: `TERMS_DOCUMENT_VERSION`, `PRIVACY_DOCUMENT_VERSION`, `PRIVACY_REQUIRED_CONSENTS`, `PRIVACY_OPTIONAL_CONSENTS`, `PLEDGE_CURRENT_VERSION`, `POLICY_DOCUMENTS_DIR`
  - 등록 절차는 [policies/README.md](../../../policies/README.md)에 있다.

### 요구사항

- **정책 문서**: 실제 이용약관과 개인정보 처리방침 문구는 AI가 작성하지 않는다.
  - 시연 목적의 문서를 쓸지, 승인된 문서를 받을지 사용자와 정한다(미확정).
  - 시연 문서라면 "시연용" 표시와 승인 기록을 남긴다.
  - manifest와 버전 환경변수가 일치하게 구성한다.
- **보호소 코드**: 키 생성과 보관 방법(배포 호스트 `.env.api`)을 정하고, 시드 데이터로 발급, 검증, 시작 흐름을 확인한다.
- **레벨업**: 정책 버전 이름과 시작 시각을 정한다(미확정). 시작 시각 이전 산책은 인정되지 않는다(기존 데이터는 바뀌지 않음). 백필(`scripts/backfill_volunteer_stats.py`)은 레벨을 바꾸지 않는 통계 재계산이며 필요 여부를 함께 정한다.
- 필요한 환경변수 목록과 값의 성격(비밀인지 여부)을 16-3 담당(사용자)에게 전달한다.

### 완료 조건

- 세 정책의 결정 사항, 설정 값의 출처, 검증 결과가 문서화되어 있다.
- 시드 환경에서 온보딩 완료, 코드 발급 후 시작, 레벨업 평가까지 확인했다.

## 7. 18~20단계 담당 범위

- **18 담당 기능별 검증**: 위 16-4~16-9 기능의 실제 DB 기준 검증. 데이터 보존, 백업과 복구 리허설, 시드 데이터 정합성, 위치와 정책 설정의 실패 경로를 포함한다.
- **19 배포 환경의 DB와 기능 검증**: 배포 후 스키마 기록과 실제 카탈로그 대조, 시드 상태, 정책 문서와 코드 발급, 레벨업 평가 동작을 확인한다.
- **20 DB와 기능별 개선**: 비차단 성능과 정리 항목. 예: 통계 집계 인덱스와 실행 계획, `FINISHED_WALK_STATUSES` 중복 정리(`docs/TODO.md`)
- 보안, 개인정보, 데이터 유실, 안전 신고, 핵심 기능 차단 문제는 20단계로 미루지 않는다.
