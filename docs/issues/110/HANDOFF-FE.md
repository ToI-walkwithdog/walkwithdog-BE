# FE와 BE 연동 가이드 (17단계)

실제 FE 코드와 BE 코드를 비교해, FE를 어떻게 고쳐 서버에 연결할지 정리했다. BE API 명세를 다시 적기보다, 화면마다 **무엇을 무엇으로 바꾸는지**에 집중한다. 세부 계약은 링크한 BE 문서를 기준으로 한다.

- 조사 기준: FE `main` `04e497e056eb078ad584708226202f5bfbd2bb6f`(봉사자 앱 `app/`, `src/`, 보호소 웹 `shelter-web/`), BE `dev` `f5b230e696f3b7461eda5cf36f5266990e5f65df`
- 표기: **확정**(사용자 승인), **권장**(제안), **미확정**(결정 필요), **미확인**(이번 조사에서 확인하지 못함)
- 범위 밖: FE Issue 작성, 실제 FE 코드 수정, FE 역할 분담

## 목차

1. [조사 결론](#1-조사-결론)
2. [공통 API 연동](#2-공통-api-연동)
3. [화면별 연동 현황](#3-화면별-연동-현황)
4. [화면별 수정 가이드](#4-화면별-수정-가이드)
5. [#110과 Mock SMS 반영](#5-110과-mock-sms-반영)
6. [권장 연동 순서](#6-권장-연동-순서)
7. [Notion과 코드의 차이, 미확인 범위](#7-notion과-코드의-차이-미확인-범위)
8. [FE와 BE 기능 차이 분류](#8-fe와-be-기능-차이-분류)

## 1. 조사 결론

- **봉사자 앱은 서버와 전혀 연결되어 있지 않다.** HTTP 클라이언트(`fetch`, axios 등)가 없다. 모든 상태는 `src/context/MockSession.tsx`(`MockSessionProvider`, `useMockSession()`)와 `src/data/*.ts`의 예시 데이터이며, `app/_layout.tsx`가 Provider를 설치한다. 환경변수는 지도용 `EXPO_PUBLIC_KAKAO_NATIVE_APP_KEY`, `EXPO_PUBLIC_WALK_MAP_MOCKUP`뿐이다.
- **보호소 웹(`shelter-web/`)도 데모 전용이다.** `shelter-web/src/DemoContext.tsx`와 `shelter-web/src/data/demo.ts`를 쓰고 서버 호출이 없다.
- **BE는 대부분의 화면에 필요한 API를 갖췄다**(로그인부터 선호 설정까지 01~15단계). 따라서 17단계의 핵심은 FE의 Mock 상태를 서버 상태로 바꾸는 작업이다.
- **오류 형식이 두 가지다.**
  - 계약형 API(회원가입, 로그인, 온보딩, 정책, 지역, 보호소 코드, 프로필, 선호)는 `{code, data, message}`다.
  - 산책과 예약 일부(`/walks/*`, `/walk-requests` 일부, `/dogs/*` 일부)는 FastAPI 기본 `{detail}`이며, 일부는 `detail.code`가 있다.
  - 공통 클라이언트에서 둘 다 처리해야 한다(2절).
- **서버 준비 상태**: 실제 개발 DB 적용(16-6), 초기 데이터(16-7), 정책 문서 등록(16-9) 전에는 온보딩 완료와 예약 흐름을 실서버에서 끝까지 검증할 수 없다(6절).

## 2. 공통 API 연동

기존 FE 구조(화면 컴포넌트, 라우팅, `useMockSession()` 사용처)를 최대한 유지한다. Mock 함수를 같은 이름의 서버 호출로 바꾸는 방식을 권장한다.

| 항목 | 권장 방식 |
| --- | --- |
| Base URL | `EXPO_PUBLIC_API_BASE_URL`(신규). 개발 서버 주소는 16-10 이후 사용자가 전달한다. |
| 공통 클라이언트 | `src/api/client.ts`(신규): `fetch` 래퍼. JSON 직렬화, `Authorization: Bearer <access_token>`, timeout(권장 10초, `AbortController`), 오류 정규화 |
| 인증 토큰 | `POST /auth/login` 응답 `access_token`, `expires_in`(초). 기기 안전 저장소(`expo-secure-store`, 새 의존성)를 권장한다. 로그아웃은 `POST /auth/logout` 후 로컬 토큰을 삭제한다. |
| 인증 만료 | 401(`AUTHENTICATION_REQUIRED`, `INVALID_ACCESS_TOKEN`, `SESSION_EXPIRED`) 시 토큰을 지우고 `/1-2`로 이동한다. 갱신 토큰은 없다(#13 미구현). |
| 오류 정규화 | `{code, data, message}`면 그대로, `{detail: string}`이면 `{code: null, message: detail}`, `{detail: {code, ...}}`이면 `detail.code`를 쓴다. 화면 분기는 HTTP 상태와 `code`로만 한다. 문구 비교는 하지 않는다. |
| 날짜와 시각 | 서버 응답 시각은 UTC ISO 8601이다. 화면 표시는 KST로 바꾼다. 예약 슬롯의 일자와 시각은 KST 현지 값이다(#29). 생년월일은 `YYYY-MM-DD`다. |
| Idempotency-Key | `POST /walk-requests`는 UUID `Idempotency-Key` 헤더가 필요하다. 화면 진입 시 한 번 만들고, 같은 신청의 재시도에 재사용한다. 마커 `client_marker_id`, 신고 `client_report_id`도 같은 원칙이다(#89). |
| 버전 | 프로필 `profile_version` → `expected_profile_version`, 선호 `version` → `expected_version`, 개인 메모 `expected_version`. 409 버전 충돌이면 다시 조회하고 사용자에게 확인받는다. |
| 응답 유실 | 결과가 불확실하면 다시 보내기 전에 서버 상태를 조회한다. 예약은 상세, 산책은 `GET /walks/active`, 인계는 산책 상세, 메모는 요약을 본다. |
| 개인정보 | 전화번호, 인증번호, 토큰, 메모, 좌표를 콘솔과 분석 로그에 남기지 않는다. 화면 이탈과 로그아웃 시 메모리 상태를 지운다. |

수정 전후 예시(권장):

```ts
// 전: src/screens/onboarding/LoginScreen.tsx
if (id && password) router.replace("/3");

// 후
const res = await api.post("/auth/login", { identifier: id, password });
if (!res.ok) return showError(res.code);          // INVALID_CREDENTIALS 등
await saveToken(res.data.access_token);
const me = await api.get("/users/me");
router.replace(routeForOnboarding(me.data));      // next_onboarding_step 기준
```

## 3. 화면별 연동 현황

상태 구분은 다음과 같다.
- **Mock 사용**: FE가 예시 데이터를 쓰며, 대응 BE API가 있다.
- **FE 전용**: 서버 호출이 필요 없다.
- **BE 없음**: 대응 API가 없다.
- **계약 불일치**: FE 입력과 BE 계약이 다르다.
- **실환경 필요**: 실서버, 데이터, 정책 확정 후 검증해야 한다.
- **조사 불가**: 이번 조사에서 확인하지 못했다.

"연동 완료"인 화면은 없다.

FE 파일 경로는 봉사자 앱 `src/screens/` 기준이다. 폴더가 생략된 파일은 바로 위 행과 같은 폴더에 있다. 라우트와 파일 대응은 FE `README.md`의 화면 표와 같다.

| 화면 | FE 파일 | BE API | 상태 | 수정 | 관련 Issue | 우선순위 | 확인된 문제 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1-1 시작 | `onboarding/WelcomeScreen.tsx` | `GET /users/me`(토큰 복원) | Mock 사용 | FE | #45 | 1 | 5초 후 무조건 로그인 화면 |
| 1-2 로그인 | `onboarding/LoginScreen.tsx` | `POST /auth/login`, `GET /users/me` | Mock 사용 | FE | #30, #32, #45 | 1 | 인증 없이 홈 이동 |
| 1-3 회원가입 | `onboarding/RegisterScreen.tsx` | `GET /users/id-availability`, `GET /users/nickname-availability`, `POST /users/` | Mock 사용, 계약 불일치 | FE | #30, #31 | 1 | 동의 정보를 `privacy`로 보내지 않음. 닉네임만 로컬 저장 |
| 1-4, 1-5 개인정보 | `onboarding/PrivacyPolicyScreen.tsx`, `PrivacyDetailsScreen.tsx` | `GET /policies/privacy` | Mock 사용, 실환경 필요 | FE | #76 | 1 | 체크박스 기본 선택. 정책 문서 미등록(16-9) |
| policy-info | `onboarding/PolicyInfoScreen.tsx` | `GET /policies/terms`, `GET /policies/privacy` | Mock 사용, 실환경 필요 | FE | #76 | 2 | 준비 중 문구 |
| 1-6 서약 | `onboarding/PledgeScreen.tsx` | `PUT /users/me/pledge` | Mock 사용 | FE | #33 | 1 | 저장 안 함 |
| 2-1 대기 | `onboarding/OnboardingWaitingScreen.tsx` | `GET /users/me` | FE 전용(서버 상태로 분기) | FE | #45 | 2 | 1.5초 후 자동 이동 |
| 2-2 기본 정보 | `onboarding/UserInput1Screen.tsx` | 발송, 확인, `PUT /users/me/onboarding/basic-info` | Mock 사용, 실환경 필요 | FE | #40, #110, #124 | 1 | 앱 안에서 고정값과 비교. BE 확인 API 호출로 교체(5절) |
| 2-3, 2-4 반려견 경험 | `UserInput2Screen.tsx`, `UserInput3Screen.tsx` | `GET/PUT /users/me/onboarding/dog-experience` | Mock 사용 | FE | #42 | 1 | 화면 간 값 전달과 저장 없음 |
| 2-5 활동 방식, 동의 | `UserInput4Screen.tsx` | `GET /regions/sido`, `GET /regions/sigungu`, `GET/PUT /users/me/onboarding/walking-routine`(`agreements`) | Mock 사용, 계약 불일치 | FE | #43, #44, #78 | 1 | 지역 자유 입력. BE는 지역 코드 1~3개 |
| 2-6 위치 | `UserInput5Screen.tsx` | `POST /regions/resolve-location` | Mock 사용, 실환경 필요 | FE | #46 | 3 | API 호출 없음. Kakao 결정 대기(16-8) |
| 3 홈 | `home/HomeScreen.tsx` | `GET /users/me`, `GET /walks/active`, `GET /walk-requests/my?startable_only=true&limit=1`, `GET /walk-guides/first-walk/status` | Mock 사용 | FE | #45, #84, #59 | 2 | 로컬 예약과 기록 사용 |
| 7-1, 7-2 산책 가능 목록 | `booking/AvailableBookingsScreen.tsx` | `GET /dogs/walk-availability`, `GET /users/me/walk-preferences`, `GET /users/me/onboarding/walking-routine` | Mock 사용, 실환경 필요 | FE | #29, #105 | 2 | 지역 고정 문구. 초기 데이터 필요(16-7) |
| 7-3 강아지 상세 | `booking/DogDetailsScreen.tsx` | `GET /dogs/{dog_id}/walk-availability` | Mock 사용 | FE | #29 | 2 | 일정 인덱스 사용 |
| 7-4, 7-5 신청 | `booking/ApplicationReviewScreen.tsx`, `ApplicationCompleteScreen.tsx` | `POST /walk-requests`(`Idempotency-Key`) | Mock 사용 | FE | #29 | 2 | 멱등 키 없음 |
| 8-1, 8-2 내 예약 | `bookings/MyBookingsScreen.tsx` | `GET /walk-requests/my`(`status`, `status_group`) | Mock 사용 | FE | #84 | 2 | 지난 예약 정의 다름 |
| 8-3~8-5 상세 | `bookings/BookingDetailScreen.tsx` | `GET /walk-requests/{id}`(`actions`) | Mock 사용 | FE | #84 | 2 | 예약 번호와 사유 고정 |
| 8-6~8-8 취소 | `bookings/BookingCancellationScreen.tsx` | `PATCH /walk-requests/{id}` `CANCEL` | Mock 사용 | FE | #84 | 2 | 사유 미전송, 대기 취소 없음 |
| 4-1~4-3 보호소 코드 | `walk/ShelterCodeScreen.tsx`(`CodeEntryResultScreen.tsx`는 어느 라우트에서도 쓰지 않음) | `POST /walk-requests/{id}/shelter-code/verify` | Mock 사용, 실환경 필요 | FE | #87 | 3 | 코드 `428619` 고정. 보호소 발급 필요 |
| 4-4 출발 체크 | `walk/WalkChecklistScreen.tsx` | `POST /walks` | Mock 사용 | FE | #87 | 3 | 체크 후 시작 API 없음 |
| 5-1, 5-2, 5-4 산책 중, 마커 | `walk/WalkScreen.tsx` | `GET /walks/active`, `POST /walks/{id}/points`, `POST /walks/{id}/markers` | Mock 사용, 실환경 필요 | FE | #89 | 3 | 통계 고정, GPS 미수집 |
| 5-3 GPS 끊김 | `walk/GPSLostScreen.tsx` | `GET /walks/active`(`last_point_received_at`, `auto_end_at`) | Mock 사용 | FE | #89 | 4 | 자동 진입 없음 |
| 5-5~5-7 신고 | `walk/WalkScreen.tsx`, `OtherReportScreen.tsx`, `ReportResultScreen.tsx` | `POST /walks/{id}/incidents`(`client_report_id`) | Mock 사용 | FE | #89 | 3 | 미전송 |
| 산책 종료 | `walk/WalkScreen.tsx` | `PATCH /walks/{id}/end` | Mock 사용 | FE | #89 | 3 | 종료 API 없음 |
| 6-1 인계 | `walk/WalkHandoverScreen.tsx` | `POST /walks/{id}/handover` | Mock 사용 | FE | #93 | 3 | 체크만 로컬 |
| 6-2 기록 저장 | `walk/WalkRecordSaveScreen.tsx` | `GET /walks/{id}/summary`, `PUT /walks/{id}/private-memo` | Mock 사용 | FE | #95 | 3 | 로컬 기록 생성 |
| 6-3 레벨업 | `walk/LevelUpScreen.tsx` | `GET /walks/{id}/summary`(`level_evaluation_status`, `level_up_event`), `GET /volunteers/me` | Mock 사용 | FE | #97 | 4 | 자동 진입 없음, 레벨 고정 |
| 9-1, 9-2 이력 | `history/WalkHistoryScreen.tsx`, `WalkRecordDetailScreen.tsx` | `GET /walks/history`, `GET /walks/{id}`, `GET /walks/{id}/summary` | Mock 사용 | FE | #99 | 3 | 로컬 기록 |
| 10-1 마이페이지 | `mypage/MyPageScreen.tsx` | `GET /volunteers/me`, `GET /users/me`, `POST /auth/logout` | Mock 사용 | FE | #99 | 3 | 레벨 3 고정 |
| 10-2 내 정보 수정 | `mypage/EditProfileScreen.tsx` | `GET/PATCH /users/me/profile`, 프로필 발송과 확인 | Mock 사용, 실환경 필요 | FE | #103, #110 | 3 | 번호 변경 미지원 안내. OTP 입력 UI 없음 |
| 10-3 선호 | `mypage/WalkPreferencesScreen.tsx` | `GET/PATCH /users/me/walk-preferences` | Mock 사용 | FE | #105 | 3 | 지역 고정 표시(BE도 지역 미지원) |
| settings | `mypage/SettingsScreen.tsx` | 없음 | FE 전용 | 없음 | | 4 | |
| 보호소 웹 전체 | `shelter-web/src/pages/*.tsx` | 4.8절 | Mock 사용(데모) | FE | #29, #61, #84, #87, #89 | 4 | 데모 데이터, 코드 발급과 실시간 화면 없음 |

보호소 웹은 실제 코드(`shelter-web/src/`)를 조사해 [4.8](#48-보호소-웹-shelter-web)에 화면별로 정리했다.

## 4. 화면별 수정 가이드

각 화면 묶음마다 다음을 적었다: 현재 구현(파일, 컴포넌트, Mock 사용처), 연결할 BE(라우터 위치), 수정 대상, 처리 규칙, 테스트. 요청과 응답 필드의 정확한 정의는 링크한 BE 문서를 따른다.

### 4.1 시작, 로그인, 회원가입, 정책 (1-1~1-6, policy-info)

- **현재**
  - `WelcomeScreen`은 `setTimeout` 뒤 `router.replace("/1-2")`
  - `LoginScreen`은 입력이 비어 있지 않으면 `router.replace("/3")`
  - `RegisterScreen`은 `useMockSession().updateProfile`로 닉네임만 저장하고 `push("/1-6")`
  - 정책 화면은 준비 중 문구이고 체크박스가 미리 선택되어 있다.
- **BE**
  - `app/routers/auth.py`: `POST /auth/login`(`identifier`, `password` → `TokenResponse{access_token, token_type, expires_in}`), `POST /auth/logout`
  - `app/routers/users.py`: `POST /users/`(`login_id`, `nickname`, `password`, `privacy`), 중복 확인 API, `GET /users/me`
  - `app/routers/policies.py`: `GET /policies/{kind}`
  - `PUT /users/me/pledge`
  - 문서: [#30](../30/HANDOFF.md), [#31](../31/HANDOFF.md), [#33](../33/HANDOFF.md), [#45](../45/HANDOFF.md), [#76](../76/HANDOFF.md)
- **수정**
  - `WelcomeScreen`: 저장된 토큰이 있으면 `GET /users/me`로 진입 화면을 정하고, 없거나 401이면 `/1-2`로 간다.
  - `LoginScreen`: 2절 예시처럼 로그인 후 `next_onboarding_step` 기준으로 이동한다. 값별 이동: `AGE_REVIEW`(생년월일 정정, BE `PUT /users/me/age-review/birth-date`, FE 화면 없음), `PLEDGE` → `/1-6`, `BASIC_INFORMATION` → `/2-2`, `PET_EXPERIENCE` → `/2-3`, `WALKING_ROUTINE` → `/2-5`, `null` → `/3`
  - `RegisterScreen`: 입력 중 아이디와 닉네임 중복 확인 API를 호출한다. 가입 시 `/1-4`의 동의 결과를 `privacy`로 보낸다(문서 버전은 `GET /policies/privacy`의 값). 가입 후 바로 로그인해 토큰을 받는다.
  - 정책 화면: 체크박스 기본 선택을 해제하고, 서버 문서와 버전을 표시한다.
  - `PledgeScreen`: `PUT /users/me/pledge` 성공 후 이동한다.
- **처리**: 가입 rate limit(IP당 600초 5회), 로그인 rate limit(IP당 60초 10회)의 429를 안내한다. 정책 문서 503(`POLICY_DOCUMENT_UNAVAILABLE`)이면 가입을 막고 재시도를 안내한다.
- **테스트**: 정상 가입과 로그인, 중복 아이디와 닉네임, 잘못된 비밀번호, 토큰 만료 후 재진입, 단계별 진입 화면

### 4.2 온보딩 Step 1~3, 위치 (2-1~2-6)

- **현재**
  - `UserInput1Screen`은 이름, 생년월일, 전화번호를 입력받고 테스트 코드 `123456`으로 확인한다. `updateProfile` 뒤 `push("/2-3")`.
  - `UserInput2Screen`, `UserInput3Screen`, `UserInput4Screen`은 각 화면 내부 상태만 쓰고 저장하지 않는다.
  - `UserInput5Screen`은 위치 권한만 요청하고 홈으로 간다.
- **BE**
  - `app/routers/onboarding_basic.py`: 발송, 확인, `GET/PUT /users/me/onboarding/basic-info`
  - `app/routers/onboarding_experience.py`: `GET/PUT /users/me/onboarding/dog-experience`
  - `app/routers/onboarding_routine.py`: `GET/PUT /users/me/onboarding/walking-routine`(지역 코드 1~3개, 시간대 1~4개, 시간과 크기 코드, `agreements`)
  - `app/routers/regions.py`: 시도와 시군구 목록, `POST /regions/resolve-location`
  - 문서: [#40](../40/HANDOFF.md), [#42](../42/HANDOFF.md), [#43](../43/HANDOFF.md), [#44](../44/HANDOFF.md), [#46](../46/HANDOFF.md), [#78](../78/HANDOFF.md), [#110](HANDOFF.md)
- **수정**
  - Step 1: 발송 → 확인 → `PUT basic-info`(`full_name`, `birth_date`, `phone_verification_id`) 순으로 호출한다(5절).
  - Step 2: 두 화면의 값을 하나의 요청으로 모아 `PUT dog-experience`로 저장한다(화면 간 전달 상태가 필요하다).
  - Step 3: 지역 자유 입력을 시도와 시군구 선택으로 바꾸고 **지역 코드**를 보낸다. 시간, 크기, 시간대를 서버 코드로 매핑한다(예: '30분 이내' → `UP_TO_30_MIN`, '소형견' → `SMALL`). 필수 동의 두 개를 `agreements`로 보낸다.
  - 위치: 권한이 허용되면 좌표로 `resolve-location`을 호출해 지역 선택 초기값으로 쓴다. 422 `REGION_NOT_SUPPORTED`나 503 `GEOCODING_UNAVAILABLE`이면 수동 선택으로 간다.
- **처리**
  - 각 단계는 서버 상태로 재진입한다(`GET` 단계 API, `GET /users/me`).
  - 같은 입력의 재저장은 200, 다른 입력은 409(`*_ALREADY_COMPLETED`)이므로 재조회 후 다음 단계로 이동한다.
  - 만 14세 미만은 422와 함께 계정이 나이 확인 대기로 바뀐다(#41).
- **테스트**: 단계 순서 강제(409 `PREVIOUS_STEP_NOT_COMPLETED`), 재진입, 코드 매핑, 위치 거부와 실패

### 4.3 홈, 목록, 상세, 신청 (3, 7-1~7-5)

- **현재**
  - `HomeScreen`은 `useMockSession()`의 `bookings`, `records`, `profile`을 쓴다.
  - `AvailableBookingsScreen`은 `src/data/availableBookings.ts`와 `preferences.size`를 쓴다.
  - `DogDetailsScreen`과 `ApplicationReviewScreen`은 `draftDogId`, `draftScheduleIndex`, `applyForBooking()`을 쓴다.
- **BE**
  - `app/routers/dogs.py`의 `availability_router`: `GET /dogs/walk-availability`(`region_codes` 1~3개 필수, `size_code`, `limit`, `offset`), `GET /dogs/{dog_id}/walk-availability`
  - `app/routers/walk_requests.py`: `POST /walk-requests`
  - 문서: [LATEST #29](../../handoff/LATEST.md), [#105](../../handoff/LATEST.md)
- **수정**
  - 목록의 초기 지역은 `GET /users/me/onboarding/walking-routine`의 `region_codes`다.
  - 초기 크기는 사용자가 목록에서 고른 값, `GET /users/me/walk-preferences`의 `effective_preferences.size_code`, 전체 순서로 정한다. 목록에서 바꾼 값은 저장하지 않는다.
  - 일정은 인덱스 대신 서버의 `date_slot_id`를 쓴다.
  - 신청은 `date_slot_id`, 시작과 종료(오프셋 포함), `Idempotency-Key`를 보낸다.
- **처리**
  - `WALK_SLOT_CHANGED`면 상세를 다시 조회한다. `WALK_SLOT_UNAVAILABLE`이면 다른 일정을 안내한다. `WALK_REQUEST_RETRYABLE`이면 같은 키로 재시도한다.
  - 온보딩 미완료 사용자의 신청은 거절된다(#81).
- **테스트**: 지역과 크기 필터, 빈 목록, 동시 신청, 같은 키 재전송, 응답 유실 후 내 예약 확인

### 4.4 내 예약, 상세, 취소 (8-1~8-8)

- **현재**: `MyBookingsScreen`은 `bookings`, `selectBooking`을 쓰고, `BookingDetailScreen`과 `BookingCancellationScreen`은 `selectedBooking`, `cancelBooking()`을 쓴다.
- **BE**: `GET /walk-requests/my`, `GET /walk-requests/{id}`, `PATCH /walk-requests/{id}`. 문서는 [LATEST #84](../../handoff/LATEST.md).
- **수정**
  - 탭을 `status`(`PENDING`, `APPROVED`)와 `status_group=PAST`로 매핑한다. 두 필터를 함께 보내면 422다.
  - 화면 이동은 상세의 `actions`로 정한다.
  - 취소 사유(`SCHEDULE_CHANGED`, `CANNOT_VISIT`)를 보낸다. 대기 중인 신청 취소도 허용된다.
- **처리**: 재전송 409 `REQUEST_ALREADY_CANCELLED`나 응답 유실이면 상세를 다시 조회한다.
- **테스트**: 상태별 탭, 취소 가능 시점, 중복 취소

### 4.5 보호소 코드, 출발, 산책 중, 신고, 종료 (4-1~5-7)

- **현재**
  - `ShelterCodeScreen`은 `src/data/shelterCodes.ts`의 `428619`와 비교한다.
  - `WalkChecklistScreen`은 `clearMarkers()` 뒤 `/5-1`로 간다.
  - `WalkScreen`은 고정 통계와 `addMarker()`를 쓰고, 신고는 저장하지 않는다.
- **BE**
  - `app/routers/shelter_codes.py`: 검증
  - `app/routers/walks.py`: `POST /walks`, `GET /walks/active`, `points`, `markers`, `incidents`, `PATCH /walks/{id}/end`
  - 문서: [LATEST #87, #89](../../handoff/LATEST.md)
- **수정**
  - 코드 화면은 선택한 예약 ID로 `verify`를 호출하고, 체크리스트 완료 시 `POST /walks`를 호출한다.
  - 산책 화면은 `expo-location`으로 좌표를 모아 `points`에 순번과 함께 보낸다.
  - 마커는 `client_marker_id`와 `position_recorded_at`을 붙이고, 신고는 `client_report_id`를 붙인다. `OTHER`는 설명 1~1,000자다.
  - 종료는 `PATCH end`로 한다.
- **처리**
  - 코드 422 `SHELTER_CODE_INVALID`, 429 `SHELTER_CODE_RATE_LIMITED`(`retry_after_seconds`)를 처리한다.
  - 시작 409는 `detail.code`로 나눈다: `WALK_START_CODE_REQUIRED`, `WALK_START_BOOKING_NOT_ELIGIBLE`, `WALK_START_ALREADY_STARTED`, `WALK_START_RETRYABLE`.
  - 앱을 다시 열면 `GET /walks/active`로 복구한다. `auto_end_at`을 지나면 서버가 자동 종료한다.
  - GPS가 없어도 신고는 막지 않는다.
- **테스트**: 실제 기기 GPS, 백그라운드와 재시작 복구, 신고 재시도 멱등, 자동 종료 후 화면 처리

### 4.6 인계, 기록, 메모, 레벨업, 이력 (6-1~6-3, 9-1, 9-2)

- **현재**: `WalkHandoverScreen`은 로컬 체크만 하고, `WalkRecordSaveScreen`은 `saveRecord()`로 로컬 기록을 만든다. 이력은 `records`, `selectRecord`를 쓴다.
- **BE**
  - `POST /walks/{id}/handover`
  - `PUT /walks/{id}/private-memo`
  - `GET /walks/{id}/summary`(`private_memo`, `handover_status`, `level_evaluation_status`, `level_up_event`)
  - `GET /walks/history`
  - 문서: [LATEST #93, #95, #97, #99](../../handoff/LATEST.md)
- **수정**
  - 인계 체크를 `dog_returned`, `equipment_returned`, `condition_shared`와 `handover_result`로 보낸다. 자동 종료 산책은 `acknowledge_auto_timeout=true`가 필요하다.
  - 메모는 `expected_version`과 함께 저장한다.
  - 레벨업 화면은 요약의 `level_up_event`가 있을 때만 보여준다. `PENDING`이면 잠시 뒤 다시 조회한다.
  - 이력은 서버 목록과 `shelter_name`을 쓴다.
- **처리**: 인계 재제출은 200 기존 결과, 다른 내용은 409다. 메모 버전 충돌 409는 요약을 다시 조회한다.
- **테스트**: 인계 응답 유실 후 상세 확인, 메모 충돌, 레벨 평가 대기와 완료

### 4.7 마이페이지, 내 정보 수정, 선호 (10-1~10-3)

- **현재**: `MyPageScreen`은 레벨 3 고정과 로컬 집계를 쓴다. `EditProfileScreen`과 `WalkPreferencesScreen`은 `updateProfile`, `updatePreferences`로 메모리만 바꾼다.
- **BE**: `GET /volunteers/me`, `GET/PATCH /users/me/profile`, 프로필 발송과 확인, `GET/PATCH /users/me/walk-preferences`. 문서는 [LATEST #99, #103, #105](../../handoff/LATEST.md).
- **수정**
  - 마이페이지는 서버 레벨과 `history_*` 통계를 쓴다.
  - 내 정보 수정은 조회한 `profile_version`을 보낸다. 번호 변경은 5절의 흐름을 따르며, **인증번호 입력 UI를 새로 추가**해야 한다.
  - 선호는 `version`을 보낸다. 지역은 BE가 지원하지 않으므로 고정 문구를 유지하고 저장하지 않는다.
  - 로그아웃은 `POST /auth/logout`과 로컬 상태 정리다.
- **처리**: 409 `PROFILE_VERSION_MISMATCH`, `PREFERENCE_VERSION_MISMATCH`면 재조회 후 확인받는다. 저장 응답을 받은 뒤에만 화면을 닫는다.
- **테스트**: 버전 충돌, 번호 변경 전 과정, 다른 계정 번호(저장 시 409)

### 4.8 보호소 웹 (`shelter-web/`)

- **구조**: Vite와 React, 해시 라우팅이다.
  - `shelter-web/src/App.tsx`의 `App()`가 `window.location.hash`를 `page/action/id`로 나눠 화면을 고른다.
  - 상태는 `shelter-web/src/DemoContext.tsx`의 `DemoProvider`와 `useDemo()`가 관리한다(`useState` 메모리, 새로고침하면 초기화).
  - 초기값은 `shelter-web/src/data/demo.ts`의 `initialShelter`, `initialDogs`, `initialSchedules`, `initialBookings`, `initialReports`, `initialWalks`다.
  - 서버 호출은 없다.
- **공통 수정**
  - 봉사자 앱과 같은 원칙의 `shelter-web/src/api/client.ts`(신규, `fetch`, Bearer, 오류 정규화)를 둔다.
  - `DemoContext`의 함수 이름(`saveDog`, `addSchedule`, `updateBooking` 등)을 서버 호출로 바꾸거나, 페이지에서 직접 호출하도록 바꾼다.
  - 토큰은 웹이므로 메모리 또는 `sessionStorage`를 권장한다. XSS 노출을 고려해 `localStorage`는 피한다.
- **인증과 권한**
  - 로그인은 SHELTER 계정이고, `GET /users/me`의 `role`과 `shelter_id`로 담당 보호소를 정한다.
  - 보호소 API는 `require_shelter`, `require_shelter_access`로 자기 보호소만 허용한다(다른 보호소는 403).
  - `shelter_id`가 없는 SHELTER 계정은 관리자가 `PATCH /users/{id}/shelter`로 연결해야 한다.

#### 로그인 (`#login`, `pages/LoginPage.tsx`의 `LoginPage`)

- **현재**: `handleLogin()`은 "아직 연결되지 않았어요" 문구만 표시하고, `onPreview`는 대시보드로 간다.
- **수정**
  1. `POST /auth/login`(`identifier`, `password`)
  2. `GET /users/me`
  3. `role`이 `SHELTER`(또는 `ADMIN`)이고 `shelter_id`가 있으면 대시보드로 간다.
  4. `USER` 계정이나 `shelter_id`가 없으면 안내 후 로그아웃한다.
  - 미리보기 버튼은 데모 확인용으로 남길지 FE가 정한다.
- **오류**: 401 `INVALID_CREDENTIALS`, 429(IP당 60초 10회)
- **테스트**: 보호소, 관리자, 일반 사용자 계정별 진입. 소속 없는 보호소 계정

#### 보호소 인증 (`#verification`, `pages/SettingsPage.tsx`의 `VerificationPage`)

- 현재 화면 자체가 "인증 방법, 서류, 승인 기준이 정해지지 않았다"고 안내한다.
- 연동 대상이 아니다. 8절 분류를 본다.

#### 대시보드 (`#dashboard`, `pages/DashboardPage.tsx`의 `DashboardPage`)

- **현재**: `useDemo()`의 `shelter`, `dogs`, `schedules`, `bookings`, `reports`에서 대기 예약 수와 미확인 신고 수를 계산한다.
- **BE**
  - 대기 예약: `GET /shelters/{shelter_id}/walk-requests?status=PENDING`
  - 진행 중 산책: `GET /shelters/me/live-walks`
  - 미확인 신고: `GET /shelters/me/incident-alerts`
  - 강아지 수: `GET /shelters/{shelter_id}/dogs`
- **수정**: 각 카드를 해당 API 결과로 바꾼다. 실패한 카드만 오류를 표시하고, 나머지는 보여준다.

#### 강아지 (`#dogs`, `#dogs/new`, `#dogs/edit/{id}`, `pages/DogsPage.tsx`의 `DogsPage`, `DogFormPage`)

- **현재**
  - 목록은 `dogs`와 `toggleDog()`(활성 전환)을 쓴다.
  - 폼은 `name`, `breed`, `age`(살), `size`(소형견, 중형견, 대형견), `difficulty`(0 이상, 0.1 단위), `notes`, `photo`(FileReader로 만든 data URL) 상태를 `saveDog()`로 저장한다.
- **BE**
  - 목록: `GET /shelters/{shelter_id}/dogs`(`walkable` 필터, `DogCardResponse`에는 산책 설정이 없다)
  - 상세: `GET /dogs/{dog_id}`(`walk_settings` 포함)
  - 신규: `POST /dogs/walk-registration`(multipart)
    - 필드: `name`, `breed`, `age`, `age_months`, `difficulty`, `precautions`, `size_code`, `difficulty_score`, `max_walk_minutes`, `shelter_id`
    - `slots`: JSON 배열 문자열, `image`: 파일 필수
  - 설정 수정: `PATCH /dogs/{dog_id}/walk-settings`
- **필드 매핑**

| FE | BE | 비고 |
| --- | --- | --- |
| `size` 소형견, 중형견, 대형견 | `size_code` `SMALL`, `MEDIUM`, `LARGE` | |
| `difficulty`(0.1 단위) | `difficulty_score`(1~5, **0.5 단위**) | 입력 단위를 BE에 맞춘다. 범위 밖이면 422 |
| 없음 | `difficulty` `BEGINNER`, `INTERMEDIATE`, `EXPERIENCED` | FE에 입력을 추가할지 정한다(기본 `BEGINNER`). |
| `notes` | `precautions` | |
| `photo` data URL | `image` 파일(multipart) | data URL 대신 원본 `File`을 보낸다. |
| `active` 전환 | `walkable`(`PATCH walk-settings`) | |
| 기본 정보(이름, 견종, 나이) 수정 | 해당 API 없음 | 8절 분류 |

- **오류**: 422(필드 검증), 403(다른 보호소), 413이나 이미지 형식 오류(업로드 제한은 BE 문서 #29 참고)
- **테스트**: 신규 등록과 사진, 설정 수정, 산책 가능 전환, 다른 보호소 강아지 접근 403

#### 산책 일정 (`#schedules`, `#schedules/new`, `pages/SchedulesPage.tsx`의 `SchedulesPage`, `ScheduleFormPage`)

- **현재**
  - 목록은 `schedules`와 `toggleSchedule()`(일시 중지와 재개)을 쓴다.
  - 폼은 `dogId`, `date`, `start`, `end`, `maxMinutes`(자유 숫자)를 `addSchedule()`로 저장한다.
- **BE**
  - `GET/POST /dogs/{dog_id}/date-slots`
  - `PATCH /dogs/{dog_id}/date-slots/{slot_id}`: `slot_date`, `start_time`, `end_time`, `duration_minutes`
  - `DELETE /dogs/{dog_id}/date-slots/{slot_id}`: `is_active=false`로 **영구 종료**(재개 API 없음)
- **필드 매핑**

| FE | BE | 비고 |
| --- | --- | --- |
| `date` | `slot_date`(KST 현지 날짜) | |
| `start`, `end` | `start_time`, `end_time`(KST 현지 시각) | |
| `maxMinutes`(자유 숫자) | `duration_minutes` **30, 60, 90 중 하나** | 입력을 선택지로 바꾼다. |
| 일시 중지 | `DELETE`(영구 종료) | 재개는 지원하지 않는다. 8절 분류 |

- **오류**: 겹침, 과거 시각, 예약이 있는 일정의 변경 거절(#29 계약). 응답 코드에 따라 목록을 다시 조회한다.
- **테스트**: 30, 60, 90분 등록, 겹침 거절, 예약이 걸린 일정 변경 시도

#### 예약 (`#bookings`, `#bookings/{id}`, `pages/BookingsPage.tsx`의 `BookingsPage`)

- **현재**
  - `bookings`, `dogs`, `schedules`를 보여주고, `pending`일 때 "목업 승인", "목업 거절" 버튼이 `updateBooking(id, "confirmed" | "rejected")`를 부른다.
  - 신청자 나이와 경험은 데모 값이다.
  - **보호소 코드 발급 UI가 없다.**
- **BE**
  - 목록: `GET /shelters/{shelter_id}/walk-requests?status=...`
  - 상세: `GET /walk-requests/{request_id}`
    - `applicant`: `nickname`, `name`, `age`, `dog_experience`, `experienced_breed`, `walk_frequency`
    - 그 밖에 `actions`, 일정, 보호소
  - 승인과 거절: `PATCH /walk-requests/{request_id}`(`action`: `APPROVE`, `REJECT`, 거절 사유 코드와 사유)
  - 코드 발급: `POST /walk-requests/{request_id}/shelter-code`
- **수정**
  - 상태 값을 `PENDING`, `APPROVED`, `REJECTED`, `CANCELLED`로 매핑한다.
  - 신청자 정보는 상세 응답의 `applicant`를 쓴다.
  - 승인된 예약에 "코드 발급" 버튼을 **새로 추가**한다(FE 작업이며 BE는 있다).
  - 발급 응답의 `shelter_code`(6자리 문자열)와 `expires_at`을 화면에만 표시하고 저장하거나 로그에 남기지 않는다.
- **오류**
  - 승인 시 일정 충돌이나 이미 처리됨(409)이면 다시 조회한다.
  - 코드 발급은 예약 시간 밖이거나 이미 시작한 예약이면 거절된다(#87). 재발급하면 이전 코드가 무효가 된다.
- **테스트**: 승인, 거절, 중복 승인 경합, 시간 안과 밖의 코드 발급, 재발급

#### 산책 현황 (`#live`, `pages/OperationsPage.tsx`의 `LivePage`)

- **현재**: "실시간 연결 전" 안내만 있고 데이터가 없다.
- **BE**
  - `GET /shelters/me/live-walks`: 카드에 `walk_id`, `dog_name`, `walker_nickname`, `started_at`, `elapsed_seconds`, `scheduled_end_at`, `is_overdue`, `overdue_minutes`, `last_latitude`, `last_longitude`, `last_location_at`, `is_stale`가 있다.
  - `GET /shelters/me/live-walks/{walk_id}`
  - `GET /shelters/me/live-walks/{walk_id}/updates`
- **수정**: 카드 목록 UI를 새로 만들고 주기적으로 조회한다(폴링 간격은 FE가 정한다, 권장 10~30초). 위치는 담당 보호소 화면에서만 표시하고 저장하지 않는다.
- **테스트**: 진행 중 0건과 여러 건, 시간 초과 표시, GPS 끊김(`is_stale`)

#### 신고 (`#reports`, `#reports/new`, `pages/OperationsPage.tsx`의 `ReportsPage`, `ReportFormPage`)

- **현재**
  - `reports`(종류 `incoming` 봉사자 접수, `outgoing` 보호소 작성)와 `reviewReport()`를 쓴다.
  - 작성 폼은 `person`, `dogName`, `title`, `message`를 받아 `addReport()`로 저장한다.
- **BE**
  - 봉사자 신고 조회: `GET /shelters/me/incidents`, `GET /shelters/me/incident-alerts`
  - 확인 처리: `POST /shelters/me/incident-alerts/{incident_id}/ack`
  - 보호소의 신고 접수: 산책에 연결된 신고는 기존 `POST /walks/{walk_id}/incidents`로 가능하다. 코드상 자기 보호소 산책이면 담당자와 관리자도 허용되고(`authorize_action`), 실행 검증은 미확인이다.
- **수정**
  - 수신 목록과 확인 처리는 위 API로 바꾼다.
  - 작성 폼은 자유 입력(대상자, 강아지 이름) 대신 **산책을 선택**해 `incident_type`, `description`, `occurred_at`(필수, 오프셋 포함), `client_report_id`로 보내는 방식이어야 한다.
  - 산책과 무관한 신고는 8절 분류를 본다.
- **테스트**: 미확인 목록, 확인 처리 후 재조회, 산책 연결 신고 접수와 재시도 멱등

#### 산책 이력과 평가 (`#history`, `#history/{id}`, `pages/OperationsPage.tsx`의 `HistoryPage`)

- **현재**: `walks`(강아지 이름, 신청자, 날짜, 시간, 거리, 메모, 평가)와 `saveEvaluation(id, text)`(자유 텍스트)를 쓴다.
- **BE**
  - 이력: `GET /shelters/me/walks`, `GET /shelters/me/walks/{walk_id}`
  - 평가: `POST /walks/{walk_id}/volunteer-review`(`rating` 0.5~5, 0.5 단위), `tags`(복수), `comment`(최대 500자), 1회만 가능
  - 조회: `GET /walks/{walk_id}/volunteer-review`
- **필드 매핑**
  - 자유 텍스트 평가를 별점, 태그, 코멘트로 바꾼다.
  - 봉사자의 개인 메모(`private_memo`)는 보호소에 **노출되지 않는다**(#95). 현재 화면의 "메모" 표시는 제거하거나 다른 정보로 대체한다.
- **오류**: 중복 평가 409, 완료되지 않은 산책 409, 수정과 삭제 불가(405)
- **테스트**: 평가 작성, 중복, 진행 중 산책 평가 시도

#### 보호소 설정 (`#settings`, `pages/SettingsPage.tsx`의 `SettingsPage`)

- **현재**: `name`, `area`(지역 자유 입력), `phone`을 `setShelter()`로 저장한다. `reset()`은 데모를 초기화한다.
- **BE**
  - 지역: `PATCH /shelters/{shelter_id}/region`(`region_code`, 활성 시군구)
  - 시도와 시군구 목록: `GET /regions/sido`, `GET /regions/sigungu`
  - 보호소 정보 조회: `GET /shelters/{shelter_id}`
  - 이름과 연락처 수정 API는 없다(8절).
- **수정**
  - 지역은 자유 입력 대신 시도와 시군구 선택으로 바꾸고 `region_code`를 보낸다.
  - 데모 초기화 버튼은 서버 연동 후 제거한다.

## 5. #110과 Mock SMS 반영

- **발송 200**은 서버의 요청 **접수**이며 문자 발송 성공이 아니다. 문자가 오지 않으면 60초 뒤 다시 요청하게 안내한다(`resend_after_seconds`).
- **유지되는 제한**: 만료 300초(`expires_in_seconds`), 재발송 60초, 하루 5회(429 `PHONE_VERIFICATION_RATE_LIMITED`, `retry_after_seconds`), 실패 5회(429 `VERIFICATION_ATTEMPTS_EXCEEDED`), 최신 요청만 유효(이전 요청은 410)
- **제거된 응답**: 발송 409 `PHONE_ALREADY_REGISTERED`, 확인 409 `VERIFICATION_NOT_SENT`. 확인 실패는 상태와 무관하게 422 `VERIFICATION_CODE_INVALID`다.
- **저장은 별도 호출**: 확인 뒤 반드시 저장 API를 호출한다. 온보딩은 `PUT /users/me/onboarding/basic-info`, 프로필은 `PATCH /users/me/profile`(`phone_number`, `phone_verification_id`, `expected_profile_version`)이다. 다른 계정에 등록된 번호는 저장 단계에서 409 `PHONE_ALREADY_REGISTERED`다.
- **목적 분리**: 온보딩 API로 받은 인증은 프로필에 쓸 수 없고, 그 반대도 마찬가지다(404).
- **Mock SMS 테스트 방법**(#124, 정책은 [HANDOFF.md 8절](HANDOFF.md#8-sms-정책-변경mock-sms))
  - 시연과 테스트 서버는 실제 문자를 보내지 않는다. 고정 인증번호와 내부 합성 테스트 번호 `010-0000-XXXX`만 쓴다. 고정 인증번호는 담당자가 별도로 전달하며 앱 코드, 저장소, 로그에 넣지 않는다.
  - 순서: 회원가입, 로그인, 서약 → 발송 `POST /users/me/phone-verifications` `{"phone_number": "010-0000-XXXX"}` → 확인 `POST /users/me/phone-verifications/confirm`(`verification_id`, 사용자가 입력한 인증번호) → 저장 `PUT /users/me/onboarding/basic-info`. 프로필 번호 변경도 같은 방식이다.
  - 앱 안에서 고정값을 비교하지 않고 반드시 BE 확인 API를 호출한다(현재 `UserInput1Screen.tsx`의 자체 비교는 교체 대상).
  - 대역 밖 번호는 발송 422 `INVALID_PHONE_NUMBER`다. 번호는 HANDOFF.md 8절의 담당 대역(봉사자 앱 3000~3999, 보호소 웹 4000~4999) 안에서 사람마다 다르게 쓴다.
  - 발송은 비동기다. 발송 작업 전에 확인하면 422 `VERIFICATION_CODE_INVALID`이고 실패 횟수(5회)에 포함된다. 특정 시간이 지나면 발송이 끝났다고 가정하지 말고, 확인을 자동으로 반복하지 않는다. 사용자가 인증번호를 입력하고 확인을 누를 때만 호출한다.

## 6. 권장 연동 순서

API 의존성 기준:

1. 공통 클라이언트, 토큰, 오류 정규화(2절)
2. 로그인, 회원가입, `GET /users/me` 진입 분기(4.1)
3. 정책 문서와 동의, 서약(4.1)
4. 온보딩 Step 1~3, 위치(4.2)
5. 홈, 목록, 상세, 신청(4.3)
6. 내 예약, 취소(4.4)
7. 보호소 코드, 시작, 산책 중, 신고, 종료(4.5)
8. 인계, 기록, 메모, 레벨업, 이력(4.6)
9. 마이페이지, 내 정보 수정, 선호(4.7)
10. 보호소 웹(4.8절): 로그인, 대시보드, 강아지, 일정, 예약과 코드 발급, 산책 현황, 신고, 이력과 평가

| 구분 | 범위 |
| --- | --- |
| 서버 준비 전에도 할 수 있음 | 1~2의 클라이언트와 오류 처리, 화면별 요청과 응답 모델, Enum 매핑, 상태 관리와 화면 이동 구조, 인증번호 입력 UI, 로컬 BE(`APP_ENV=local`, `SMS_PROVIDER=mock`, 32자 이상 `PHONE_VERIFICATION_HMAC_KEY`, Mock 위치)로 하는 개발. 단, 인증번호 확인은 16-2 결정 전까지 로컬에서도 끝낼 수 없다([HANDOFF 8절](HANDOFF.md#8-sms-정책-변경mock-sms)) |
| 실서버와 초기 데이터 후 검증 | 목록, 신청, 승인, 코드 발급(보호소 계정), 산책 전 과정, 이력과 통계(16-6, 16-7, 16-10) |
| 정책 확정 후 검증 | 정책 문서와 동의(16-9), Mock SMS 인증번호 전달(16-2), 실제 위치 판별(16-8) |

## 7. Notion과 코드의 차이, 미확인 범위

- **확인된 차이**
  - Notion의 두 발송 명세는 발송 409 `PHONE_ALREADY_REGISTERED`를 적고 있다. 코드는 #110에서 이를 제거했다(코드 기준).
  - Notion GET과 PATCH 선호 명세 일부에 지역 필드가 남아 있다. 코드는 지역을 지원하지 않는다(#105).
  - Notion은 실제 SMS 공급자를 전제한다. 확정 정책은 Mock SMS다(2026-11-25).
- **미확인**
  - 이번 작성에서 Notion 전체 화면 문서를 다시 읽지는 않았다(각 기능 Issue 착수 시 대조했다).
  - 불확실한 기능(보호소 인증, 보호소 작성 신고, 토큰 갱신)은 Notion을 다시 확인했다(8절).
  - 보호소 웹은 코드로 확인했으나 브라우저에서 실행해 보지는 않았다.
  - FE `_legacy-web/`은 README에 "원본 파일을 보존하고 이전하지 않음"으로 적힌 사용하지 않는 구버전이라 조사에서 제외했다.
  - 실제 기기에서의 GPS와 백그라운드 동작은 검증하지 않았다.

## 8. FE와 BE 기능 차이 분류

현재 FE 화면에 있다는 이유만으로 신규 BE 기능을 추가하지 않는다. 기존 Issue, Notion, 실제 코드를 근거로 분류했다.

분류는 다음 다섯 가지다.
- **재사용**: 기존 BE 기능을 재사용할 수 있다.
- **필수 연동**: FE를 연동하면 되는 필수 기능이다.
- **추가 BE 개발**: 추가 BE 개발이 필요하다.
- **정책 결정**: 후속 정책 결정이 필요하다.
- **범위 제외**: 현재 개발 범위에서 제외한다.

| 기능 | 분류 | 근거 | 현재 처리 |
| --- | --- | --- | --- |
| 보호소 인증(`#verification`) | 정책 결정 | FE 화면이 "인증 방법, 서류, 승인 기준 미정"이라고 안내한다. Notion에는 ADMIN의 보호소 생성과 담당자 소속 지정만 있다. 관련 Issue가 없다. | ADMIN은 보호소 생성(`POST /shelters/`)과 기존 SHELTER 계정의 소속 연결(`PATCH /users/{id}/shelter`)만 할 수 있다(재사용). SHELTER 계정을 발급하는 API는 없다(역할 변경은 #13 미구현). 시연 환경은 16-7 시드 도구가 만든 계정을 쓴다. 인증 절차는 **미확정**이며, 정해질 때까지 연동하지 않는 것을 **권장**한다. |
| 보호소가 작성하는 신고 | 산책 연결 신고는 재사용 가능(**미확인**), 그 밖은 정책 결정 | 코드상 `POST /walks/{walk_id}/incidents`는 자기 보호소 산책이면 담당자와 관리자도 허용한다(`authorize_action`). 실행 검증은 하지 않았다. `incident_type`은 산책 안전 사건 Enum이고, 종료된 산책에는 늦은 신고 제한이 있다. 현재 FE 폼은 봉사자에 대한 신고 작성이라 목적이 다르다. | **권장**: 산책 안전 사건이면 산책을 선택해 접수하도록 FE 폼을 바꾼다. 봉사자 행동 신고 같은 산책 무관 신고는 정책이 정해질 때까지 연동하지 않는다(**미확정**). |
| 토큰 갱신 | 정책 결정 | Issue #13(열림)과 Notion "로그인 세션 갱신"(`/auth/refresh`, dev 미존재, 설계 필요). 세션은 1시간(`SESSION_SECONDS=3600`)이다. | 현재는 401 시 재로그인(필수 연동). 산책 중 만료로 GPS 업로드가 끊길 수 있어, 18단계 검증에서 영향을 확인하고 정책을 정한다. |
| 보호소 코드 발급 UI | 재사용(BE), 필수 연동(FE) | BE `POST /walk-requests/{id}/shelter-code`가 있다(#87). FE에 화면이 없다. | FE에 버튼과 표시를 추가한다. |
| 실시간 산책 현황 | 재사용, 필수 연동 | BE `GET /shelters/me/live-walks*`가 있다. FE는 안내 문구뿐이다. | FE에 목록을 추가한다. |
| 신청자 정보(나이, 경험) | 재사용 | `GET /walk-requests/{id}`의 `applicant` | 목록은 요약, 상세는 `applicant`를 쓴다. |
| 강아지 기본 정보(이름, 견종, 나이) 수정 | 정책 결정 | 일반 수정 API가 없다(`PUT /dogs/{id}/traits`, `PATCH walk-settings`만 있음). 공공데이터 강아지는 동기화로 덮어쓸 수 있다. | 수정 허용 범위(공공데이터 강아지, 보호소 등록 강아지)를 정한 뒤 필요하면 별도 Issue로 한다. |
| 일정 일시 중지와 재개 | 정책 결정 | BE는 `DELETE`로 영구 종료만 지원한다. | 재개 대신 새 일정 등록으로 안내할지 정한다. 그 전까지 FE는 "종료"만 제공한다. |
| 보호소 이름과 연락처 수정 | 정책 결정 | 수정 API가 없다(지역만 있음). 공공데이터 보호소는 동기화 대상이다. | 범위 결정 후 필요하면 별도 Issue로 한다. |
| 보호소 평가 형식 | 재사용, 필수 연동 | BE는 별점, 태그, 코멘트(#61, F-15) | FE 자유 텍스트를 바꾼다. |
| 봉사자 출발 체크리스트(4-4) 저장 | 범위 제외 | BE 저장 기능이 없고, 시작은 코드 검증으로 한다(#87). | FE 로컬 체크를 유지한다. |
| 선호 지역(10-3) | 범위 제외 | #105 확정: 지역은 FE 선택 UI가 생긴 뒤 별도 Issue로 한다. | 고정 문구를 유지한다. |
| 생년월일 정정(나이 확인 대기) | 재사용, 필수 연동 | BE `PUT /users/me/age-review/birth-date`, `DELETE /users/me`(#41). FE 화면이 없다. | FE 화면을 추가할지 FE가 정한다. 없으면 대기 안내만 한다. |
| Mock SMS 인증번호 전달 | 정책 결정 | 16-2(고정 OTP와 전달 방식 미확정) | 계약만 맞춘다(5절). |
