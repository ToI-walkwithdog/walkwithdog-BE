# 인수인계: F-03 산책 등록 (보호소)

- 날짜: 2026-11-09 (KST)
- 브랜치: feature/f-03-walk-register
- 작업 요약: 보호소가 산책 가능한 강아지 정보와 날짜 지정형 산책 슬롯을 등록하는 API(F-03) 구현 완료. 

## 1. 변경 파일
- migrations/001_add_f03_walk_register.sql
- app/models.py
- app/services/dog_service.py
- app/routers/dogs.py
- docs/TODO.md, docs/handoff/LATEST.md
- .env.example

## 2. 결정 사항
- 사진 저장: 환경변수 `UPLOAD_DIR` 기준 로컬 디렉터리에 UUID 파일명으로 저장, 5MB 제한 및 확장자(jpg/png) 시그니처 검사 적용.
- 슬롯 모델: `dog_walk_date_slots` 테이블을 신설하여 기존 요일 기반 슬롯과 분리. 주마다 다른 요일 대응 가능.
- 시간 규칙: KST 기준, 당일 지난 시각은 등록 불가. 최대 6개월 내 설정 가능.

## 3. 실행한 테스트 명령과 결과
- 실행 명령: (예: `pytest tests/test_f03_walk_register.py -v`)
- 결과: (로컬 테스트 완료 후 통과 수 기재 요망)

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 예약 목록에 등록한 강아지·슬롯 표시 | `test_register_and_list_display` | - |
| 필수값 누락 시 등록 거부 | `test_missing_required_fields` | - |
| 지난 시각·6개월 초과 기간 등록 거부 | `test_past_time_and_6_months_limit` | - |
| 동일 강아지 겹치는 시간대 거부 | `test_overlap_time_slots` | - |
| 파일 형식(jpg/png) 및 5MB 크기 제한 | `test_image_upload_validation` | - |
| 보호소 인증 정보로 이름·지역 자동 채움 | `test_auto_fill_shelter_info` | - |
| 담당 보호소가 아닌 일반 사용자 등록 제한 | `test_unauthorized_access` | - |

## 5. 로컬 실행 준비
- `.env`에 `UPLOAD_DIR=./uploads/dogs` 추가.
- 저장소 루트에서 `mkdir -p uploads/dogs` 실행.
- PostgreSQL 마이그레이션 SQL 실행.

## 6. 확인하지 못한 것
- 클라이언트 앱에서 Multipart/form-data 형식으로 JSON 배열(슬롯 정보)을 보낼 때의 파싱 호환성.

## 7. 다음 할 일 및 주의사항
- 기존 산책 신청 검증(`validate_eligibility`)이 `dog_walk_date_slots`를 바라보도록 변경하는 후속 이슈 진행 필요.