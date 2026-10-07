# 설문 / 매칭 API

> 로그인·본인 접근 제한이 추가되었습니다. 설문·매칭·시뮬레이션 호출에 Bearer
> 토큰이 필요합니다. [회원가입·로그인 순서와 정책](auth-api.md)을 먼저 확인하세요.
> 기존 비밀번호 없는 시연 계정은 로그인할 수 없으며, 새 계정을 등록해 본인 ID로 시연합니다.
>
> 시연 강아지는 이제 기본 추천에서 제외합니다. 전용 시연에서만 `MATCHING_INCLUDE_DEMO=true`로
> 포함할 수 있습니다. 성향 시각화·수집·대화·시뮬레이션 API는 [추가 API 문서](backend-data-ai-api.md)를 참고하세요.

## 실행 전: 기존 PostgreSQL DB 마이그레이션

업데이트된 서버를 시작하기 **전에** `migrations/001_matching_snapshot.sql`을
기존 DB에 적용해야 합니다. `Base.metadata.create_all()`은 기존 테이블의 컬럼을
추가하지 않습니다. DB 백업 후, 사용 중인 DB 관리 도구에서 SQL을 실행하거나:

```sh
psql --dbname '<database-url>' --file migrations/001_matching_snapshot.sql
```

백업·구조 검증을 포함한 자동 적용 절차는 아래 '로컬 API 시연'을 참고하세요. 신규 DB는
현재 SQLAlchemy 모델로 생성할 수 있습니다. 기존 매칭 이력은 삭제하지 않으며,
어떤 설문으로 만들어졌는지 알 수 없으므로 기존 행의 `survey_id`는 NULL로 둡니다.
신규 컬럼은 nullable이어서 기존 서버 코드도 읽을 수 있습니다. 롤백 시에는
서버 코드만 되돌리고 컬럼을 유지하면 새 분석 이력을 보존할 수 있습니다.

## 선택값

`GET /surveys/options`에서 각 필드의 `{code, label}` 목록을 조회할 수 있습니다.
프론트엔드는 label을 화면에 표시하고 code를 전송하세요. OpenAPI에도 Enum이
표시됩니다. 코드 목록은 `app/survey_codes.py`에 정의되어 있습니다.

| 필드 | 코드 |
| --- | --- |
| housing_type | studio, apartment, house_with_yard, house_without_yard |
| family_type | single, couple_without_children, young_children, school_children, adult_children, senior, multigenerational |
| outing_frequency | mostly_home, 1_to_2_days, 3_to_4_days, daily |
| life_pattern | remote_worker, office_worker, irregular_worker, student |
| walk_frequency | daily, 3_to_4_per_week, 1_to_2_per_week |
| walk_duration | under_20_minutes, 20_to_40_minutes, 40_to_60_minutes, 60_minutes_or_more |
| dog_experience | first_time, previous_owner, current_owner |
| mbti | 16개 MBTI 코드 (대문자) |

기존 한글 선택값도 허용합니다. 공백과 `0-6세` / `0~6세` 같은 범위 표기 차이는
정규화합니다. 신규 설문은 DB에 코드로 저장하고 응답도 코드로 반환합니다.
기존 DB의 한글 값은 변경하지 않지만 읽을 때 알려진 값은 코드로 반환합니다.
알 수 없는 과거 문자열은 조회 시 보존하고 점수 계산에는 사용하지 않습니다.
새 요청의 알 수 없는 선택값/MBTI는 422입니다.

`daily_walk_time`은 **하루 산책 가능 시간(시간 단위 숫자, 0~24)**입니다.
`0.5`는 30분, `1.5`는 90분입니다. 기존 `"30분"`, `"1.5시간"`도 허용하고
숫자로 반환합니다. `walk_duration`은 **1회 산책 가능 시간**의 범주 코드입니다.
기존처럼 선택항목은 생략/NULL이 가능하며 필수항목 확대는 하지 않았습니다.

