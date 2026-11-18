# F-03 산책 등록 (보호소) 구현 TODO

- [x] 산책 등록 API (새 강아지 등록 + 기존 강아지 선택 모두 지원)
- [x] 난이도 3단계(BEGINNER, INTERMEDIATE, EXPERIENCED) 확장 적용
- [x] 나이 년·개월 두 컬럼 처리 및 견종 입력 지원
- [x] 주 단위 슬롯 저장 구조 (dog_walk_date_slots 테이블) 추가
- [x] 입력 검증 (필수값, 30/60/90분 소요시간, 당일 지난 시각 거부, 최대 6개월, 시간대 겹침 방지)
- [x] 강아지 사진 업로드 처리 (5MB 제한, jpg/png 시그니처 검증, 로컬 UPLOAD_DIR 저장)
- [x] 사용자 산책 예약 목록 조회 시 등록된 슬롯 노출
- [x] DB 스키마 변경 (마이그레이션 SQL 작성)
- [x] 완료 기준 검증 테스트 작성
- [x] .env.example 갱신

# F-04 산책 예약 구현 TODO

- [x] 지역 텍스트 기반 산책 가능 강아지/슬롯 목록 조회 API
- [x] 산책 예약 신청 API (상태: PENDING)
- [x] 예약 신청 시 동시성 제어 (DB Row Lock 기반 중복 방지)
- [x] 동일 사용자의 동일 시간대 중복 예약 검증
- [x] DB 스키마 변경 (walk_requests 테이블 date_slot_id 추가 마이그레이션)
- [x] 동시성 제어 및 예약 완료 기준 검증 테스트 작성

# F-05 산책 승인 구현 TODO

- [x] 보호소 승인 대기 목록 (PENDING 상태) 조회 기능 검증
- [x] 신청 상세 조회 API (GET /walk-requests/{request_id}, 신청자 닉네임/반려견 경험/산책 빈도 및 강아지 난이도)
- [x] 보호소 승인/거절 처리 및 사용자 예약 내역(GET /walk-requests/my) 반영
- [x] 거절 사유 3종 코드(SCHEDULE_CONFLICT, EXPERIENCE_MISMATCH, INFO_INSUFFICIENT) 및 기타(OTHER, 직접 입력 필수) 검증
- [x] 거절 사유 텍스트만 전달 시 OTHER 자동 매핑(하위 호환)
- [x] 이미 처리된 신청 재처리 방지 (409)
- [x] 거절된 슬롯 재신청 가능 상태 복귀
- [x] 승인 후 사용자 취소 시 슬롯 복귀 및 보호소 목록에 "승인 후 취소"(cancelled_after_approval) 표시
- [x] 강아지 난이도 3단계(BEGINNER, INTERMEDIATE, EXPERIENCED) 모델/스키마/DB 제약 확장
- [x] DB 스키마 마이그레이션 SQL (migrations/011_f05_walk_approval.sql) 작성
- [x] 완료 기준 검증 테스트 작성 (tests/test_f05_walk_approval.py 11건 통과)

# F-06 산책 중 지도·경로 기록 TODO

- [x] DB 마이그레이션 (012_f06_walk_tracking.sql) 및 Walk 모델 컬럼/제약조건 추가
- [x] 정확도 50m 초과 409 거부 제거 및 last_* 갱신 (중복 좌표 묶음은 수신시각 갱신 제외)
- [x] 응답 스키마 WalkResponse 필드 추가 및 ActiveWalkResponse 정의
- [x] 수동/자동 종료 공통 로직 분리 및 만료 산책 자동 종료 (close_stale_walks)
- [x] 진행 중 산책 복원 API (GET /walks/active) 구현
- [x] 서버 백그라운드 주기 점검 루프 등록 및 .env.example 갱신
- [x] 기존 테스트(51m 허용) 수정 및 F-06 완료 기준 검증 테스트 작성


# F-07 위험 상황 신고 TODO

- [x] DB 마이그레이션 (013_f07_incident_report.sql) 및 WalkIncident 모델 확장
- [ ] 신고 접수 스키마 및 서비스 확장 (유형 7종, 멱등, 종료 산책, 위치대체, 3분 중복)
- [ ] 보호소 알림/확인/이력 API 라우터 구현 (GET/POST /shelters/me/incident-alerts, GET /shelters/me/incidents)
- [ ] 기존 테스트(description 공백) 수정 및 F-07 완료 기준 검증 테스트 작성
