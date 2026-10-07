# 로그인과 개인 데이터 접근 제한

## 현재 범위

1단계 백엔드 구현: 이메일/비밀번호 회원가입, 로그인, 본인 정보, 로그아웃,
설문·매칭·시뮬레이션의 본인 접근 제한. Firebase 인증은 연결하지 않았습니다.
이 구현과 Firebase를 동시에 사용하지 말고 클라이언트 인증 방향을 하나로 통일하세요.

역할은 `user`, `shelter`, `admin`이며 새 회원은 항상 `user`입니다. 관리 API와 개인 API는
모두 `auth_sessions`의 해시 토큰으로 인증합니다. `app.dependencies`는 같은 인증 함수를
사용하는 역할 검사입니다. `session_tokens` 모델은 기존 스키마 호환용으로만 남겼으며,
그 테이블의 평문 토큰은 새 인증에 사용하지 않습니다. 기존 고정 salt 해시를 임의로 변환하지 않습니다.

기존 계정과 비밀번호, 설문, 매칭 이력을 덮어쓰지 않습니다. 비밀번호가 없거나
과거 평문 형식인 계정은 로그인할 수 없습니다. 기존 계정에 임의 비밀번호를
부여하거나 기존 이메일로 재가입하여 계정을 가져갈 수 없습니다. 이메일 소유권
검증과 비밀번호 재설정은 후속 구현입니다.

## DB 준비

역할 병합 후 기존 DB에는 005를 추가로 적용해야 합니다. 아래 도구는 기본 조회만 하며,
`--apply`에서 백업 확인 후 역할 컬럼을 추가합니다. 기존 역할·비밀번호·세션은 덮어쓰지 않습니다.
이번 충돌 해결에서는 실제 DB에 적용하지 않았습니다.

```sh
venv/bin/python -m scripts.migrate_backend 005
venv/bin/python -m scripts.migrate_backend 005 --apply
```

서버를 중지하고 현재 연결 대상과 구조를 확인한 뒤 `pg_dump` 백업을 검증하세요.
이미 있는 `auth_sessions` 구조가 다르면 아래 SQL을 적용하지 말고 중단하세요.

```sh
psql --dbname '<개발 DB 연결 문자열>' --file migrations/002_auth_sessions.sql
venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

SQL은 세션 테이블·인덱스만 추가합니다. 기존 데이터와 비밀번호는 변경하지 않습니다.
서버 시작 전에 명시적으로 준비하세요. `AUTO_CREATE_SCHEMA=true`는 신규/격리 DB에만
사용하세요. 기본 서버 시작은 DB 구조를 변경하지 않습니다. 개발 DB에는 002 세션
마이그레이션을 백업 후 적용하고 실제 로그인 검증을 완료했습니다.

## Swagger 요청 순서

1. `POST /users/`: 비밀번호 필수(15~128자), 이메일과 닉네임을 등록합니다.

```json
{"email":"my-demo@example.com","nickname":"내 시연 계정","password":"choose-a-new-long-password"}
```

2. `POST /auth/login`: 같은 이메일·비밀번호를 전송합니다.
3. 반환된 `access_token`을 Swagger **Authorize**에 입력합니다(Bearer 접두어 없이 토큰만).
4. `GET /users/me`에서 본인 ID를 얻습니다.
5. `docs/matching-api.md`의 설문 JSON에 본인 ID를 사용해 설문→매칭→이력을 실행합니다.
6. `POST /auth/logout` 이후 같은 토큰의 요청은 401입니다.

토큰은 1시간 유효하며 DB에는 토큰의 SHA-256 해시만 저장합니다. 로그아웃은
현재 토큰만 폐기합니다. 비밀번호는 개별 salt를 가진 scrypt(N=2^17,r=8,p=1)로 저장합니다.
로그인 응답은 캐시하지 않도록 설정합니다.

## 접근 정책

| 경로 | 정책 |
| --- | --- |
| `POST /users/`, `POST /auth/login`, `GET /surveys/options` | 로그인 없이 가능 |
| `GET /users/me` | 로그인 필수 |
| `GET /auth/me` | `/users/me`와 동일; 응답에 본인 role 포함 |
| `POST /dogs/`, `PUT /dogs/{dog_id}/traits` | shelter 또는 admin 역할 |
| `POST /breed-traits/` | admin 역할 |
| `GET /users/{user_id}`, `POST /surveys/`, `GET /surveys/{user_id}` | 본인 ID만 허용 |
| `POST /matching/score`, `GET /matching/history/{user_id}` | 본인 ID만 허용 |
| `POST /matching/{survey_id}` | 본인 설문만 조회·매칭 가능; 타인 설문은 404 |
| `GET /simulation/timeline` | 본인 ID만 허용 |

인증 누락·잘못된/만료된 토큰은 401, 요청 ID가 본인이 아니면 403,
본인에게 보이지 않는 설문은 404, 입력 형식 오류는 422입니다.
기존 경로와 설문/매칭 응답은 유지하되 인증 없이 사용하던 호출은 더 이상 허용하지 않습니다.

자동 시연 스크립트는 본인 ID와 `DEMO_ACCESS_TOKEN` 환경변수를 사용합니다.
기존 비밀번호 없는 seed 계정 대신 위 절차로 새 시연 계정을 만드세요. 토큰을
공유 문서·커밋·CLI 인자·로그에 넣지 마세요.

## 검증과 운영 경계

```sh
venv/bin/python -m unittest discover -s tests -v
```

테스트는 격리된 SQLite를 사용하며 실제 개발 DB를 수정하지 않습니다. 기존
매칭 회귀 테스트는 테스트 전용 인증 의존성을 주입하고, `test_auth.py`는 실제
등록·로그인·토큰·접근 제한 경로를 우회 없이 검증합니다.

로컬 시연용 단계입니다. 외부 공개 전에 HTTPS, 공유 저장소 기반 로그인 속도 제한,
이메일 확인·재설정, 만료 세션 정리, 관리용 데이터 변경 API 권한, 감사 로그가 필요합니다.
현재 역할 제한은 보호소 소속 확인과 별개입니다. 보호소별 소속 관계·타 보호소 수정 차단과
보호소 생성 API의 관리 정책은 산책 기능 개발 시 추가해야 합니다.
CORS는 localhost/127.0.0.1의 3000 포트만 허용합니다. 실제 클라이언트가 결정되면
허용 origin을 명시적으로 조정하고 와일드카드를 사용하지 마세요.

## 다음 단계에서 필요한 선택

프론트엔드 폴더에는 이미지 자산만 있습니다. PDF의 Android 앱과 웹 화면 중
어느 플랫폼으로 만들지 확인한 뒤 설문·매칭 화면을 연결합니다. 그 뒤 성향값의
출처·정보 없음 표시를 포함한 시각화를 추가하고, Gemini 연결 정보 및 개인정보
전송 범위를 확인하여 AI 대화를 구현합니다. AI 응답을 임시 고정 문구로 대체하고
완료했다고 표시하지 않습니다. 마지막으로 기존 timeline API를 사용하는 실제
2D 화면·시간대별 인터랙션을 구현합니다.
