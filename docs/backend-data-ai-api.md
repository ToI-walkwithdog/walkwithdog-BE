# 백엔드 데이터·성향·AI·시뮬레이션

프론트엔드 없이 Swagger와 CLI에서 검증하는 단계입니다. 외부 공개·배포는 하지 않습니다.

## 1. PostgreSQL 로그인 검증

002 마이그레이션은 개발 DB를 백업한 뒤 적용했습니다. 실제 PostgreSQL에서
회원가입·로그인·본인 접근·타인 접근 차단·매칭 재호출·로그아웃을 검증했습니다.
검증용 계정·설문·매칭·대화 데이터는 바깥 트랜잭션을 롤백해 남기지 않습니다.
시퀀스 번호는 PostgreSQL 특성상 롤백 후 건너뛸 수 있습니다.

```sh
venv/bin/python -m scripts.migrate_backend 002
venv/bin/python -m scripts.migrate_backend 002 --apply
venv/bin/python -m scripts.verify_auth_postgres
```

로그인 사용법은 [auth-api.md](auth-api.md)를 참고하세요.

## 2. 공공데이터 수집·갱신

출처: [국가동물보호정보시스템 구조동물 조회 서비스](https://www.data.go.kr/data/15098931/openapi.do).
공식 Swagger 명세에서 v2 주소·개 축종코드·페이지 옵션을 확인했습니다.

```text
https://apis.data.go.kr/1543061/abandonmentPublicService_v2/abandonmentPublic_v2
```

003은 `dogs`의 외부 원본·동기화 시각·보호 상태·중성화 코드와 보호소 외부 코드를
추가합니다. 적용 전 백업을 검증하고 적용 후 구조·기존 행 수를 확인합니다.

```sh
venv/bin/python -m scripts.migrate_backend 003 --apply
venv/bin/python -m scripts.sync_public_data --max-pages 1 --rows 5
venv/bin/python -m scripts.sync_public_data --max-pages 1 --rows 5 --apply
```

CLI는 기본 미리보기, `--apply`에만 백업 후 저장입니다. 이번 개발 DB에는 실제 5건을
추가했고 같은 범위를 다시 확인하여 신규 0건·갱신 대상 5건임을 검증했습니다.
전체 전국 데이터를 가져온 것은 아닙니다. `truncated=true`이면 지정한 범위 밖에 데이터가 남아 있습니다.

Swagger `POST /public-api/fetch-dogs`도 기본 미리보기입니다. `.env`의
`PUBLIC_IMPORT_KEY`를 충분히 긴 별도 관리 비밀값으로 설정하고 `X-Import-Key`에
입력하세요. 일반 로그인 토큰으로 수집 관리 권한을 주지 않습니다.

역할 병합 후에는 `admin` 역할의 Bearer 토큰으로도 수집·진단 API를 사용할 수 있습니다.
일반 사용자와 보호소 담당자의 토큰은 403입니다. `X-Import-Key`를 보낸 경우에는 반드시
그 키를 검증하며, 잘못된 키를 관리자 토큰으로 우회하지 않습니다. 두 경로 모두 기본은 미리보기입니다.

```json
{"dry_run":true,"max_pages":2,"rows":20}
```

- 모든 지정 페이지를 받아 성공한 뒤 하나의 DB 트랜잭션으로 저장합니다.
- 외부 유기번호 기준으로 신규 저장/갱신합니다. PostgreSQL advisory lock과 유일성 제약으로 수집을 직렬화합니다.
- 기존 설명의 유기번호가 실제 응답과 정확히 일치하고 충돌이 없을 때만 기존 행을 재사용합니다.
- 중복 식별자·보호소 충돌은 자동 병합/삭제하지 않고 409로 중단합니다.
- 기존 관찰 성향·설명·특이사항·사용자·설문·매칭 분석은 덮어쓰지 않습니다.
- 새 강아지의 알려지지 않은 성향은 3이 아니라 NULL입니다.
- 응답에 요청 전체 URL·서비스 키·외부 오류 본문을 반환하지 않습니다.
- 종료된 동물, 확인된 보호자 반환 문구, 시연 데이터는 신규 추천에서 제외합니다.
- 보호중/상태 미상은 입양 가능 확정이 아닙니다. 입양 가능 여부는 보호소 확인이 필요합니다.
- 기존 매칭 이력은 재해석하지 않습니다. 과거 시연 데이터 결과도 그대로 남습니다.

`MATCHING_INCLUDE_DEMO=true`는 전용 로컬 시연/테스트에서만 사용하세요.
자동 일정 실행·전국 전체 수집·오래된 상태 자동 판정은 이번에 추가하지 않았습니다.

## 3. 성향 시각화 데이터

`GET /dogs/{dog_id}/personality`: 공개 강아지 정보와 동일하게 로그인 없이 조회합니다.

- `axes`: 활동량·분리불안·친화력·훈련성·독립성 다섯 축, 1~5 범위.
- `value=null`, `available=false`: 없거나 범위 밖인 값. 0 또는 3으로 대체하지 않습니다.
- `source`, `estimated`: 출처와 추정 여부. `shelter_observed`만 관찰값으로 구분합니다.
- `keywords`: 고정 성향 문구·기존 성향 키워드의 빈도 기반 워드클라우드 데이터. AI 생성 결과가 아닙니다.
- `missing_fields`: 추가 평가가 필요한 성향 목록.
- 분리불안은 높을수록 불안 경향이며 좋은 성향 점수가 아닙니다. 축별 `interpretation`을 사용하세요.

기존 `breed_default` 값의 진위를 이 API가 새로 검증하지는 않습니다. 보호소 관찰 데이터 확보는 여전히 필요합니다.

## 4. AI 페르소나 대화

004 대화 테이블을 백업 후 적용했습니다. [Gemini 공식 REST 명세](https://ai.google.dev/api/generate-content)를
사용하며 `gemini-3-flash-preview` 실제 생성 응답까지 포함한 PostgreSQL 흐름을 검증했습니다.
검증용 계정·설문·대화는 롤백했습니다.

```sh
venv/bin/python -m scripts.migrate_backend 004 --apply
```

`.env`의 `GEMINI_API_KEY`와 현재 검증된 `GEMINI_MODEL=gemini-3-flash-preview`를 사용합니다.
Preview 모델은 변경·종료될 수 있으므로 배포 전에 모델 목록과 실제 생성 요청을 다시 검증하세요.
비밀값을 채팅·문서·커밋에 넣지 마세요. 설정이 없으면 503이며 고정 답변으로 대체하지 않습니다.

1. 로그인 후 Swagger Authorize.
2. 본인 설문 ID와 강아지 ID로 `POST /chat/sessions`.

```json
{"survey_id":1,"dog_id":1,"consent_to_external_ai":true}
```

위 숫자는 예시입니다. 본인 응답 ID를 사용하세요. 동의는 대화 내용과 해당 강아지의
기본 정보·성향·정규화된 생활조건 및 최근 대화가 외부 Gemini로 전송된다는 뜻입니다.
사용자 이메일·닉네임·DB 계정 ID는 서버 컨텍스트에 포함하지 않습니다. 메시지의
이메일·휴대전화·주민번호·카드번호·일반적인 API 키/토큰 패턴은 외부 전송과 DB 저장 전에
삭제 표시로 바꾸고 응답의 `pii_redacted`로 알립니다. 자유 형식 이름·주소 등 모든 개인정보를
완벽히 탐지하지는 못하므로 민감정보를 입력하지 마세요.

3. 반환된 세션 ID로 `POST /chat/sessions/{session_id}/messages`.

```json
{"message":"산책하는 하루는 어떨까?"}
```

4. `GET /chat/sessions/{session_id}/messages`로 본인 이력 확인.

세션 시작 시점의 성향·생활조건을 보존하므로 새 설문이나 강아지 정보 갱신이 기존
대화 컨텍스트를 바꾸지 않습니다. 타인 세션은 404입니다. 요청 실패 시 사용자/AI
메시지는 모두 저장하지 않습니다. 메시지는 1,000자, 세션은 20회, 외부 전송 이력은
최근 10개 메시지로 제한합니다. 동일 세션 요청은 PostgreSQL 행 잠금으로 순서를 보존합니다.
토큰·키·외부 오류는 반환하지 않습니다. 타임아웃 504, 실패/차단/비정상 응답 502입니다.

가상 표현임을 고지하고 건강·검사·입양 확정 사실을 만들어내지 않도록 지시합니다.
프롬프트만으로 모델의 사실성을 완전히 보장할 수는 없습니다. 실제 응답 평가·비용 제한·
사용자별 속도 제한·운영 개인정보 처리 정책 검토 후에 공개해야 합니다.
[실제 합성 데이터 평가 결과와 남은 위험](ai-safety-evaluation.md)을 함께 확인하세요.

## 5. 시뮬레이션 보강

기존 `GET /simulation/timeline?user_id=...&dog_id=...`를 유지합니다.
`survey_id`를 추가하면 본인의 특정 설문을 사용하고 생략하면 최신 설문을 사용합니다.
타인 설문은 404입니다.

기존 `timeline`, `warning` 필드를 유지하고 다음을 추가했습니다.

- `survey_id`, `daily_walk_minutes`, 성향 출처·추정 여부·누락 목록.
- `event_id`, `animation`, `interaction_options`: 2D 클라이언트가 사용할 안정적인 이벤트 코드.
- `is_simulation`, `notice`: 실제 행동 예측이 아닌 가상 생활 시나리오임을 표시.

`POST /simulation/interaction`은 상태를 저장하지 않는 행동 선택 미리보기입니다.

```json
{"survey_id":1,"dog_id":1,"time":"daytime","action":"enrichment"}
```

해당 시간대에 허용되지 않는 행동은 422입니다. 활동량·분리불안과 생활조건에 따른
기존 주의점을 함께 반환합니다. 애니메이션 파일·실제 2D 게임 화면은 구현하지 않았습니다.

## 검증 및 서버 실행

```sh
venv/bin/python -m unittest discover -s tests -q
venv/bin/python -m scripts.verify_auth_postgres
venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

새 API를 사용하려면 이전 프로세스가 아니라 업데이트된 서버를 실행하세요.
기본 서버 import는 테이블을 자동 생성하지 않습니다. `AUTO_CREATE_SCHEMA=true`는 신규/격리 DB에만 사용하세요.
백업은 gitignored `.demo-backups/`에 파일 권한 600으로 저장합니다.
기존 행 보존 비교는 `scripts.verify_preserved_rows`로 수행했습니다.