## 호출 흐름

1. `GET /surveys/options` → 선택지 표시
2. `POST /surveys/` → 설문 저장, 응답의 `id` 확보
3. `POST /matching/{survey_id}` → 상위 최대 5마리와 결과 화면 데이터
4. `GET /matching/history/{user_id}` → 같은 구조의 저장된 결과

설문 요청 예시:

```json
{
  "user_id": 1,
  "housing_type": "studio",
  "family_type": "single",
  "outing_frequency": "daily",
  "daily_walk_time": 0.5,
  "life_pattern": "office_worker",
  "walk_frequency": "3_to_4_per_week",
  "walk_duration": "20_to_40_minutes",
  "dog_experience": "first_time",
  "mbti": "INFJ"
}
```

매칭 생성/이력 조회는 배열을 유지하면서 다음 필드를 제공합니다:

- 기존 `id`, `user_id`, `dog_id`, `score`, `reason`, `detail_analysis`
- 추가 `survey_id`, `label`, `warnings`, `care_points`, `score_breakdown`
- `dog`: 상세 정보, 5차원 성향, `shelter`의 이름·전화·주소·홈페이지

`dog.shelter`는 연결된 보호소가 없으면 NULL입니다. 건강검진/중성화 여부,
거리·좌표 등 아직 수집하지 않는 정보는 만들지 않았습니다.

`POST /matching/score`는 호환용으로 유지하며 최신 설문으로 개별 강아지의
점수를 계산합니다. 생성 API와 동일한 분석 함수를 사용하지만 저장하지 않습니다.
같은 생성 요청의 재호출은 저장된 결과/ID를 반환하므로 중복 이력이 생기지 않습니다.
새 설문 ID로 요청하면 새로운 이력을 남깁니다. 설문별 결과 저장은 한 트랜잭션이며,
DB의 `(survey_id, dog_id)` 유일성으로 중복을 방지합니다. 동점은 dog_id 순입니다.

신규 분석은 점수·이유·주의점·관리 포인트·점수 구성의 스냅샷으로 저장합니다.
이력의 분석은 나중에 설문/강아지 성향이 변경되어도 유지합니다.
강아지 기본정보/보호소 정보는 현재 정보를 보여줍니다.
기존 이력은 저장된 점수/설명을 유지하고 `survey_id: null`, 주의점/관리 포인트는
빈 배열, `score_breakdown: {}`로 반환합니다. 최신 설문으로 재해석하지 않습니다.

## 점수 구성

기존 매칭 가중치를 유지하되 한글 문자열 비교를 Enum 비교로 바꿨습니다.
`daily_walk_time`은 부분 문자열이 아니라 수치 구간으로 계산합니다
(0.5~1시간 미만 / 1~2시간 미만 / 2시간 이상).
예전 코드에서 `"30분"`의 `3`을 3시간처럼 해석하던 오류도 해결했습니다.

`score_breakdown`은 기존의 값 유무에 따른 고정 숫자 대신 **실제 가감점**입니다.
`base_score`(50), `housing_match`, `family_match`, `outing_match`,
`life_pattern_match`, `walk_match`, `experience_match`, `personality_match`를
포함하며 음수도 가능합니다. 최종 score는 합계를 0~100으로 제한합니다.
기존 가중치가 없던 학생/성인 자녀 가구에 임의의 가중치를 추가하지 않았습니다.

인증/본인 접근 제한은 별도 후속 작업입니다. 현재 user_id 기반 API를
유지했으므로 이 변경만으로 실제 사용자 서비스의 인증이 갖춰지지는 않습니다.

## 테스트

```sh
venv/bin/python -m unittest discover -s tests -v
```

