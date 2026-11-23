# #31 회원가입 개인정보 동의 인수인계

- 브랜치 `feature/onboarding-privacy-consent`, 기준 `feature/onboarding-login-id`의 `2666cd8` ([Draft PR #47](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/47)).
- 근거 [Issue #31](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/31), [Notion 회원가입 명세](https://app.notion.com/p/41fb7bbac8c78001808cd1dba181d1db).

## API와 설정

`POST /users/`는 `login_id`, `nickname`, `password`, `privacy`를 받는다. `privacy.document_version`은 서버 버전과 정확히 일치해야 하며, `privacy.age_14_plus_confirmed`는 JSON `true`여야 한다. 회원가입 성공은 HTTP 200 `OK`와 `data`의 `id`, `login_id`, `nickname`, `role`, `shelter_id`를 반환한다. 로그인 토큰은 `POST /auth/login`에서 발급한다.

| 요청 필드 | 저장 유형 | 적용 조건 |
| --- | --- | --- |
| `privacy.collection_use_agreed` | `PRIVACY_COLLECTION_USE` | 활성화된 항목에 `true`를 제출하면 저장. 필수로 설정하면 `true`가 있어야 가입 가능 |
| `privacy.ai_overseas_transfer_agreed` | `AI_OVERSEAS_TRANSFER` | 활성화된 경우에만 FE에서 제출. 저장과 필수 검증은 서버 설정을 따름 |
| `privacy.age_14_plus_confirmed` | `users.age_14_plus_confirmed_at` | 모든 가입에서 `true` 필수. 자기확인 선언의 서버 시각 |

배포 설정에서 `PRIVACY_DOCUMENT_VERSION`(1~32자), `PRIVACY_REQUIRED_CONSENTS`, `PRIVACY_OPTIONAL_CONSENTS`를 관리한다. 목록은 위 저장 유형을 쉼표로 구분한다. 필수 목록은 명시적으로 설정하며, 빈 값은 필수 항목이 없다는 설정이다. 선택 목록 생략 시 선택 항목이 없다. 문서 버전이나 필수 목록이 없거나, 목록에 미지원 항목 또는 필수와 선택에 동시에 지정한 항목이 있으면 500으로 가입을 차단한다. 배포용 버전과 활성 항목의 기본값은 두지 않았다.

| HTTP | 코드 | `data.field` |
| --- | --- | --- |
| 422 | `INVALID_PRIVACY_PAYLOAD` | `privacy` |
| 422 | `PRIVACY_CONFIRMATION_REQUIRED` | `privacy` |
| 422 | `AGE_CONFIRMATION_REQUIRED` | `privacy.age_14_plus_confirmed` |
| 409 | `POLICY_VERSION_MISMATCH` | `privacy.document_version` |
| 500 | `INTERNAL_SERVER_ERROR` | `data`는 `{}` |

`privacy` 객체 누락, 알 수 없는 내부 필드 및 잘못된 타입은 422다. 버전 문자열의 대소문자와 공백을 포함해 현재 설정값과 정확히 비교한다. 기존 계정 입력, 409 중복, 429 제한 응답은 기존 계약을 따른다.

## 트랜잭션과 배포

요청 제한 → 계정 입력과 개인정보 확인 검증 → 필수 동의와 버전 대조 → 아이디 및 닉네임 중복 확인 → 비밀번호 해싱 → 계정 및 자기확인 시각과 동의 저장 → 커밋 순서로 처리한다. `user_consents`는 `users.id`를 참조하고 사용자, 동의 유형, 기록 시각 인덱스를 둔다. 활성 항목의 명시적 동의만 `AGREE`와 문서 버전, 서버 기록 시각으로 저장한다. 계정 INSERT의 중복은 기존 409로 처리한다. 동의 INSERT에 실패하면 500을 반환하고 계정도 롤백한다.

기존 DB에서 [#30 사전 검사와 백업 절차](../30/HANDOFF.md#6-마이그레이션과-배포-순서)를 먼저 따른다. 선행 `016` 또는 승인된 계정 전환과 `017`의 적용 상태를 확인한 뒤 `018_user_consents.sql`을 적용하고 새 API를 기동한다. 로그인도 새 `users` 컬럼을 조회하므로 `018`을 기동 전에 적용해야 한다. `018`은 기존 계정의 자기확인 시각을 `NULL`로 유지한다. 실제 개발 DB 사전 검사, 계정 전환, 마이그레이션, 통합 테스트는 대기 중이다.

## 검증

Windows, Python 3.12, SQLite와 임시 로컬 PostgreSQL 16을 사용했다. `DATABASE_URL=sqlite://`로 고정했고 테스트 전용 DB마다 합성 데이터를 사용했다.

| 실행 | PR #47 기준 | #31 |
| --- | --- | --- |
| `python -m pytest tests -q --tb=short` (`SIGNUP_TEST_DATABASE_URL` 설정) | 440 통과, 2 실패, 82 건너뜀 | 446 통과, 2 실패, 82 건너뜀 |
| 가입, PostgreSQL 가입, 요청 제한, 인증, 보호소 권한 5개 파일 재검증 | 별도 미실행 | 109 통과 |
| 나머지 산책 PostgreSQL 테스트 6개 파일, 각각 빈 DB | 63 통과, 1 실패, 초기화 오류 18 | 63 통과, 1 실패, 초기화 오류 18 |

- 가입 계약, 필수 동의 및 버전 검증, 동의 INSERT 실패 시 전체 롤백과 재시도, 중복 동시 요청, `018` 재실행과 기존 계정 보존을 검증했다.
- 가입 후 로그인과 본인 조회, 로그아웃, 보호소 및 관리자 권한을 회귀 검증했다. `017`과 `018`을 적용한 스키마에서도 기존 역할별 로그인이 통과했다.
- 공통 실패는 `test_walk_settings_api.py::test_invalid_scalar_types_and_unknown_fields_are_422`의 본 테스트와 하위 케이스, `test_walk_recording_postgres.py::test_end_wins_incident_race_and_freezes_all_events`다. 초기화 오류 18건은 `test_gps_motion_postgres.py`, `test_walk_postgres.py`가 `walk_reviews`보다 `walk_review_tags`를 먼저 생성하는 기존 문제다.

## FE 연동과 정책 대기

FE `main` `04e497e`의 개인정보 요약 화면 3개, 상세 화면 2개 체크박스는 기본 선택 상태다. 상세 화면은 목업이고 체크 결과나 버전을 가입 화면에 전달하지 않는다. 산책 루틴의 이용약관과 개인정보 수집 이용 체크는 처음에 해제되어 있고 UI에서 둘 다 필수다. 가입은 목업 닉네임 검사와 로컬 검증을 통과하면 완료 화면으로 이동하며, API 오류를 처리하는 분기가 없다.

- 개인정보 문서를 이메일 수집 없이 확정하고 버전을 올린다. 실제 표시된 버전과 사용자가 직접 변경한 동의값을 메모리에서 가입 화면까지 전달한다. 개인정보 체크박스는 체크 해제 상태로 시작한다.
- 가입 요청에 계정 입력과 `privacy`를 함께 보낸다. 성공 후 `identifier`로 로그인한다. 409 문서 버전 불일치 시 최신 문서를 보여 주고 다시 확인받는다. 가입 실패 시 성공 화면 이동을 차단하고 공통 오류를 표시한다. FE 비밀번호 하한을 15자로 맞춘다.
- 배포 전에 문서 버전, 활성 항목과 필수 여부를 확정한다. 이용약관의 유형과 기록 방식, 산책 중 책임 고지의 동의 여부, 루틴 화면 동의 중복, 가입 종료 계정 포함 이력 보존 기간, 위치정보 동의 범위는 기획 협의가 남아 있다.
