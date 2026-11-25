# #110 인수인계와 16~20단계 문서 안내

이 문서는 #110(인증번호 발송 API의 전화번호 등록 여부 노출 방지)의 결과와, 16~20단계 역할 분담을 위한 인수인계 문서 묶음의 시작점이다.

- 조사 기준: BE `dev` `f5b230e696f3b7461eda5cf36f5266990e5f65df`, FE `main` `04e497e056eb078ad584708226202f5bfbd2bb6f`
- 작성일: 2026-11-25
- 표기: **확정**은 사용자가 승인한 정책, **권장**은 제안, **미확정**은 결정 전, **미확인**은 이번 조사에서 확인하지 못한 내용이다.

## 목차

1. [문서 묶음](#1-문서-묶음)
2. [#110 최종 구현과 API 계약](#2-110-최종-구현과-api-계약)
3. [SUPPRESSED와 비동기 발송 정책](#3-suppressed와-비동기-발송-정책)
4. [마이그레이션 037과 배포 선행 조건](#4-마이그레이션-037과-배포-선행-조건)
5. [커밋, PR, 리뷰, 테스트](#5-커밋-pr-리뷰-테스트)
6. [실제 DB 미적용과 남은 검증](#6-실제-db-미적용과-남은-검증)
7. [보안 위험과 후속 작업](#7-보안-위험과-후속-작업)
8. [SMS 정책 변경(Mock SMS)](#8-sms-정책-변경mock-sms)

## 1. 문서 묶음

| 문서 | 대상 | 내용 |
| --- | --- | --- |
| [HANDOFF-ROADMAP.md](HANDOFF-ROADMAP.md) | 전체 팀 | 20단계 로드맵, 수정된 16단계, 담당자와 병렬 순서, 브랜치와 충돌 방지, 외부 공개 전 필수 검증 |
| [HANDOFF-BE.md](HANDOFF-BE.md) | 다른 BE 팀원 | 16-4~16-9와 18~20단계 담당 범위의 상세 개발 가이드 |
| [HANDOFF-FE.md](HANDOFF-FE.md) | FE 담당자 | 실제 FE와 BE 코드를 비교한 화면별 연동 가이드 |

관련 Issue
- BE 팀원 작업: 16-4 [#113](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/113), 16-5 [#114](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/114), 16-6 [#115](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/115), 16-7 [#116](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/116), 16-8 [#117](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/117), 16-9 [#118](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/118)
- 문서 반영: [#119](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/119)

기존 기능별 인수인계는 그대로 유지한다.
- [docs/handoff/LATEST.md](../../handoff/LATEST.md): #29, #84~#110의 API 계약
- [docs/issues/](../): #30~#78 온보딩과 지역 관련 Issue별 HANDOFF, TODO
- [docs/migrations/README.md](../../migrations/README.md): 마이그레이션 관리 기준
- [docs/deployment/](../../deployment/): Docker, EC2 배포와 롤백(#101, #109)
- [docs/TODO.md](../../TODO.md): 단계별 남은 작업

## 2. #110 최종 구현과 API 계약

대상은 두 발송 API와 두 확인 API다.

| API | 목적 | 응답 |
| --- | --- | --- |
| `POST /users/me/phone-verifications` | 온보딩 Step 1 | 200 접수. 등록 여부와 무관하게 같은 응답 |
| `POST /users/me/phone-verifications/confirm` | 온보딩 확인 | 200 인증, 422 불일치, 410 만료, 429 시도 초과, 409 이미 사용(`VERIFICATION_ALREADY_CONSUMED`), 404 없음 |
| `POST /users/me/profile/phone-verifications` | 프로필 번호 변경 | 위와 같음(Step 1 기록 필요) |
| `POST /users/me/profile/phone-verifications/confirm` | 프로필 확인 | 위와 같음 |

- **발송 200**: `{verification_id, expires_in_seconds: 300, resend_after_seconds: 60}`. 서버의 발송 요청 **접수** 성공이며 문자 수신을 뜻하지 않는다(확정).
- **503 `SMS_DELIVERY_UNAVAILABLE`**: 공급자 미설정이나 발송 대기열 포화일 때만 반환한다. 아무것도 기록하거나 차감하지 않는다.
- **제거된 응답**: 발송 409 `PHONE_ALREADY_REGISTERED`, 확인 409 `VERIFICATION_NOT_SENT`
- **유지된 정책**: 인증번호 6자리, 유효 300초, 재발송 60초, 계정과 번호별 하루 5회, 요청당 실패 5회, 저장 가능 15분, 최신 요청만 유효, 온보딩과 프로필 목적 분리
- **저장 API는 별도**: 확인만으로 번호가 저장되지 않는다. 온보딩은 `PUT /users/me/onboarding/basic-info`, 프로필은 `PATCH /users/me/profile`(`expected_profile_version` 필수)을 호출해야 한다.
- **소유 증명 후 409 유지**: 저장 시 다른 계정에 등록된 번호면 409 `PHONE_ALREADY_REGISTERED`(인증번호로 소유를 증명한 사람만 도달)
- 세부 계약: [docs/handoff/LATEST.md의 #110 절](../../handoff/LATEST.md)

## 3. SUPPRESSED와 비동기 발송 정책

- **처리 순서**(모든 요청 동일)
  1. 발송 슬롯 확보(worker 2, 대기 20)
  2. 계정 상태와 서약(또는 Step 1) 확인
  3. 일일 카운터 차감
  4. 재발송 간격 확인
  5. 등록 여부 확인
- **SUPPRESSED**: 다른 계정에 등록된 번호의 요청은 `SUPPRESSED`로 저장한다. 인증번호는 HMAC만 남기고 버리며, 공급자를 호출하지 않는다. 어떤 인증번호로도 인증되지 않고 저장에도 쓸 수 없다.
- **비동기 발송**: `app/phone_verification_service.py`의 `SmsDispatcher`(프로세스 내부 Thread Pool)와 `deliver_verification()`
  - 독립 세션을 쓰고, 공급자 호출 중 트랜잭션을 유지하지 않는다.
  - 발송 직전(T2) `users` FOR SHARE, 요청 FOR UPDATE 순으로 잠그고 다시 확인한다.
  - 결과(T3)는 `SENDING`인 행만 바꾼다. 그사이 인증된 행은 항상 `SENT`이다.
  - 자동 재발송은 없고, 중복 dispatch는 한 번만 발송한다. 슬롯 반납은 멱등이다.
- **확인**: DB만 쓴다. 틀린 인증번호는 상태와 무관하게 422, 맞는 인증번호는 `SENDING`, `SENT`, `UNKNOWN`에서만 인증된다.
- **전제**: API worker 1개(배포 compose 기준). 정상 종료는 대기 작업을 실행하고 끝나며, 강제 종료 시 `PENDING`, `SENDING`이 남는다. 사용자는 60초 뒤 재요청한다.

## 4. 마이그레이션 037과 배포 선행 조건

`migrations/037_phone_verification_suppressed.sql`은 `ck_phone_verifications_send_status`에 `SUPPRESSED`를 추가한다(선행 024, 기존 행 유지, 재실행 가능, SHA-256은 `migrations/manifest.json` 참조).

확정 조건(16-6 담당자가 반드시 지킨다):
1. **037을 먼저 적용한 뒤** 이 스키마를 요구하는 코드(#110 이후 이미지)를 배포한다. 037 없이 배포하면 등록된 번호의 요청만 CHECK 위반 500이 되어, 그 자체가 등록 여부 노출이다.
2. DB 호환성 기록(`schema-record.json`)과 이미지별 승인의 `migrations`에 `037_phone_verification_suppressed.sql`과 그 SHA-256을 포함한다.
3. 카탈로그 지문(`fingerprint`)은 037 적용 **이후**의 카탈로그로 생성한다.
4. 신규 DB를 모델로 초기화한 경우에도 037과 동등한 CHECK가 있는지 검증하고, 실제 SQL 적용 이력과 초기화 증빙을 구분해 기록한다.
5. 기존 배포 스크립트(`scripts/deploy.sh`, `scripts/deployment_common.sh`의 `schema_evidence()`)는 기록 형식, DB 식별값, 배포 시점 카탈로그 지문과 기록의 일치, 승인 파일과 기록의 일치를 검사한다. 하지만 `migrations` 배열의 내용(037 포함 여부)이나 그 스키마가 이미지가 요구하는 스키마인지는 검사하지 않으므로 선행 적용 순서를 **직접 강제하지 않는다**. 순서는 기록 발급 절차와 검토로 지킨다.
6. 이전 이미지로 롤백할 때 DB 마이그레이션을 무조건 되돌리지 않는다. 이전 코드에서도 `SUPPRESSED` 행은 인증되지 않는다. 스키마 자체를 되돌릴 때만 `SUPPRESSED` 행을 먼저 삭제해야 이전 CHECK를 다시 걸 수 있다.

## 5. 커밋, PR, 리뷰, 테스트

- Issue [#110](https://github.com/ToI-walkwithdog/walkwithdog-BE/issues/110), PR [#112](https://github.com/ToI-walkwithdog/walkwithdog-BE/pull/112), merge `f5b230e`(부모 `f805490`, `7afc3d6`)
- 커밋
  - `388f07c`: 기능
  - `e8a9bc4`: 리뷰 차단 사항 수정(프로필 증빙 미사용 검사 복원, 프로필 저장 재검증 테스트, 멱등 슬롯)
  - `7afc3d6`: dev(PR #111) 병합
- 독립 리뷰 3회: 1차 차단 2건 수정, 2차와 3차(dev 병합 후) 차단 없음
- 테스트
  - 전체 1019 passed, 0 failed, 88 skipped(CI 전용 PG 그룹). CI `Dev quality gate` 통과
  - 신규 26건: `tests/test_phone_verification_privacy.py` 20건, `tests/test_phone_verification_privacy_postgres.py` 6건
  - 기존 8건은 테스트 ID를 유지하고 기대값만 바꿨다.
- 변이 검증 6건: confirm 상태 차단, Step 1과 프로필 상태 재검증, 프로필 미사용 검사, T3의 인증 후 `SENT`, 카운터 후 재발송 순서

## 6. 실제 DB 미적용과 남은 검증

- 035, 036, 037을 포함해 024 이후 마이그레이션의 개발 DB 적용 여부는 **미확인**이다. 적용은 16-5, 16-6에서 별도 승인 후 진행한다.
- **#110 추가 검증(완료)**: 서로 다른 계정이 같은 번호로 요청해 **일일 한도 초과**로 거절될 때의 롤백을 PostgreSQL에서 검증했다(`tests/test_phone_verification_privacy_postgres.py`).
  - `test_the_number_daily_limit_refuses_another_account_after_the_resend_wait_and_rolls_back`: 재발송 간격을 지난 다른 계정의 요청이 번호 일일 한도로 거절되는 경우다.
  - `test_an_exhausted_number_refuses_concurrent_accounts_without_charging_them`: 한도가 소진된 번호로 두 계정이 동시에 요청하는 경우다.
  - 두 테스트 모두 미등록 번호와 등록 번호를 확인한다. 고정 시각 기준 429 `PHONE_VERIFICATION_RATE_LIMITED`, 정확한 `retry_after_seconds`와 `Retry-After`를 확인하고, 거절된 요청이 올린 계정과 번호 카운터가 롤백되어 기존 값(계정 2 또는 1, 번호 5)이 그대로 유지되고 요청 행과 문자가 남지 않음을 확인한다.
  - 변이 검증 2건(일일 한도 거절 제거, 거절 전 카운터 커밋)에서 두 테스트가 실패하는 것을 확인했다.
  - 기능 코드는 바꾸지 않았다. 카운터 차감과 재발송 검사의 순서는 기존 동시 요청 테스트가 계속 검증한다.
- 실제 SMS 발송은 하지 않는다(8절).

## 7. 보안 위험과 후속 작업

외부 공개 전(16-10 이전) 필수 검증 항목이다. 상세 계획은 [HANDOFF-ROADMAP.md의 외부 공개 전 필수 검증](HANDOFF-ROADMAP.md#7-외부-공개-전-필수-검증)에 있다.

- **슬롯 반납 시점 차이**: `SUPPRESSED`는 즉시, 실제 발송은 공급자 호출 뒤 반납한다. 서버 전체를 포화시키며 503 시점을 관찰하는 간접 노출 가능성이 있다.
- **계정 행 잠금 타이밍**: 실제 발송 직후 발송 작업이 자기 계정 `users` 행을 FOR SHARE로 잠시 잡는다.
- **번호별 일일 한도 소진 악용**: 다른 사람이 한 번호의 하루 5회를 소진할 수 있다(기존 정책과 같음).
- **Mock 인증 환경의 접근 제한**: 8절의 Mock SMS 환경이 외부에 열려 있으면 누구나 인증을 통과할 수 있는지 확인해야 한다.

## 8. SMS 정책 변경(Mock SMS)

확정(2026-11-25):
- 최종 배포 환경은 실제 사용자 운영이 아니라 기능 테스트와 시연 목적이므로 **SOLAPI를 연동하지 않고 Mock SMS를 사용**한다.
- 16-2는 "SOLAPI 실제 SMS 어댑터 구현"에서 **"Mock SMS 테스트 환경 구성"**으로 바뀐다. 16-3은 SOLAPI 환경변수를 제외한다. 16-8은 **Kakao 외부 서비스 설정**만 다룬다.
- 기존 `MockSmsProvider`(`app/sms_provider.py`)와 #110의 비동기 발송 구조를 재사용한다. 인증 API, 만료, 재발송 제한, 일일 한도, 실패 횟수, `SUPPRESSED` 차단 정책은 유지한다.
- 실제 사용자 운영 환경과 테스트 환경을 구분하고, 테스트 계정과 합성 데이터를 쓴다.

### 16-2 Mock SMS 테스트 환경 (#124)

**고정 인증번호는 실제 전화번호의 소유를 증명하지 않는다.** 이 모드로 인증된 번호는 합성 테스트 데이터로만 취급하고, 시연 DB는 실제 사용자 데이터와 분리한다.

**설정**(값은 코드, Issue, PR, 문서, 로그에 적지 않는다. 고정 인증번호는 담당자가 팀원에게 별도로 전달한다)

| 환경변수 | 시연 서버 값 | 규칙 |
| --- | --- | --- |
| `APP_ENV` | `development` | Mock은 `local`, `development`, `test`에서만 허용 |
| `SMS_PROVIDER` | `mock` | 서버 실행용 Mock은 발송 내용(번호, 문장)을 보관하지 않는다. |
| `SMS_MOCK_FIXED_CODE` | 비밀로 전달 | ASCII 숫자 정확히 6자리. 빈 값이나 미설정이면 기존 무작위 인증번호 |
| `PHONE_VERIFICATION_HMAC_KEY` | 환경별 비밀값 | 32자 이상. 없으면 발송과 확인이 500 |
| `ACCOUNT_EXIT_RECORD_POLICY` | 미설정 유지 | |

- `SMS_MOCK_FIXED_CODE`에 값이 있는데 다른 공급자, 공급자 미설정, 허용되지 않는 `APP_ENV`, 형식 오류 중 하나면 두 발송 API 모두 503 `SMS_DELIVERY_UNAVAILABLE`이고 아무것도 기록하지 않는다.
- SOLAPI는 연동하지 않는다. 코드 삭제는 20단계로 보류한다. 현재 `SMS_PROVIDER=solapi`는 SOLAPI 설정값이 하나라도 비면 발송 API가 503이다. 모두 있으면 placeholder 객체가 만들어져 발송 API는 200으로 접수하고, 비동기 발송 단계에서 `FAILED`가 된다.

**내부 합성 테스트 번호 대역 `010-0000-0000`~`010-0000-9999`**(공식 테스트 전용 예약 번호라는 뜻은 아니다)
- 고정 인증번호 모드의 발송은 이 대역만 받는다. 대역 밖 번호는 슬롯 확보와 DB 기록 전에 422 `INVALID_PHONE_NUMBER`(`{"field":"phone_number"}`)이며, 응답은 번호의 등록 여부와 무관하다. 온보딩과 프로필 번호 변경 모두 같다.
- `SMS_MOCK_FIXED_CODE`에 값이 있으면(형식 오류 포함) 대역 밖 번호의 증빙으로 Step 1 저장과 프로필 번호 변경을 할 수 없다(409 `PHONE_VERIFICATION_REQUIRED`). 모드를 켜기 전에 만든 증빙도 같다.
- 담당별 권장 대역(`010-0000-XXXX`, 코드로 강제하지 않음). 번호별 하루 5회와 번호 UNIQUE가 그대로이므로 사람마다 다른 번호를 쓴다.

| 담당 | XXXX |
| --- | --- |
| 16-2 담당자(사용자) | 1000~1999 |
| 다른 BE 팀원 | 2000~2999 |
| FE 봉사자 앱 | 3000~3999 |
| FE 보호소 웹 | 4000~4999 |
| Seed 데이터(#116) | 9000~9999 |

**유지되는 #110 정책**: 비동기 발송(worker 2개, 대기 20개), 발송 200은 접수, `SUPPRESSED` 차단(고정 인증번호로도 422), 요청 ID에 묶인 HMAC 저장, 만료 300초, 재발송 60초, 계정과 번호별 하루 5회, 실패 5회, 최신 요청만 유효, 목적 분리, 저장 시 재검증과 재사용 방지. 인증번호 조회 API, 발송 상태 API, 신규 인프라는 없다.

**환경 전환 영향**(설정 변경은 컨테이너 재생성으로만 반영)
- 무작위에서 고정으로: 대역 밖 번호의 기존 증빙은 저장할 수 없다.
- 고정에서 무작위나 다른 환경으로: 이미 고정 인증번호로 만든 요청(대역 안 번호만 존재)은 만료(300초)까지 확인되고, 인증 후 15분까지 저장될 수 있다. 만료 뒤의 확인은 410 `VERIFICATION_EXPIRED`, 저장 유효 시간이 지난 저장은 409 `PHONE_VERIFICATION_REQUIRED`다.
- 로컬 `.env`나 셸에 `SMS_MOCK_FIXED_CODE`를 둔 채 저장소 테스트를 실행하면 기존 인증 테스트가 실패한다. 테스트 전에는 이 값을 비운다.

**FE 테스트 방법**은 [HANDOFF-FE.md 5절](HANDOFF-FE.md#5-110과-mock-sms-반영)에 있다.

**16-3에 넘길 환경변수**: 위 표의 `APP_ENV`, `SMS_PROVIDER`, `SMS_MOCK_FIXED_CODE`, `PHONE_VERIFICATION_HMAC_KEY`를 배포 설정에 반영하고 SOLAPI 환경변수를 제거한다. 16-2는 `deploy/` 파일을 바꾸지 않았다.

**최초 시연 서버 배포 전 필수 검증**: Mock SMS 동작(대역 안 번호의 발송, 확인, 저장과 대역 밖 번호의 422), 서버 접근 제한, #110의 남은 보안 위험(7절). [HANDOFF-ROADMAP.md 7절](HANDOFF-ROADMAP.md#7-외부-공개-전-필수-검증)
