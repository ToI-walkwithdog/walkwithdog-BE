# 인수인계: 온보딩 최초 산책 서약 (Issue #33)

- 작성: 2026-11-23 (KST). 이 문서는 구현 후에 작성했다. 코드와 테스트는 바꾸지 않았다.
- 브랜치: `feature/onboarding-pledge`, 기능 커밋 `681e84f`
- PR: [Draft PR #50](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/50), 작성 시점 대상 브랜치 `feature/onboarding-privacy-consent`(#48, `dev`에 병합됨)
- 근거: [Issue #33](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/33)의 "최종 결정 반영 (2026-11-23)", Notion [최초 산책 서약 확인 및 기록](https://app.notion.com/p/41fb7bbac8c780a29735fef171825764), [온보딩 전체 설계](https://app.notion.com/p/41fb7bbac8c7809b914bcf3e80d9d876)
- 상태: `dev` 미병합, 개발 DB 미적용

## 1. 개요와 범위

가입과 로그인 직후 일반 사용자가 현재 버전의 최초 산책 서약을 확인하면 서버가 이력을 저장한다. 같은 버전으로 다시 요청하면 처음 확인한 시각을 그대로 돌려준다.

확정 순서(2026-11-23 사용자 결정): 회원가입 → 로그인 → 최초 서약 → Step 1 기본 정보 → Step 2 반려견 경험 → Step 3 산책 루틴 → 선택적 위치 권한 안내 → 홈

| 구분 | 내용 |
|---|---|
| 포함 | `PUT /users/me/pledge`, `user_pledge_acknowledgements`, 서버 서약 버전 검증, 멱등 응답, 이전 버전 이력 보존, 동시 요청 처리, 저장 실패 롤백, 현재 버전 기록 조회 함수 |
| 제외 | 별도 서약 조회 API, `GET /users/me`의 서약 상태 필드(#45), 서약 미완료 사용자의 산책 신청 차단, 개인정보 동의(#31) |
| 폐기 | Step 1~3 완료를 서약의 선행 조건으로 보던 BE Notion 기준안. 이 API는 409 `PREVIOUS_STEP_NOT_COMPLETED`를 반환하지 않는다 |

### #33과 #40의 책임 구분

| 항목 | 구현한 곳 |
|---|---|
| 라우트, 버전 검증, 저장, 멱등, `users` 행 `FOR SHARE` 잠금 | #33 (`681e84f`) |
| 계정 상태 확인(403 `AGE_REVIEW_REQUIRED`), 잠긴 행 기준 상태 재확인, `lock_user()`의 `populate_existing()` | #40 (`a15329b`, PR #52) |

#33 커밋만 놓고 보면 계정 상태 검증은 없다. `users.account_status`가 #40에서 생겼기 때문이다. #33 시점에는 해당 테스트를 사유와 함께 skip했고, #40에서 실제 테스트로 바꿨다. Notion의 #33 구현 결과 절은 #33 시점 기준이라 "미구현, #40 연동 대기"로 남아 있다.

## 2. 주요 변경 파일 (#33 커밋)

| 파일 | 내용 |
|---|---|
| `app/pledge_service.py` | 버전 설정 읽기, 요청 버전 검증, 기록 조회, 저장 |
| `app/routers/users.py` | `PUT /users/me/pledge`, `get_pledge_user` |
| `app/schemas.py` | `PledgeRequest`, `PledgeData`, `PledgeResponse` |
| `app/models.py` | `UserPledgeAcknowledgement` |
| `migrations/019_user_pledge_acknowledgements.sql` | 새 테이블 |
| `.env.example` | `PLEDGE_CURRENT_VERSION` 이름(값 없음) |
| `tests/test_pledge.py`, `tests/test_signup_postgres.py` | SQLite와 격리 PostgreSQL 테스트 |

## 3. API 계약

### PUT /users/me/pledge

- 요청: `{"pledge_version": "pledge-v1"}`. 사용자 ID, 확인 시각, 동의 Boolean은 받지 않는다.
- 성공 200(첫 요청과 같은 버전 재요청 모두)

```json
{"code": "OK", "data": {"pledge_completed": true, "pledge_version": "pledge-v1", "acknowledged_at": "2026-11-23T08:30:00+00:00"}, "message": "서약 확인이 완료되었습니다."}
```

- `acknowledged_at`은 서버가 저장한 최초 확인 시각(UTC)이다.

| 순서 | 검사 | 실패 응답 |
|---|---|---|
| 1 | Bearer 인증(`get_contract_user`) | 401 `AUTHENTICATION_REQUIRED`, `INVALID_ACCESS_TOKEN`, `SESSION_EXPIRED`, `USER_NOT_FOUND` |
| 2 | `role=user` | 403 `PLEDGE_NOT_ALLOWED` "서약할 수 없는 계정입니다." (보호소, 관리자) |
| 3 | 계정 상태 `ACTIVE` (#40에서 추가) | 403 `AGE_REVIEW_REQUIRED` "연령 확인 정보를 먼저 수정해 주세요." |
| 4 | 요청 버전 형식 | 422 `PLEDGE_VERSION_REQUIRED`(누락, `null`, 공백, 본문 없음), `INVALID_PLEDGE_VERSION`(문자열 아님, 앞뒤 공백 제거 후 32자 초과). `data.field=pledge_version` |
| 5 | 서버 현재 버전과 정확히 일치 | 409 `PLEDGE_VERSION_MISMATCH` "서약 내용을 다시 확인해 주세요." |
| 6 | 저장 | 500 `INTERNAL_SERVER_ERROR` (설정 누락, DB 오류) |

- 인증, 역할, 상태 오류는 본문 검증보다 먼저 반환한다.
- `USER_NOT_FOUND`는 Notion 서약 오류 표에 없지만 공통 계약 인증 의존성이 반환한다.

### 서버 서약 버전

- `PLEDGE_CURRENT_VERSION`(1~32자)을 요청마다 읽는다. 관리 위치(설정 또는 DB)가 Issue에서 미확정이라 #31 문서 버전과 같은 설정 방식을 썼다.
- 설정이 없거나 32자를 넘으면 버전 비교 전에 500으로 실패하고 아무것도 저장하지 않는다(fail closed). 기본값은 없다.
- 버전이 바뀌면 이전 버전 기록은 그대로 두고 새 버전 확인을 요구한다. 현재 버전 기록만 완료로 본다.

## 4. DB와 마이그레이션

`migrations/019_user_pledge_acknowledgements.sql`

| 컬럼 | 타입 | 제약 |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `user_id` | INTEGER | NOT NULL, FK `users(id)` (`fk_user_pledge_ack_user_id`, NO ACTION) |
| `pledge_version` | VARCHAR(32) | NOT NULL |
| `acknowledged_at` | TIMESTAMPTZ | NOT NULL |

- `uq_user_pledge_ack_user_version UNIQUE (user_id, pledge_version)`: 사용자와 버전별 1행, 최초 확인 시각 유지. 이 인덱스가 `user_id` 조회도 맡는다.
- 새 테이블 하나만 추가한다. 기존 테이블과 행은 바꾸지 않는다. `BEGIN`/`COMMIT` 한 트랜잭션이고 재실행하면 변경이 없다.
- `users`에 컬럼을 추가하지 않으므로 019 적용 전에도 가입과 로그인은 동작하고 서약 API만 실패한다.
- SQLAlchemy 모델의 `id`는 SQLite에서만 INTEGER로 바뀐다(자동 증가 호환).

## 5. 처리 흐름, 트랜잭션, 잠금

`acknowledge_pledge()`의 순서

1. `current_pledge_version()`: 설정이 없으면 쓰기 전에 실패
2. 요청 버전과 서버 버전 비교, 다르면 409(기록 없음)
3. 트랜잭션: `users` 행을 `FOR SHARE`로 잠근다. #40 이후에는 `lock_user()`가 `populate_existing()`으로 잠금 시점 값을 다시 읽고 `ensure_active()`로 상태를 재확인한다.
4. `(user_id, pledge_version)` 기록이 없으면 서버 시각으로 `INSERT ... ON CONFLICT (user_id, pledge_version) DO NOTHING`
5. 커밋. 예외가 나면 롤백 후 다시 던진다(500).
6. 커밋 후 기록을 다시 읽어 응답한다. 이 요청이 넣은 행이든 동시 요청이 먼저 넣은 행이든 최초 시각을 돌려준다.

- 잠금 순서는 #40 이후 모든 온보딩 쓰기에서 같다: `users` 행 먼저, 사용자 소유 행 다음.
- `FOR SHARE`는 #40의 상태 전환(`FOR UPDATE`)과 충돌한다. 서약이 먼저 잠그면 서약 기록이 남고 상태 전환은 기다린다. 상태 전환이 먼저 커밋되면 서약은 잠긴 행에서 403을 받고 기록이 생기지 않는다.
- 상태 전환은 기존 서약 기록을 지우지 않는다. 서약 기록은 #41 가입 종료에서만 지울 수 있고, 그 삭제는 보존 정책 결정 전까지 운영에서 실행되지 않는다(#41 문서 참고).

## 6. 멱등성, 동시성, 롤백

- 같은 버전 재요청: 기존 행을 그대로 반환한다. 시계를 뒤로 옮겨도 최초 시각이 유지된다.
- 동시 요청: UNIQUE 제약에서 먼저 저장된 행만 남는다. 늦은 요청은 아무것도 넣지 않고 같은 행을 재조회해 같은 시각으로 200을 받는다.
- 응답 유실과 중복 클릭: 같은 요청을 다시 보내면 된다.
- 저장 실패: 롤백 후 500, 기록 없음. 재시도하면 성공한다.

## 7. 보안

- 요청에서 사용자 ID를 받지 않는다. 토큰의 세션 사용자만 서약한다.
- 서약 버전 외에 개인정보를 받거나 로그에 남기지 않는다.

## 8. 테스트 결과 (PR #50, #52 본문 기록 인용. 이번 문서 작업에서 다시 실행하지 않음)

실행 환경: Windows 11, Python 3.12 `.venv`, SQLite, 격리 PostgreSQL 16.16 테스트 DB(`SIGNUP_TEST_DATABASE_URL`)

| 시점 | 실행 | 결과 |
|---|---|---|
| #33 (`681e84f`) | `tests/` 전체 | 463 passed, 2 failed, 83 skipped |
| #33 | 선행 #48 기준 | 446 passed, 2 failed, 82 skipped |
| #33 | `tests/test_pledge.py` | 13 passed, 1 skipped(#40 대기) |
| #33 | `tests/test_signup_postgres.py` 서약 관련 | 4 passed, 서약 테스트 3개 2회 반복 통과 |
| #40 (`a15329b`) | `tests/test_pledge.py` | 14 passed (skip 해제) |
| #40 | 격리 PostgreSQL 서약 경합 | 서약이 공유 잠금 중일 때 상태 전환 대기, 상태 전환 후 서약 403과 기록 없음 |

- 실패 2건은 선행 브랜치에서도 실패하는 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`다.
- 건너뛴 82건은 각자 빈 DB가 필요한 산책 PostgreSQL 테스트로 실행하지 않았다.
- 검증 항목: 최초 서약 200과 1건, 가입 후 로그인 직후 Step 1~3 없이 성공, 같은 버전 재요청 시각 유지, 이전 버전 보존, 422와 409에서 기록 없음, 401과 보호소, 관리자 403, 서버 버전 미설정과 33자 설정 500, SQLite 커밋 실패 롤백, PostgreSQL 삽입 후 실패 롤백, PostgreSQL 동시 6건 모두 200과 같은 시각과 1건, UNIQUE 제약, 016~018 이후 019 적용과 재실행, `create_all()` 없이 동작
- 미실행: 개발 DB 검증, FE 실연동

## 9. FE 연동

- `POST /users/` 성공 후 `POST /auth/login`으로 토큰을 받고 서약 화면에서 `PUT /users/me/pledge`를 호출한다. 자동 로그인 API나 가입 응답 토큰은 없다. 가입 후 로그인이 실패하면 가입을 다시 부르지 않고 로그인 화면으로 보낸다.
- 화면에 표시한 서약 문서의 버전을 `pledge_version`으로 그대로 보낸다. 409면 최신 서약을 다시 보여 주고 다시 확인받는다.
- 403 `PLEDGE_NOT_ALLOWED`는 보호소, 관리자다. 403 `AGE_REVIEW_REQUIRED`는 연령 확인 화면(#41, FE 미구현)으로 보낸다.
- 서약 완료 여부 복원은 #45의 `GET /users/me` 확장 후 가능하다.

## 10. Notion 설계와 실제 구현의 차이

| 항목 | Notion | 구현 | 근거 |
|---|---|---|---|
| 선행 조건 | 서약 명세 일부와 기존 BE 기준안에 Step 1~3 완료 후 서약 | Step 1~3 확인 없음, 409 `PREVIOUS_STEP_NOT_COMPLETED` 미반환 | 2026-11-23 사용자 결정(FE 순서) |
| 서약 버전 관리 | "서버에서 관리", 예시 `pledge-v1` | `PLEDGE_CURRENT_VERSION` 설정 | 관리 위치 미확정 |
| 401 코드 | 3종 | 공통 의존성의 `USER_NOT_FOUND` 포함 4종 | 공통 인증 계약 |
| `GET /users/me` 서약 상태 | 포함 | 미구현 | #45 범위 |

Notion 온보딩 문서에서 확정 순서와 맞지 않는 문구(위치 권한 안내 단계 누락 등)는 Notion의 #33 구현 결과 절에 표시만 했고 수정하지 않았다.

## 11. 미구현과 미확정

- [ ] 서약 버전 값과 관리 위치(설정 또는 DB)
- [ ] 서약 미완료 사용자의 산책 신청 차단 여부(산책 신청 화면 설계 때 결정)
- [ ] 역할 차단 코드 `PLEDGE_NOT_ALLOWED`와 다른 온보딩 API의 `ONBOARDING_NOT_ALLOWED` 통일 여부(#45 오류 코드 정합성)
- [ ] `GET /users/me`의 서약 상태와 `onboarding_completed`(#45)
- [ ] 가입 종료 계정의 서약 기록 법정 보존 여부와 기간(#41 대기 사항)
- [ ] FE 서약 화면이 표시한 버전을 그대로 보내는지 연동 확인

## 12. 개발 DB와 배포 대기

- 019는 016, 017(`017_remove_user_email.sql`), 018(`018_user_consents.sql`) 다음에 적용한다. 실제 개발 DB 적용 여부는 확인하지 못했다(접속 정보 없음). 적용은 승인 후 진행한다.
- 019 자체는 기존 행을 바꾸지 않는다. 단, #40 이후 코드는 `users.account_status`가 필요하므로 이 스택을 배포할 때는 020까지 함께 적용해야 로그인이 동작한다(#40 문서 참고).
- 배포 설정: `PLEDGE_CURRENT_VERSION`을 FE가 표시하는 서약 버전과 같게 설정한다.
- 마이그레이션 번호: `origin/dev`에 `017_f10_behavior_marker.sql`, `018_f11_walk_overtime.sql`이 들어와 017, 018 번호가 각각 두 파일이다. 019 자체는 `dev`와 겹치지 않지만, 온보딩 파일 전체의 번호 정리가 병합 전 과제로 남아 있다. 이 문서 작성 시점에 파일 이름과 SQL은 바꾸지 않았다.

## 13. 후속 Issue와 의존 관계

- 선행: #30, #32(가입, 로그인, 계약 401), #31(개인정보 동의)
- 후속: #40(Step 1 선행 조건으로 `current_pledge_acknowledgement()` 사용, 계정 상태 연결), #41(정정 후 다음 단계 계산, 가입 종료 시 서약 기록 처리), #42(Step 2 선행 조건), #45(`GET /users/me` 서약 상태)

## 14. 관련 링크

- Issue #33, PR #50, 후속 PR #52(#40), #53(#41), #57(#42)
- Notion: [최초 산책 서약 확인 및 기록](https://app.notion.com/p/41fb7bbac8c780a29735fef171825764), [내 사용자 정보 조회](https://app.notion.com/p/41fb7bbac8c7806f8f31e76e267314e2), [온보딩 전체 설계](https://app.notion.com/p/41fb7bbac8c7809b914bcf3e80d9d876)의 "#33 최초 산책 서약 구현 결과" 절
