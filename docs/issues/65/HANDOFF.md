# 인수인계: F-14 이후 PostgreSQL 가입, 로그인 마이그레이션 테스트 회귀 수정 (Issue #65)

- 작성: 2026-11-24 (KST)
- 브랜치: `fix/f14-signup-migration-regression` (`origin/dev` `4e1ea8f`에서 분기), PR 대상 `dev`
- 근거: [Issue #65](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/65), 원인 PR #60(F-14, [#59](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/59))
- 범위: 테스트와 문서만 바꿨다. 기능 코드, SQL 원본, manifest는 바꾸지 않았다. 개발 DB에는 접속하지 않았다.

## 1. 실제 오류

격리 PostgreSQL 16.16(`toi_walk_test_` 테스트 DB, 테스트마다 `su_<uuid>` 스키마)에서 `dev` `4e1ea8f`를 그대로 실행해 재현했다. API 계층은 예외 종류만 기록하므로, 저장소 밖 실행 스크립트에서 SQLAlchemy `handle_error` 이벤트로 원문을 받았다. 오류 원문에 개인정보나 비밀 값은 없었다(파라미터는 키 이름만 기록).

| 테스트 | 실패 단계 | 예외, SQLSTATE | 실패한 쿼리 |
|---|---|---|---|
| `test_migration_016_after_f08_f09_keeps_their_schema_and_signup_works_without_create_all` | 016 검증 뒤 017, 018 적용, 최신 API의 가입 | `psycopg2.errors.UndefinedColumn`, `42703` | `INSERT INTO users (login_id, nickname, password, role, shelter_id, phone, age_14_plus_confirmed_at, first_walk_guide_completed_at) ...` |
| `test_migration_017_drops_only_email_and_every_role_logs_in_by_id` (하위 `user`, `shelter`, `admin`) | 017 검증 뒤 018 적용, 최신 API의 로그인 | 같음 | `SELECT users.id, ..., users.age_14_plus_confirmed_at, users.first_walk_guide_completed_at, users.created_at FROM users WHERE lower(users.login_id) = ...` |
| `test_migration_018_preserves_existing_accounts_and_adds_consent_history` | 018 검증 뒤 최신 API의 가입 | 같음 | 위 016과 같은 `INSERT` |

- 오류 원문: `"first_walk_guide_completed_at" 칼럼은 "users" 릴레이션(relation)에 없음`, `users.first_walk_guide_completed_at 칼럼 없음`
- 각 테스트의 단계별 DDL과 데이터 검증은 실패 전에 모두 통과했다. 실패는 최신 API를 띄운 뒤에만 났다.
- 최초 발생: `dev` 병합 커밋 `b19fd84`(PR #60). 직전 `60fd162`(PR #58)에서는 통과한다. PR #60 구현 커밋 `6018a52`이 `app/models.py`의 `User`에 `first_walk_guide_completed_at`을 추가했다.
- 컬럼을 만드는 파일: `migrations/019_first_walk_guide.sql`(`ALTER TABLE users ADD COLUMN IF NOT EXISTS first_walk_guide_completed_at TIMESTAMPTZ NULL`, `psql-single`, `requires` 없음)

**원인**: 기능 코드와 SQL의 결함이 아니다. 세 테스트는 016~018만 적용한 스키마에서 최신 앱을 띄우는데, 최신 `User` 모델은 019가 만드는 컬럼까지 조회하고 저장한다. 테스트가 최신 모델에 필요한 스키마를 준비하지 않은 것이 원인이다.

## 2. 수정 내용 (`tests/test_signup_postgres.py`)

| 항목 | 내용 |
|---|---|
| `GUIDE_MIGRATION`, `USER_COLUMNS_AFTER_018` | 018 이후 파일이 추가하고 최신 `User`가 매핑하는 컬럼과 그 파일(`first_walk_guide_completed_at` → `019_first_walk_guide.sql`) |
| `migrate_users_for_current_models()` | 위 파일을 적용하는 헬퍼. 단계 검증이 끝난 뒤, 최신 API를 띄우기 직전에만 호출한다 |
| 016 단계 테스트 | 016 검증은 그대로 두고, 017, 018 다음에 019를 적용한 뒤 가입. 마지막의 016 재적용 거부 검사는 019까지 적용한 스냅샷과 비교 |
| 017 단계 테스트 | 017 검증(이메일 컬럼, 인덱스, 제약만 제거, 행 보존, 재실행 무변경)은 그대로 두고, 018 다음에 019를 적용한 뒤 역할별 로그인 |
| 018 단계 테스트 | 018 검증은 그대로 둔다. 019를 적용하면서 019가 `users` 컬럼 하나만 추가하고 다른 테이블, 인덱스, 제약은 바꾸지 않는지 확인. 018 재실행 무변경 검사는 019 적용 후 스냅샷과 비교 |
| 새 019 단계 테스트 `test_migration_019_adds_only_the_guide_column_and_matches_the_current_user_model` | 아래 3절 |

- 금지 사항 준수: 테스트 삭제, `skip`, `xfail`, 예외 무시, 검증을 약하게 만드는 Mock, 제약 제거 없음

## 3. 단계별 검증 의도를 유지한 방법

- 016, 017, 018의 DDL과 데이터 검증은 해당 단계까지의 파일만 적용한 상태에서 실행한다. 019는 그 검증이 모두 끝난 뒤에만 적용한다. 과거 단계 검증 앞에 019를 먼저 적용하지 않는다.
- 019 단계 테스트(신규)
  - 018 단계에서 최신 모델이 매핑하지만 DB에 없는 `users` 컬럼이 `USER_COLUMNS_AFTER_018`과 정확히 같은지 확인(019 전 스키마)
  - 019 적용 후 `users`에 `first_walk_guide_completed_at TIMESTAMPTZ NULL` 하나만 추가, 다른 테이블, 인덱스, 제약 동일, 기존 계정 3개 행 동일, 새 컬럼은 모두 NULL
  - 019 적용 후 `users` 컬럼 집합이 최신 `User` 모델과 정확히 같음
  - 재실행 무변경
  - `create_all()` 없이 기존 계정 로그인, `/users/me`, 새 가입 성공, 새 계정의 완료 시각 NULL
- 앞으로 `users`에 컬럼을 추가하는 마이그레이션이 생기면 `USER_COLUMNS_AFTER_018`에 넣어야 019 단계 테스트가 통과한다. 모델과 마이그레이션의 불일치를 500이 아니라 명확한 실패로 알려 준다.

## 4. 검증 결과

실행 환경: Windows 11, Python 3.12 `.venv`, `DATABASE_URL=sqlite://`, `SIGNUP_TEST_DATABASE_URL`, `REGION_TEST_DATABASE_URL`을 격리 테스트 DB로 지정

| 실행 | 수정 전 (`dev` `4e1ea8f`) | 수정 후 |
|---|---|---|
| 대상 테스트 3개 | 3개 실패(하위 포함 6건) | 통과 |
| 오류 기록 스크립트 | DB 오류 1, 4, 1건 | 0, 0, 0건 |
| `python -m pytest -q tests/test_signup_postgres.py` | 3 failed(하위 포함 6건) | 15 passed, 19 subtests |
| 가입, 인증, 요청 제한, 보호소 권한, 마이그레이션 검사 테스트 | | 114 passed |
| `python scripts/check_migrations.py` | 종료 코드 0 | 종료 코드 0 |
| `python -m pytest -q tests/` | 528 passed, 11 failed, 82 skipped | 532 passed, 5 failed, 82 skipped |

- 수정 후 남은 실패(모두 `dev`에서도 실패)
  - `tests/test_walk_domain.py` 3건: F-15 회귀, [#66](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/66)에서 수정
  - `tests/test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`(본 테스트와 하위 1건): 이전부터 실패
- 통과 증가 4건: 고친 3건과 새 019 테스트 1건
- 반대 검사: 저장소 밖 pytest 플러그인으로 `migrate_users_for_current_models()`를 빈 함수로 바꾸면 원래 실패 6건이 그대로 다시 난다.
- `check_migrations.py` 출력의 019, 020 `BLOCKED FUTURE MIGRATION`은 온보딩 Draft PR(#50, #52)과의 기존 번호 충돌이며 이 작업과 무관하다.
- 테스트 DB는 실행 뒤 비어 있음을 확인했다(사용자 스키마 0, `public` 테이블 0).

## 5. 통합 마이그레이션과 앱 기동 순서

- 현재 `User` 모델이 매핑하는 `users` 컬럼은 016(`login_id`), 017(`email` 제거), 018(`age_14_plus_confirmed_at`), 019(`first_walk_guide_completed_at`)까지 적용해야 모두 존재한다.
- 따라서 통합 마이그레이션에서는 016~019를 적용한 뒤 새 코드를 기동한다. 019가 없으면 사용자 조회가 있는 가입과 로그인이 `UndefinedColumn`(42703)으로 500이 된다. `docs/migrations/README.md`의 "파일별 적용과 실패 복구 기준" 1번에 이 내용을 추가했다.
- manifest의 `requires`는 SQL 객체 의존성만 다룬다. 019는 SQL 의존성이 없지만, 앱 기동 조건으로는 필요하다.
- 실제 개발 DB 적용은 프로젝트 정책대로 모든 기능 개발과 PR 병합 뒤 통합 마이그레이션에서 진행한다. 그 전에는 새 스키마를 요구하는 코드를 공용 서버에 배포하지 않는다.

## 6. 관련 Issue에 미치는 영향

- #59: 회귀 테스트가 다시 통과한다. #59의 종료는 이 PR 병합 뒤 검토한다.
- #30, #31, #32: 각 Issue 현황 절에 "F-14 회귀로 실패"로 적힌 격리 DB 검증(016 이후 가입, 017 이후 역할별 로그인, 018 이후 계정 보존과 동의)이 이 브랜치에서 다시 통과한다. 다른 미완료 조건(개발 DB 적용, 정책 결정, FE 확인)은 바뀌지 않는다.
- #66: 별개의 독립 작업이다. 이 PR은 `test_walk_domain`을 바꾸지 않았다.
- 온보딩 Draft PR(#50, #52, #53, #57)의 `tests/test_signup_postgres.py`도 이 파일을 크게 바꿨다. 그 PR들이 `dev`를 반영할 때 이 변경과 합쳐야 하며, 그 브랜치의 `users` 컬럼(`account_status` 등)도 `USER_COLUMNS_AFTER_018`에 추가해야 한다.

## 7. 관련 링크

- Issue #65, #59, #30, #31, #32, #66, PR #60
- `tests/test_signup_postgres.py`, `migrations/016_onboarding_signup.sql`, `migrations/017_remove_user_email.sql`, `migrations/018_user_consents.sql`, `migrations/019_first_walk_guide.sql`, `migrations/manifest.json`, `docs/migrations/README.md`