추가 테스트 패키지는 필요하지 않습니다. 설치된 FastAPI, SQLAlchemy, httpx와
표준 unittest를 사용합니다. 테스트는 독립 FastAPI 앱과 메모리 SQLite DB를 만들며,
`app.main`의 시작 시 DB 생성이나 실제 `.env` DB 연결을 실행하지 않습니다.
Enum/한글 호환, 입력 검증, 수치 시간 계산, 점수 구성, 분석 응답, 404/422,
중복 방지/충돌 롤백, 이력 스냅샷, 기존 데이터 호환, 시뮬레이션 경고를 검증합니다.
현재 개발 PostgreSQL의 실제 적용·동시 요청 검증 결과는 `docs/demo-verification.md`에 기록했습니다.

## 로컬 API 시연

인증이 없는 서버이므로 로컬에서만 실행하세요. 기존 API 경로/응답 구조를 바꾸거나
외부에 배포하지 않습니다. 작업 디렉터리는 `backend`입니다.

### 1. DB 조회 → 백업 → 마이그레이션

```sh
venv/bin/python -m scripts.prepare_demo_db
venv/bin/python -m scripts.prepare_demo_db --apply
```

첫 명령은 읽기 전용이며 연결 대상(host/port/database), 현재 구조, 테이블별 행 수를
보여줍니다. 비밀번호·DB URL·레코드 내용은 출력하지 않습니다. 도구는 명시적인
로컬 PostgreSQL 주소만 허용합니다. 연결 대상이 개발 DB인지 출력에서 확인하세요.

`--apply`는 미적용 상태에서만 백업을 만들고 마이그레이션합니다. 진행 중인 DB 쓰기를
멈춘 후 실행하세요. 백업은 `.demo-backups/`에 custom-format으로 저장되며 파일 권한은
600, 새 디렉터리 권한은 700입니다. Git에서는 제외됩니다. `pg_dump` 성공·파일 크기·
`pg_restore --list` 성공을 확인한 뒤에만 적용합니다. 이는 전체 복원 테스트를 뜻하지 않습니다.
백업 실패/시간 초과는 DB 변경 없이 중단합니다. 백업 원문 오류는 비밀정보 보호를 위해 숨깁니다.

기존 컬럼/외래키/인덱스 구조가 다르면 자동 수정하지 않습니다. 이미 적용된 상태에서
재실행하면 `applied: false`, `schema.ready: true`이며 추가 백업이나 DB 변경이 없습니다.
마이그레이션은 잠금과 한 트랜잭션 안에서 검증하며, 잠금 대기 또는 행 수 검증 실패 시
롤백합니다. 보존된 백업을 현재 DB에 자동 복원/덮어쓰기하지 않습니다.

### 2. 시연 데이터 준비

```sh
venv/bin/python -m scripts.seed_demo
venv/bin/python -m scripts.seed_demo --apply
venv/bin/python -m scripts.seed_demo --apply
```

첫 명령은 생성 예정 수만 조회합니다. `--apply`는 가상 사용자 1명, 보호소 1곳,
서로 다른 성향의 강아지 6마리를 한 트랜잭션으로 생성합니다. `[DEMO]` 이름과
`DEMO-API-*` 식별자로 표시됩니다. 사용자 비밀번호는 NULL이며 실제 로그인 계정이
아닙니다. 보호소 전화번호/주소도 가상입니다. 이미 있는 시연 행은 수정하지 않고
재사용합니다. 식별자가 다른 기존 데이터와 충돌하면 중단합니다.

결과의 `user_id`, `shelter_id`, `dog_ids`를 사용하세요. 현재 검증 환경의 시연 사용자
ID는 3이지만 다른 DB에서도 3이라고 가정하면 안 됩니다. 두 번째 적용 결과의
`planned_creates`는 모두 0이어야 합니다. 추천 후보에는 기존 강아지도 포함됩니다.
시연 강아지 순위를 강제로 높이지 않으며 가상 데이터는 실제 입양에 사용하면 안 됩니다.

### 3. 서버와 Swagger 실행

