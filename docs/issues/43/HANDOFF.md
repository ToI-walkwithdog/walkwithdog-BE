# 인수인계: 행정구역 지역 마스터와 시도, 시군구 목록 조회 (Issue #43)

- 작성: 2026-11-23 (KST)
- 브랜치: `feature/regions-master` (`origin/dev` `0936933`에서 분기, 2026-11-24 `ae1b114` 병합 반영), [Draft PR #64](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/64) → `dev`
- 근거: [Issue #43](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/43), Notion [시도 지역 목록 조회](https://app.notion.com/p/420b7bbac8c7819691a9c52404a7ef63), [시군구 지역 목록 조회](https://app.notion.com/p/420b7bbac8c781b8b028e6bc5cf7c3d1)
- 갱신: 2026-11-24 마이그레이션 운영 정책(11절)과 #63 병합 후 manifest 정식 등록(12절) 반영. 기능 코드와 SQL은 바꾸지 않았다.
- 상태: `dev` 미병합, 개발 DB에 022 미적용, 지역 데이터 미적재. 개발 DB 적용은 전체 기능 개발과 PR 병합이 끝난 뒤 통합 마이그레이션으로 진행한다(11절).

## 1. 구현 범위

| 구분 | 내용 |
|---|---|
| 포함 | `regions` 테이블(022), 공식 법정동코드 변환과 적재 스크립트, 활성과 폐지 관리, 세종시 전용 코드, `GET /regions/sido`, `GET /regions/sigungu`, 지역 코드 검증 함수, 테스트 |
| 제외 | 산책 루틴과 사용자 지역 저장(#44), 현재 위치 판별, 보호소와 강아지의 지역 코드 연결(#29 등), 갱신 자동 실행 |

기존 보호소, 강아지 테이블과 #29 조회 로직은 바꾸지 않았다. `app/models.py`도 바꾸지 않았다.

## 2. 코드 체계

하나의 공식 체계(행정안전부 행정표준코드 법정동코드)만 쓴다. 행정동 코드와 역지오코딩 공급자 코드는 섞지 않는다.

| 구분 | 원본 법정동코드(10자리) | 저장 `region_code` | `parent_region_code` | `region_name` |
|---|---|---|---|---|
| 시도 | `1100000000` | `11` | 없음 | `서울특별시` |
| 시군구 | `1111000000` | `11110` | `11` | `종로구` |
| 구가 있는 시의 구 | `4111700000` | `41117` | `41` | `수원시 영통구` |
| 세종시 | `3611000000`(시도 행 없음) | `36` | 없음 | `세종특별자치시` |
| 세종시 전체 | 없음(내부 코드) | `36_SPECIAL_ALL` | `36` | `세종특별자치시 전체` |

- 원본 구조: `SS GGG DDD RR`. 시도 행은 `SS00000000`, 시군구 수준 행은 `SSGGG00000`(`GGG`가 `000`이 아님)이다. 읍면동과 리 행은 쓰지 않는다.
- `region_name`은 표시명이고 식별에 쓰지 않는다. 같은 이름(`중구` 등)은 코드와 상위 시도로 구분한다.
- 원본 이름 끝의 공백(`경기도 부천시 원미구 `)과 중복 공백은 정리한다.

### 중간 계층(구가 있는 시)

- 공식 자료에는 시 행(`41110 경기도 수원시`)과 그 구 행(`41111` 장안구, `41113` 권선구, `41115` 팔달구, `41117` 영통구)이 모두 "존재"다.
- 사용자가 고르는 최종 단위는 구다. 시 행은 선택 목록에 넣지 않고, 구는 `수원시 영통구`처럼 시 이름을 붙여 시도의 직접 하위로 둔다(테이블은 시도와 시군구 2단계).
- 판정 규칙: 5번째 자리가 `0`인 현존 시군구 행 C에 대해, 앞 4자리가 같고 5번째 자리가 `0`이 아니며 **이름이 C의 이름 + 공백으로 시작하는** 현존 행이 있으면 C는 중간 계층이다.
- 코드만으로 판정하면 틀린다. `43740 충청북도 영동군`과 `43745 충청북도 증평군`은 앞 4자리가 같지만 상하위 관계가 아니며, 둘 다 선택 가능한 군으로 변환된다(테스트 포함).
- 이름에 공백이 있는 시군구(`수원시 영통구`)인데 상위 시를 찾지 못하면 변환을 중단한다.
- 2026-11-23 자료의 중간 계층 시 13개: 수원, 성남, 안양, 부천, 안산, 고양, 용인, 화성, 청주, 천안, 포항, 창원, 전주. 화성시(`41590`)는 이 자료에서 만세구, 효행구, 병점구, 동탄구를 두고 있다.

### 세종특별자치시

- 공식 전체자료에 `3600000000` 행이 없다. 시군구 수준 행 `3611000000 세종특별자치시` 하나만 있다. 행정표준코드관리시스템의 시도 선택 목록은 `36 세종특별자치시`를 보여 준다.
- 그래서 `36`을 이 행에서 만든 시도로 저장하고, `36110` 행은 선택 대상으로 저장하지 않는다(`SINGLE_TIER_SIDO` 규칙).
- 시군구 조회는 `36_SPECIAL_ALL` 하나만 반환한다. 이 값은 숫자 법정동코드와 형식이 달라 충돌하지 않는다. DB CHECK는 `SPECIAL_SELECTABLE`을 이 코드와 상위 `36`으로만 허용한다.
- `36`은 시도라 #44 선택 코드가 아니고, `36_SPECIAL_ALL`은 선택 가능하다.
- 세종시 아래에 다른 현존 시군구가 생기면 변환을 중단해 정책을 다시 보게 한다.

## 3. 공식 데이터

| 항목 | 값 |
|---|---|
| 출처 | 행정표준코드관리시스템 법정동코드 전체자료 <https://www.code.go.kr/stdcode/regCodeL.do> |
| 받는 방법 | `POST https://www.code.go.kr/etc/codeFullDown.do`, form `codeseId=00002`. 인증키 없음 |
| 보관 파일 | `data/regions/legal_dong_code_full_20261123.zip` (내려받은 그대로) |
| 수집 시점 | 2026-11-23 14:19:26 UTC, KST 23:19:26 (다운로드 응답 `Date` 헤더) |
| 자료 기준 시점 | 확인 불가. 공식 사이트가 전체자료의 기준일을 표시하지 않는다 |
| 참고: zip 안 파일 수정 시각 | 2026-10-01 09:06:34 (시간대 표시 없음. 공식 기준일로 보지 않음) |
| zip SHA-256 | `44a3fe4cacf88ce98e35fb883807d2725b589f427ff4d73ee40b914d1286bacd` |
| 텍스트 SHA-256 | `962fd4cdfb6ccf2887b745eb97db55f71112b9f9a32c1f8fbd7692fb069b1df0` |
| 형식 | CP949, 탭 구분, 머리글 `법정동코드`, `법정동명`, `폐지여부`(`존재` 또는 `폐지`) |
| 건수 | 53,403행(존재 20,560, 폐지 32,843). 같은 시점 사이트 목록의 현존 건수 20,560건과 같다 |

- 메타데이터는 `data/regions/legal_dong_code_full_20261123.json`에 있다.
- 수집 시점과 자료 기준 시점은 다르다. 수집 시점은 우리가 내려받은 시각이고, 자료 기준 시점(공식 자료가 어느 날짜의 행정구역을 나타내는지)은 공식 사이트에 표시가 없어 확인하지 못했다. 파일 이름의 `20261123`은 수집 날짜다. 기준 시점 표기 방식은 미확정 정책이다.
- 전체자료에는 생성일, 폐지일, 상위코드 열이 없다. 사용자 검색 다운로드는 이 열을 줄 수 있지만 2만 건 제한이 있어 쓰지 않았다.
- 공공데이터포털 법정동코드 API(<https://www.data.go.kr/data/15077871/openapi.do>)는 `ServiceKey`가 필요하다(무료, 이용허락범위 제한 없음, 업데이트 주기 실시간 표기). 키 발급 없이 같은 파일을 다시 받을 수 있는 전체자료 다운로드가 재현성이 높아 이것을 기준으로 했다. API 키는 코드와 문서에 넣지 않았다.
- 이 자료에는 2026년 행정구역 변경이 반영돼 있다: `12 전남광주통합특별시` 신설, `29 광주광역시`와 `46 전라남도` 폐지, `42 강원도` → `51 강원특별자치도`, `45 전라북도` → `52 전북특별자치도`.

변환 결과: 활성 시도 16, 선택 가능한 시군구 255, 세종 전용 1, 합계 272행.

## 4. 적재와 갱신

- `migrations/022_regions.sql`: 테이블, 제약, 인덱스만 만든다. 데이터는 넣지 않는다.
- `app/region_master.py`: 파일 읽기와 검증, 변환(`build_master`), 변경 계획(`plan_changes`), 적용(`apply_changes`), 적용 후 검증(`master_problems`), 공식 파일 다운로드(`download_snapshot`)
- `scripts/sync_regions.py`: 실행 도구. 기본은 미리보기(DB와 비교만 하고 롤백)다.

```powershell
python -m scripts.sync_regions --input data/regions/legal_dong_code_full_20261123.zip --offline
python -m scripts.sync_regions --input data/regions/legal_dong_code_full_20261123.zip
python -m scripts.sync_regions --input data/regions/legal_dong_code_full_20261123.zip --apply --report region_sync_report.json
python -m scripts.sync_regions --download <폴더>
```

| 상황 | 처리 |
|---|---|
| 새 코드 | 추가 |
| 같은 코드의 이름 변경 | `region_name` 갱신 |
| 자료에 `폐지`로 표시 | `is_active=false`, 행 유지 |
| 시에 구가 새로 생김 | 시 코드 비활성(`INTERMEDIATE_CITY`), 구 추가 |
| 비활성 코드가 다시 `존재` | 다시 활성 |
| 같은 자료 재실행 | 변경 0건 |
| 마스터에 있는 코드가 자료에 아예 없음 | **중단.** 폐지가 아니라 자료 누락일 수 있다 |
| 활성 행의 10% 초과(최소 3건)를 비활성화 | 중단. 검토 후 `--allow-large-change` |
| 같은 코드의 수준이나 상위 시도가 바뀜 | 중단. 수동 검토 |
| 머리글, 코드 형식, 상태값, 중복, 인코딩 오류 | 중단 |
| 활성 시도 10개 미만, 선택 가능 시군구 150개 미만 | 중단(잘린 자료 방어용 하한, 정책값 아님) |
| 선택 가능한 하위 지역이 없는 활성 시도 | 중단 |

- 모든 검사를 통과하기 전에는 아무것도 쓰지 않는다. `--apply`는 한 트랜잭션이고 실패하면 전체 롤백된다.
- PostgreSQL에서는 갱신 중 `LOCK TABLE regions IN SHARE ROW EXCLUSIVE MODE`로 동시 갱신을 막는다. 조회 API는 막지 않는다.
- 보고서(JSON): 입력 파일 이름과 두 해시, zip 내부 시각, 행 수, 변환 요약(중간 계층 목록 포함), 추가, 이름 변경, 재활성, 비활성(사유 포함) 목록. `--report`로 파일에 저장한다. DB에 이력 테이블은 두지 않았다(TODO).
- 원본 폐지 코드는 처음 적재할 때 넣지 않는다. 마스터에 들어온 뒤 폐지된 코드만 비활성으로 남는다.

## 5. DB 모델

`migrations/022_regions.sql`, 모델 `app/region_models.py`의 `Region`

| 컬럼 | 타입 | 제약 |
|---|---|---|
| `region_code` | VARCHAR(20) | PK |
| `region_name` | VARCHAR(100) | NOT NULL, 공백만 불가 |
| `region_level` | VARCHAR(20) | NOT NULL, CHECK `SIDO`, `SIGUNGU`, `SPECIAL_SELECTABLE` |
| `parent_region_code` | VARCHAR(20) | FK `regions(region_code)`, NO ACTION |
| `is_active` | BOOLEAN | NOT NULL |

- `region_level`은 Issue 초안의 VARCHAR(16)이 아니라 VARCHAR(20)이다. `SPECIAL_SELECTABLE`이 18자라 16자로는 저장할 수 없다.
- `ck_regions_hierarchy`
  - `SIDO`: 상위 없음, 코드 2자리
  - `SIGUNGU`: 코드 5자리, 상위 = 코드 앞 2자리
  - `SPECIAL_SELECTABLE`: 코드 `36_SPECIAL_ALL`, 상위 `36`
  - 2자리 코드는 시도뿐이므로 FK를 통과한 상위는 항상 시도다.
- `ck_regions_code_digits`: 특별 코드 외에는 숫자만(PostgreSQL 전용. SQLite 테스트에서는 변환기가 검사)
- `ix_regions_parent_active (parent_region_code, is_active)`: 시군구 조회와 검증용. 전체 272행이라 다른 인덱스는 두지 않았다.
- 폐지 지역은 지우지 않는다. 하위가 있는 시도는 FK 때문에 지울 수 없다.
- 번호 022는 2026-11-23 모든 원격 브랜치와 열린 PR에서 쓰이지 않았다. 017~021과 의존 관계가 없다.

## 6. API 계약

두 API 모두 인증이 없고 `{code, data, message}` 형식이며 DB를 읽기만 한다. 응답에 `Cache-Control: no-store`를 붙인다.

### GET /regions/sido

```json
{"code": "OK", "data": {"regions": [{"region_code": "11", "region_name": "서울특별시"}, {"region_code": "12", "region_name": "전남광주통합특별시"}]}, "message": "시도 목록을 조회했습니다."}
```

| HTTP | code | 조건 | message |
|---|---|---|---|
| 503 | `REGION_DATA_UNAVAILABLE` | 활성 시도가 없음(미적재 또는 비정상) | 지역 목록을 불러올 수 없습니다. |
| 500 | `INTERNAL_SERVER_ERROR` | DB 오류(테이블 없음 포함) | 일시적인 오류가 발생했습니다. |

### GET /regions/sigungu?sido_code=41

```json
{"code": "OK", "data": {"sido_code": "41", "regions": [{"region_code": "41111", "region_name": "수원시 장안구", "sido_code": "41"}]}, "message": "시군구 목록을 조회했습니다."}
```

| HTTP | code | 조건 | data |
|---|---|---|---|
| 422 | `SIDO_CODE_REQUIRED` | 누락, 빈 값, 공백 | `{"field": "sido_code"}` |
| 422 | `INVALID_SIDO_CODE` | 숫자 2자리 아님(`1`, `111`, `11110`, `1a`, 전각 숫자, 특별 코드) | `{"field": "sido_code"}` |
| 404 | `REGION_NOT_FOUND` | 없는 코드, 비활성 시도, 시도가 아닌 코드 | `{}` |
| 503 | `REGION_DATA_UNAVAILABLE` | 마스터에 활성 시도가 없음, 또는 활성 시도에 선택 가능한 하위 지역이 없음 | `{}` |
| 500 | `INTERNAL_SERVER_ERROR` | DB 오류 | `{}` |

- 성공 코드는 기존 계약대로 `OK`다.
- 반환 행은 `parent_region_code = sido_code`, 수준 `SIGUNGU` 또는 `SPECIAL_SELECTABLE`, 활성만이다. 응답의 `sido_code`는 각 행의 상위 코드다.
- 시군구 503은 이번 구현의 선택이다. 적재기가 활성 시도마다 하위 지역을 1개 이상 보장하므로, 정상 마스터에서 "하위 지역 없음"은 생기지 않는다. 그래서 빈 목록은 비정상 마스터로 보고 503을 준다. 마스터가 비어 있을 때도 404가 아니라 시도 목록과 같은 503이다. Notion 시군구 명세에는 503이 없다(미확정 정책).
- 정렬: 두 목록 모두 `region_code` 오름차순(공식 코드 순)이다. SQLite와 PostgreSQL의 한글 정렬 차이가 없다. 정렬 정책은 미확정이다.

## 7. 서비스 함수 (`app/region_service.py`)

| 함수 | 용도 |
|---|---|
| `list_sido(db)` | 시도 목록. 없으면 `ContractError` 503 |
| `list_sigungu(db, sido_code)` | 시군구 목록. 형식 검사는 라우터가 한다 |
| `check_region_codes(db, codes, *, lock=False)` | 예외 없이 `RegionCheck(regions, problems)` 반환 |
| `require_selectable_regions(db, codes, *, lock=False)` | 선택 가능한 `Region` 목록(입력 순서). 아니면 `InvalidRegionCodes(problems)` |
| `require_selectable_region(db, code, *, lock=False)` | 코드 하나 |

- 선택 가능 조건: 존재, 수준 `SIGUNGU` 또는 `SPECIAL_SELECTABLE`, 활성, 상위 시도 활성
- 사유 상수: `INVALID_FORMAT`(문자열 아님, 빈 값, 20자 초과), `DUPLICATE`, `NOT_FOUND`, `NOT_SELECTABLE_LEVEL`(시도 코드), `INACTIVE`, `PARENT_INACTIVE`
- 문자열이 아닌 값은 `int:11110`처럼 타입을 붙인 키로 기록해 실제 코드와 섞이지 않는다.
- 세션을 받아 커밋하지 않는다. 호출한 트랜잭션 안에서 실행된다.
- `lock=True`(PostgreSQL): 선택한 행과 상위 시도를 `FOR SHARE`로 잠가, 호출 트랜잭션이 끝날 때까지 갱신 스크립트가 비활성화하지 못하게 한다.
- 개수 제한(1~3개)과 HTTP 오류 코드 변환은 호출하는 쪽(#44)의 책임이다.

## 8. 테스트 결과

실행 환경: Windows 11, Python 3.12 `.venv`, SQLite, 격리 PostgreSQL 16.16 테스트 DB(`REGION_TEST_DATABASE_URL`, 빈 `toi_walk_test_` DB, 테스트마다 `rg_<uuid>` 스키마)

| 실행 | 결과 |
|---|---|
| `tests/test_regions.py` | 26 passed, 42 subtests |
| `tests/test_regions_postgres.py` | 6 passed, 12 subtests, 3회 반복 통과, 테스트 DB 정리 확인 |
| `tests/` 전체 (두 테스트 DB 변수 설정) | 509 passed, 11 failed, 82 skipped |
| `origin/dev` `0936933` 기준(같은 설정) | 477 passed, 11 failed, 82 skipped |
| `dev` `ae1b114`(#63) 병합과 manifest 등록 후 `tests/` 전체 | 528 passed, 11 failed, 82 skipped. 늘어난 19건은 `tests/test_check_migrations.py`, 실패 목록은 위와 같음 |
| 보호소, 강아지 관련 4개 파일 | 64 passed, 2 failed(기존 산책 설정 테스트) |

- 실패 11건(하위 테스트 포함)은 `dev` 기준과 목록이 같다. 새 실패는 없고 통과는 32건(지역 테스트) 늘었다.
  - `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`: 이전부터 실패
  - `test_walk_domain.py` 3건: `dev`의 F-15 `shelter_volunteer_reviews` 모델이 007 SQL 계약 검사와 맞지 않음(`dev`에서 실패)
  - `test_signup_postgres.py` 마이그레이션 3건: `dev`에 새로 들어온 모델 컬럼이 마이그레이션만 적용한 스키마에 없어 로그인과 가입이 `ProgrammingError`(`dev`에서 실패)
  - 지역 기능과 무관하며 이 브랜치에서 고치지 않았다.
- 건너뛴 82건은 각자 빈 DB가 필요한 산책 PostgreSQL 테스트로 실행하지 않았다.
- Fixture(`FIXTURE`)는 테스트 전용 합성 행이다. 실제 전국 변환은 보관한 공식 zip으로 검증한다(해시 대조, 16, 255, 1, 중간 계층 13개).
- 변환: 규칙 전체, 실제 자료, 잘못된 파일 6종, 계층 불일치 7종
- 적재: 최초 적재와 재실행 0건, 미리보기 무변경, 이름 변경, 신규, 폐지 비활성과 재활성, 시에 구가 생기는 경우(화성시 형태), 시도 통합(광주 형태), 자료 누락 중단과 무변경, 대량 비활성 중단과 허용, 적용 후 실패 시 전체 롤백, 수준과 상위 변경 거부, DB 제약
- 검증 함수: 사유 6종, 입력 순서, 쓰기 없음
- API: 시도와 시군구 정상, 동명 지역 코드 구분, 세종 전용 항목, 비활성 제외, 422 9종, 404(없음, 비활성), 503(미적재, 하위 없음), 500(DB 오류), 미인증 성공, 조회 전후 테이블 동일, 같은 요청 동일 응답
- PostgreSQL: 022 적용과 재실행 무변경, 생성 객체가 `regions` 하나뿐, 제약 12종과 하위가 있는 시도 삭제 거부, 실제 자료 272행 적재와 재실행, 022만 적용한 테이블에서 API 동작, 실패 롤백과 누락 중단, 다른 테이블이 참조하는 폐지 지역 보존, `lock=True` 동안 다른 연결의 비활성화 대기
- 미실행: 개발 DB 적용과 적재, 공식 다운로드 함수의 자동 테스트(네트워크 필요. 이번 자료는 같은 요청을 수동으로 실행해 받음), FE 연동

## 9. 운영 갱신 절차 (주기는 미확정)

실제 DB에 실행하는 단계(3, 4)는 11절의 통합 마이그레이션 이후, 별도 승인을 받은 대상에만 실행한다.

1. 공식 파일 받기: `python -m scripts.sync_regions --download <폴더>`. 출력된 해시와 행 수를 기록한다.
2. 변환 확인: `--offline`으로 시도, 시군구 수와 중간 계층 목록을 이전 보고서와 비교한다.
3. 미리보기: 대상 DB에 `--input <zip>`. 추가, 이름 변경, 비활성 목록을 검토한다. 중단되면 원인을 확인한다.
4. 백업 후 `--apply --report <파일>`. 보고서를 보관한다.
5. 새 자료를 저장소에 보관할지(`data/regions/`) 팀이 정한다.

## 10. 미확정 정책

- [ ] 공식 자료의 기준 시점 표기(현재 수집 시점을 기록하고, 기준 시점은 확인 불가로 표시. zip 안 파일 시각은 참고값)
- [ ] 갱신 주기, 담당, 자동 실행 방식
- [ ] 중간 계층 표현(현재 구를 `수원시 영통구`로 시도 아래에 둠. 시 단위 선택 허용 여부)
- [ ] 목록 정렬 기준(현재 코드 순)
- [ ] 시군구 조회의 미적재, 비정상 응답(현재 503. Notion 시군구 명세에 없음)
- [ ] 기존 사용자 선택 코드가 폐지되거나 바뀔 때 이동 또는 재선택 정책(현재 비활성으로 보존만)
- [ ] 캐시 헤더(현재 `no-store`)
- [ ] Notion 공통 구현 기준의 "Bearer 인증 및 계정 상태 검증과 연계" 문구와 본문의 "인증 불필요" 충돌(현재 인증 없음)
- [ ] 세종 전용 코드 값 `36_SPECIAL_ALL`의 팀 확인
- [ ] 대량 비활성 기준 10%와 하한값(잘린 자료 방어용 안전장치)
- [ ] FE는 지역을 한 쌍만 고르고 BE는 1~3개를 받는 차이(#44와 함께)

## 11. 마이그레이션 운영 정책과 배포 대기 (2026-11-24 결정)

**모든 기능 개발과 관련 PR 병합이 끝난 뒤 개발 DB에 통합 마이그레이션을 적용한다.** 따라서 "개발 DB에 022를 적용해야 PR #64를 병합할 수 있다"는 조건은 없앴다.

| 시점 | 할 일 |
|---|---|
| PR 병합 전 | 격리 PostgreSQL에서 022 적용, 재실행, 지역 적재와 재적재를 검증한다(8절, 완료) |
| 실제 개발 DB 적용 | 전체 기능 개발 완료 후 별도 승인으로 진행한다. 022 적용과 `sync_regions --apply`가 여기에 포함된다 |
| 실제 DB 적용 전 | 공용 서버에 새 스키마를 요구하는 코드를 배포하지 않는다 |

- 이 코드를 022 없는 DB에 배포하면 두 지역 API가 500이다(테이블 없음). 022만 적용하고 적재하지 않으면 두 API가 503이다. 다른 API는 영향이 없다. 그래서 공용 서버 배포는 통합 마이그레이션과 적재 뒤로 미룬다.
- 022는 다른 마이그레이션과 SQL 의존성이 없다(`requires` 없음).
- 개발 DB 적용과 적재는 하지 않았다.
- 마이그레이션 번호: `dev`에는 017, 018 번호가 각각 두 파일이고, `019_first_walk_guide.sql`, `020_shelter_volunteer_reviews.sql`이 #50, #52의 019, 020과 같은 번호다. 022는 겹치지 않는다. 019, 020 충돌 정리는 #63의 관리 기준에 따라 온보딩 스택에서 다룬다.

## 12. 마이그레이션 관리 PR #63과의 통합

- [PR #63](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/63)은 2026-11-23 15:04 UTC에 `dev`로 병합됐다(`ae1b114`). 병합 전 manifest는 `022_regions.sql`을 예약 항목(`state: unmerged`, 출처 PR #64, `requires: []`)으로 두고 있었다.
- 이 브랜치의 처리(2026-11-24)
  1. 최신 `dev`(`ae1b114`)를 일반 병합으로 반영했다. 충돌은 없었다.
  2. `migrations/manifest.json`에서 `022_regions.sql` 예약을 지우고 정식 `migrations` 항목으로 등록했다. 다른 예약 4개(`010`, `019`, `020`, `021`)는 그대로 두었다.

| 필드 | 값 | 근거 |
|---|---|---|
| `sha256` | `15502297302ef579b5a6b5e1f4faceefd8233a7aba5067cfedebb3e3dbd1d29f` | `scripts/check_migrations.py`의 `checksum()`(CRLF만 LF로 통일) |
| `requires` | `[]` | 022는 `regions`만 만들고 다른 SQL의 객체를 쓰지 않는다 |
| `transaction` | `file` | 파일 안에 `BEGIN`/`COMMIT`이 있다(`018_user_consents.sql`과 같은 방식) |
| `environments` | `local-test`, `dev`, `production` | 기존 항목과 같다 |
| `prechecks` | `[]` | 새 테이블만 추가하므로 기존 데이터 사전 검사가 없다 |

- 검증 결과
  - 등록 전 병합 후보: `python scripts/check_migrations.py`가 종료 코드 1(`BLOCKED FUTURE MIGRATION: reserved number 022`)
  - 등록 후: 기본 검사와 `--check-reservations-for 022_regions.sql` 모두 종료 코드 0
  - 출력에 남는 `019`, `020`의 `BLOCKED FUTURE MIGRATION`은 온보딩 스택(#50, #52)의 기존 번호 충돌이고 이 PR과 무관하다.
  - `tests/test_check_migrations.py`도 통과했다(8절 전체 결과에 포함).
- SQL을 바꾸면 체크섬을 다시 계산해 manifest를 고쳐야 한다. 병합 직전에는 최신 `dev`에서 같은 검사를 다시 실행하고, 실제 병합 뒤 `dev`에서도 한 번 더 실행한다(#63 문서의 절차).

## 13. #44 연동

```python
from app.region_service import InvalidRegionCodes, require_selectable_regions

def save_routine(db, user_id, region_codes, ...):
    if not 1 <= len(region_codes) <= 3:
        raise ...  # REGIONS_REQUIRED / REGION_LIMIT_EXCEEDED (#44)
    try:
        regions = require_selectable_regions(db, region_codes, lock=True)  # 같은 트랜잭션
    except InvalidRegionCodes as exc:
        raise ContractError(422, "INVALID_REGION_CODE", ..., {"field": "region_codes"})
    # volunteer_routine_regions(user_id, region_code) FK → regions.region_code
```

- `lock=True`는 저장 트랜잭션 안에서 부르고, `users` 행 잠금(온보딩 규칙) 다음에 둔다.
- `volunteer_routine_regions.region_code`는 `regions(region_code)` FK다. 폐지 지역은 지워지지 않으므로 저장된 참조가 깨지지 않는다.
- 응답에 지역 표시명을 넣지 않는다(#44 계약). FE는 이 두 API의 이름을 쓴다.

## 14. #41 접근 제어 테스트와의 통합

- #41 접근 제어 자동 테스트(`tests/test_age_review_access.py`, PR #53, #57)는 목록에 없는 경로를 `detail` 차단 경로로 간주한다. 이 브랜치가 `dev`에 들어간 뒤 온보딩 스택과 합치면 두 지역 경로가 403을 주지 않아 그 테스트가 실패한다. 통합할 때 두 경로를 `PUBLIC` 목록에 넣어야 한다. 공개가 의도한 정책이고, 연령 확인 필요 계정도 인증 없이 조회한다.
- 자동 분류에 맡기지 않고 두 경로를 `PUBLIC`에 명시 등록한다. 그 뒤 연령 확인 필요 계정으로 두 API를 호출하는 통합 테스트를 확인한다.
- 이 브랜치에서는 두 API가 인증 헤더 없이 200인지 테스트했다. #53, #57 브랜치는 수정하지 않았다.

## 15. 관련 링크

- Issue #43, 후행 Issue #44, PR #64, 마이그레이션 관리 PR #63
- Notion: [시도 지역 목록 조회](https://app.notion.com/p/420b7bbac8c7819691a9c52404a7ef63), [시군구 지역 목록 조회](https://app.notion.com/p/420b7bbac8c781b8b028e6bc5cf7c3d1), [온보딩 전체 설계](https://app.notion.com/p/41fb7bbac8c7809b914bcf3e80d9d876)의 "#43 지역 마스터 구현 결과" 절
- 공식 자료: [행정표준코드관리시스템 법정동코드](https://www.code.go.kr/stdcode/regCodeL.do), [공공데이터포털 법정동코드 API](https://www.data.go.kr/data/15077871/openapi.do)
