# 인수인계: F-05 산책 승인 (보호소, SC-14)

- 날짜: 2026-11-10 (KST)
- 브랜치: feature/f-05-walk-approval
- 작업 요약: 보호소가 신청자의 정보(닉네임, 반려견 경험, 산책 빈도)와 강아지 난이도를 확인하고 산책을 승인 또는 거절하는 API(F-05, SC-14) 구현 완료. 승인/거절 결과는 사용자 마이페이지 예약 내역(F-13)에 반영됨.

## 1. 변경 파일
- `migrations/011_f05_walk_approval.sql`: 강아지 난이도 3단계 확장 제약 및 `walk_requests.rejection_reason_code` 컬럼 추가
- `app/models.py`: `DogWalkDateSlot`에 필요한 `Date, Time` 누락 import 수정
- `app/walk_models.py`: 난이도 3단계 제약(`ck_difficulty`) 적용, `WalkRequest`에 `rejection_reason_code` 컬럼 추가
- `app/walk_schemas.py`: `difficulty` Literal에 `INTERMEDIATE` 추가
- `app/walk_request_schemas.py`: `RejectionReasonCode` enum 정의, `WalkRequestAction` 거절 사유 검증 확장, `WalkRequestDetailResponse` 신설, `cancelled_after_approval` 계산 필드 추가
- `app/walk_request_service.py`: `transition_request` 거절 사유 코드 매핑 및 하위 호환 처리, `get_request_detail` 신청 상세 조회 구현
- `app/routers/walk_requests.py`: `GET /walk-requests/{request_id}` 엔드포인트 신설
- `app/routers/dogs.py`: F-03의 `File, Form, UploadFile, save_dog_image` 누락 import 보완
- `tests/test_f05_walk_approval.py`: F-05 완료 기준 및 예외 케이스 검증 테스트 (11개 항목)
- `docs/TODO.md`, `docs/handoff/LATEST.md`: 작업 목록 및 인수인계 문서 갱신

## 2. 결정 사항
- **신청자 정보 조회**: `users.nickname`과 함께 최신 `surveys` 레코드의 `dog_experience`, `walk_frequency`를 반환. (신청자 이름, 나이, 경험 견종은 F-01 구현 후 연동 예정으로 이번 이슈에서는 null 응답)
- **강아지 난이도**: 3단계(`BEGINNER`, `INTERMEDIATE`, `EXPERIENCED`)로 모델, 스키마, DB 제약조건을 확장.
- **거절 사유 코드**: 
  - `SCHEDULE_CONFLICT` ("보호소 일정상 어려움")
  - `EXPERIENCE_MISMATCH` ("경험 부족")
  - `INFO_INSUFFICIENT` ("정보 확인 어려움")
  - `OTHER` ("기타", 직접 입력 필수)
  - 하위 호환: 사유 코드 없이 사유 텍스트만 전송된 요청은 `OTHER`로 자동 지정.
- **승인 후 취소 식별**: 별도 DB 컬럼 없이 `status == "CANCELLED"`이고 검토 이력(`reviewed_by IS NOT NULL`)이 존재하는 경우 `cancelled_after_approval=True`로 반환.
- **슬롯 복귀**: 거절(`REJECTED`) 또는 취소(`CANCELLED`)된 신청은 `PENDING`/`APPROVED` 활성 신청에서 제외되므로 즉시 다른 사용자(또는 동일 사용자)가 재신청 가능.

## 3. 실행한 테스트 명령과 결과
- 실행 명령:
  `$env:PYTHONPATH="."; pytest tests/test_f05_walk_approval.py tests/test_walk_requests.py -v`
- 결과:
  **43 passed in 7.20s**
  - `tests/test_f05_walk_approval.py`: 11 passed
  - `tests/test_walk_requests.py`: 32 passed

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 보호소 승인 대기 목록(PENDING) 조회 | `test_shelter_pending_requests_list` | 통과 |
| 신청 상세에서 신청자 정보 및 강아지 난이도 확인 | `test_walk_request_detail_with_applicant_and_dog_difficulty` | 통과 |
| 승인 결과 사용자 마이페이지 예약 내역 반영 | `test_approve_request_and_user_mypage_reflection` | 통과 |
| 거절 사유 코드/기본 문구 처리 및 마이페이지 반영 | `test_reject_request_with_predefined_codes_and_mypage` | 통과 |
| 거절 사유 기타(OTHER) 선택 시 상세 사유 직접 입력 필수 | `test_reject_with_other_requires_detail` | 통과 |
| 사유 코드 없이 텍스트만 전달 시 OTHER 자동 매핑(하위 호환) | `test_reject_legacy_reason_only_defaults_to_other` | 통과 |
| 이미 처리된 신청 재처리 불가 (409) | `test_already_processed_request_cannot_be_reprocessed` | 통과 |
| 거절된 슬롯 재신청 가능 상태 복귀 | `test_rejected_slot_can_be_booked_again` | 통과 |
| 승인 후 사용자 취소 시 슬롯 복귀 및 승인후취소 표시 | `test_cancel_after_approval_reopens_slot_and_shows_cancelled_after_approval` | 통과 |
| 승인 전 대기 상태에서 취소 시 승인후취소 미표시 | `test_cancel_before_approval_shows_false` | 통과 |
| 난이도 3단계(INTERMEDIATE 포함) 정상 동작 | `test_dog_walk_settings_intermediate_difficulty` | 통과 |

## 5. 로컬 실행 준비
- PostgreSQL 데이터베이스에 신규 마이그레이션 적용:
  `migrations/011_f05_walk_approval.sql`

## 6. 확인하지 못한 것
- Windows 로컬 환경에서 비-bypass 샌드박스 실행 시 Python asyncio 내부의 `_fallback_socketpair`가 로컬 소켓 권한으로 차단되는 현상 (BypassSandbox 모드에서는 정상 통과 확인).
- F-01(회원가입/프로필 관리)이 아직 dev에 머지되지 않아, 실제 사용자 이름/나이/경험견종 데이터베이스 컬럼 및 클라이언트 UI 연동 상태는 미확인 (현재는 null 및 설문 데이터로 대응).

## 7. 다음 할 일 및 주의사항
- F-01 구현 완료 후 `ApplicantDetailResponse`에 실제 이름, 나이, 경험 견종을 매핑하도록 연동 필요.
- F-04(산책 예약)가 dev에 머지된 후 날짜 슬롯(`date_slot_id`) 기준의 슬롯 해제/복귀 로직과의 통합 테스트 필요.
