# 인수인계: 내 사용자 정보 조회의 온보딩 진행 상태와 다음 단계 (Issue #45)

- 작성: 2026-11-24 (KST)
- 브랜치: `feature/onboarding-status` (`origin/dev` `2b65434`에서 분기, `main` 모드), PR 대상 `dev`
- 근거: [Issue #45](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/45)(digest `ae751fa7…`), Notion "내 사용자 정보 조회"
- DB 스키마 변경과 마이그레이션은 없다. 개발 DB와 운영 DB에는 접속하지 않았다.

## 1. FE 연동 요약

### `GET /users/me` 응답 `data`

기존 필드 `id`, `login_id`, `nickname`, `role`, `shelter_id`는 그대로다. 아래 9개를 더한다. 이메일, 비밀번호, 토큰, 해시, 전화번호, 생년월일은 반환하지 않는다.

| 필드 | 일반 사용자 | 보호소, 관리자 |
|---|---|---|
| `account_status` | `ACTIVE` 또는 `AGE_REVIEW_REQUIRED` | `ACTIVE` |
| `step1_completed` | boolean (`volunteer_onboarding_basics` 기록) | `null` |
| `step2_completed` | boolean (`volunteer_onboarding_experiences` 기록, 경험 없음 포함) | `null` |
| `step3_completed` | boolean (`volunteer_onboarding_routines` 기록) | `null` |
| `pledge_completed` | 서버 현재 서약 버전의 기록 존재 | 같음 |
| `current_pledge_version` | 서버 현재 서약 버전 | 같음 |
| `pledge_acknowledged_at` | 현재 버전 확인 시각(UTC ISO 8601) 또는 `null` | 같음 |
| `onboarding_completed` | ACTIVE이고 현재 서약과 Step 1~3이 모두 완료면 `true` | `null` |
| `next_onboarding_step` | 첫 미완료 단계 또는 `null` | `null` |

### FE 분기 기준

| 우선순위 | 서버 상태 | `next_onboarding_step` | 이동 |
|---|---|---|---|
| 0 | 유효한 세션 없음 | 401 | 로그인 |
| 1 | `AGE_REVIEW_REQUIRED` | `AGE_REVIEW` | 생년월일 정정 또는 가입 종료 |
| 2 | 현재 버전 서약 없음 | `PLEDGE` | 서약 |
| 3 | Step 1 없음 | `BASIC_INFORMATION` | 기본 정보 |
| 4 | Step 2 없음 | `PET_EXPERIENCE` | 반려견 경험 |
| 5 | Step 3 없음 | `WALKING_ROUTINE` | 산책 루틴 |
| 6 | 모두 완료 | `null`, `onboarding_completed=true` | 홈 |

- 로그인 직후, 앱 재실행, 재로그인 때 FE 로컬 상태가 아니라 이 응답으로 화면을 정한다. 현재 FE(`04e497e`)는 로그인 후 바로 홈으로 가므로 연동 때 수정이 필요하다.
- 보호소와 관리자는 `next_onboarding_step=null`을 홈 진입 신호로 단독 해석하지 않는다. `role`로 화면을 정한다.
- 서약 버전이 바뀌면 이전 기록은 남지만 `pledge_completed=false`, `pledge_acknowledged_at=null`, `next_onboarding_step=PLEDGE`가 된다.
- 연령 확인 필요 계정도 200이다. 다른 단계 기록이 있어도 `AGE_REVIEW`가 먼저다.
- 위치 권한 여부는 완료 판정에 쓰지 않는다.
- 성공과 401 모두 `Cache-Control: no-store`다. 401 코드는 #32와 같다: `AUTHENTICATION_REQUIRED`, `INVALID_ACCESS_TOKEN`, `SESSION_EXPIRED`, `USER_NOT_FOUND`.

### 단계 API 응답의 다음 단계 (함께 바뀐 동작)

Step 1(`next_step`)과 Step 2(`next_onboarding_step`)의 조회, 저장 응답도 이제 같은 공통 함수 값을 돌려준다. 정상 흐름의 값은 그대로다. Step 1 저장 뒤에는 `PET_EXPERIENCE`, Step 2 저장 뒤에는 `WALKING_ROUTINE`이다. 뒷 단계를 이미 마친 계정이 앞 단계를 조회하면 이전에는 고정값을 받았지만, 이제는 실제 다음 단계(모두 완료면 `null`)를 받는다. 필드 이름은 바꾸지 않았다.

## 2. 서버 구현

| 파일 | 내용 |
|---|---|
| `app/onboarding_common.py` | `load_progress()`: 계정 상태, 현재 버전 서약 기록, Step 1~3 기록 존재를 **한 SELECT**로 읽는다. `next_onboarding_step()`: 스냅샷에서 다음 단계를 계산하는 공통 함수. `first_incomplete_step()`과 `/users/me`가 모두 사용한다. `onboarding_status()`: 응답 필드 조립 |
| `app/routers/users.py` | `get_me`에 진행 상태 필드 추가 |
| `app/schemas.py` | `MeData` 응답 모델 |
| `app/basic_info_service.py`, `app/dog_experience_service.py` | 다음 단계 고정값을 공통 함수 값으로 교체 |

- **일관성**: PostgreSQL 기본 격리 수준(READ COMMITTED)은 문장마다 스냅샷이 다르다. 진행 상태를 한 문장으로 읽으므로 상태와 기록이 서로 다른 시점에서 섞이지 않는다. 인증 단계에서 읽은 계정 행보다 나중에 상태가 바뀌면 응답은 진행 상태 문장의 값(더 최신)을 쓴다.
- **쿼리 수**: 요청당 SELECT 3개(세션, 계정, 진행 상태)이며 상태와 관계없이 고정이다. INSERT, UPDATE, DELETE는 없다.
- **`first_incomplete_step()`**: 기존 의미를 유지한다. 계정 상태는 호출자가 확인하거나 잠근 `user` 객체의 값을 쓰고, 단계와 서약 기록은 한 문장으로 읽는다. 단계마다 따로 보내던 최대 4개 쿼리가 1개가 됐다. 보호소와 관리자, 연령 확인 계정은 이전처럼 쿼리 없이 답한다.
- **서약 버전 미설정(D1)**: `PLEDGE_CURRENT_VERSION`이 없으면 `/users/me`는 500 `INTERNAL_SERVER_ERROR`다(#33 서약 API와 같은 fail closed). 로그에는 예외 종류만 남는다. 401은 그보다 먼저 나온다. **#45를 배포하는 환경에는 이 설정이 반드시 있어야 한다.**

## 3. 결정 기록

| # | 결정 |
|---|---|
| D1 | 서약 버전 미설정 시 `/users/me`도 500(fail closed) |
| D2 | Step 1, Step 2 응답의 다음 단계를 공통 함수 값으로 변경. 필드 이름 유지 |
| D3 | Step 1의 `next_step` 이름은 바꾸지 않음(아래 통일안) |
| D4 | 오류 코드는 바꾸지 않음(아래 통일안) |
| D5 | 시각은 기존 관례대로 UTC ISO 8601 |

### 오류 코드와 필드 이름 통일안 (제안, Notion 담당자 확인 필요)

Issue #45의 "오류 코드 정합성" 항목은 미확정으로 남아 있다. 이 PR은 코드를 바꾸지 않았다. 아래는 제안이다.

| 항목 | 현재 | 영향 범위 | 제안 |
|---|---|---|---|
| 역할 403 | `ONBOARDING_NOT_ALLOWED`(Step 1~3), `PLEDGE_NOT_ALLOWED`(서약), `AGE_REVIEW_NOT_ALLOWED`(정정), `ACCOUNT_DELETION_NOT_ALLOWED`(가입 종료) | 해당 API와 FE 오류 처리 | API 목적이 달라 코드를 나누는 현재 방식 유지. FE는 403의 `code`별 문구만 매핑 |
| 연령 확인 계정 403 | `AGE_REVIEW_REQUIRED`(Step 2, 서약, Step 1), `ONBOARDING_NOT_ALLOWED`(Step 3) | Step 3 두 API, 접근 제어 테스트 분류 | `AGE_REVIEW_REQUIRED`로 통일 권장. FE가 정정 화면으로 보낼 근거가 명확해진다. Step 3 Notion 명세 변경이 먼저 필요 |
| 401 코드 집합 | `/users/me` 4종, 대부분 3종, Step 3 명세 2종 | 명세 문서 | 구현은 모두 공통 의존성이라 실제 응답은 같다. 명세를 4종(`USER_NOT_FOUND` 포함)으로 맞추는 것을 권장 |
| 다음 단계 필드 | Step 1 `next_step`, 그 외 `next_onboarding_step` | Step 1 두 API, FE | FE 연동 전이라 바꾸기 쉬운 시점. `next_onboarding_step`으로 통일 권장(명세 변경 필요) |

## 4. 검증

| 범위 | 파일 | 내용 |
|---|---|---|
| 기능 | `tests/test_users_me_onboarding.py` | 단계별 진행(서약, Step 1 SMS 실제 흐름, Step 2, Step 3)과 각 단계 API 응답 일치, #44에서 넘어온 완료 테스트, 서약만 완료, 순서를 어긴 기록, 서약 버전 변경과 기록 보존, 연령 확인 우선과 정정 후 복귀, 보호소와 관리자 null, SELECT 3개와 쓰기 없음, 민감 정보 비노출, 서약 버전 미설정 500, 401 코드와 `no-store`, 재로그인 후 같은 상태 |
| PostgreSQL | `tests/test_signup_postgres.py` | SELECT 3개와 쓰기 없음, 조회 도중 Step 3 저장과 상태 변경이 커밋돼도 한 스냅샷으로 답함, 동시 요청 8건이 같은 상태 |
| 회귀 | `tests/test_auth.py`, `tests/test_pledge.py`, `tests/test_signup.py`, `tests/test_shelter_permissions.py` | 응답 전체 비교와 서약 뒤 상태를 새 계약으로 갱신, 서약 버전 테스트값 설정 |

- Step 2 병렬 저장 테스트 두 개는 장벽을 "스레드당 첫 호출"에만 두도록 바꿨다. 저장 응답이 다음 단계를 계산하려고 같은 함수를 한 번 더 부르기 때문이다(D2). 검증 내용은 같다.
- 실행 결과는 PR 본문에 적는다.

## 5. 범위 밖

- 오류 코드 통일 구현, Step 1 필드 이름 변경
- 상태 값으로 다른 API를 막는 일(각 Issue 담당)
- 회원가입부터 홈까지 여러 API를 잇는 통합 검증(#34)
- FE 수정, Notion 수정, DB 적용, 공용 서버 배포

남은 작업은 [TODO.md](TODO.md)를 본다.