```sh
venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Swagger: `http://127.0.0.1:8000/docs`. 포트가 사용 중이면 8001 등 빈 포트로
실행하고 아래 자동 시연 명령의 `--base-url`도 동일하게 지정하세요.

Swagger에서 순서대로 실행합니다:

1. `GET /surveys/options`: 각 항목의 code/label 확인
2. `POST /surveys/`: 위 요청 JSON의 user_id만 준비 결과의 ID로 바꿔 저장
3. `POST /matching/{survey_id}`: 2번 응답의 id 사용, 최대 5개 결과 확인
4. 같은 매칭 요청 재실행: 결과 ID·점수·설명이 같은지 확인
5. `GET /matching/history/{user_id}`: 이번 설문의 결과가 중복 없이 존재하는지 확인
6. 새 설문에서 `housing_type: house_with_yard`, `outing_frequency: mostly_home`,
   `life_pattern: remote_worker`, `daily_walk_time: 2`, `walk_frequency: daily`,
   `walk_duration: 60_minutes_or_more`로 바꿔 저장·매칭 후 이전 이력도 확인

새 설문 ID는 새 분석 이력을 남기지만 같은 ID의 재호출은 저장된 결과를 반환합니다.
결과의 score/label/reason/warnings/care_points/score_breakdown, dog의 5차원 성향과
shelter 정보를 확인하세요. 보호소 없는 기존 강아지가 추천되면 shelter는 NULL일 수 있습니다.

### 4. 자동 시연

```sh
venv/bin/python -m scripts.demo_api --user-id 3 --check-concurrency
```

3은 이번 환경의 준비 결과입니다. 다른 환경에서는 출력된 user_id로 바꾸세요.
기본 주소는 `http://127.0.0.1:8000`, 요청 시간 제한은 10초입니다.
`--base-url http://127.0.0.1:8001`, `--timeout 20`으로 변경할 수 있습니다.
로컬 주소만 허용하며 인증정보 포함 URL은 거부합니다.

`--check-concurrency`는 첫 매칭을 두 요청으로 동시에 실행하고 결과가 동일한지
검증합니다. 옵션을 생략하면 첫 매칭을 순차 실행합니다. 매 실행마다 설문 2개와
각각 최대 5개 매칭 이력을 남기며, 생성한 데이터를 자동 삭제하지 않습니다.
반복 실행은 새 설문을 만드는 것이므로 전체 이력이 증가하는 것이 정상입니다.
이전 시연 이력 수를 가정하지 않고 이번 응답 ID로 분석 보존과 중복 여부를 검증합니다.
실패 단계·상태코드를 출력하고 즉시 중단하며 실패는 종료 코드 1, 성공은 0입니다.
실패 전에 성공한 설문/매칭 요청은 DB에 남습니다. 동일한 매칭 요청은 안전하게 재호출할 수 있습니다.

### 문제 해결

- DB 연결 실패: 로컬 PostgreSQL 실행 상태와 `.env`의 DATABASE_URL, 포트, 접근 권한 확인
- 백업 실패: pg_dump/pg_restore 설치·서버 버전 호환성·백업 디스크 용량·권한 확인
- 미적용 마이그레이션/컬럼 없음: 서버를 멈추고 prepare_demo_db 조회·적용 후 재시작
- 구조 불일치: 자동 수정을 하지 말고 해당 컬럼/외래키/인덱스를 DB 관리 도구에서 확인
- 매칭 404: 설문 ID가 맞는지와 강아지 존재 여부 확인, seed_demo --apply로 가상 데이터 준비
- 422: /surveys/options의 코드 사용, MBTI 16개 코드와 산책 시간 0~24시간 범위 확인
- API 연결 실패: 서버와 --base-url의 포트 일치 여부 확인

강아지 삭제나 DB 초기화로 실패 상황을 만들지 마세요. 이런 테스트는 메모리 SQLite 등
격리 환경의 자동 테스트에서만 수행합니다.
