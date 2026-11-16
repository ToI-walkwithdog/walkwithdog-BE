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

# F-06 산책 중 지도·경로 기록 TODO

- [x] DB 마이그레이션 (012_f06_walk_tracking.sql) 및 Walk 모델 컬럼/제약조건 추가
- [x] 정확도 50m 초과 409 거부 제거 및 last_* 갱신 (중복 좌표 묶음은 수신시각 갱신 제외)
- [x] 응답 스키마 WalkResponse 필드 추가 및 ActiveWalkResponse 정의
- [x] 수동/자동 종료 공통 로직 분리 및 만료 산책 자동 종료 (close_stale_walks)
- [x] 진행 중 산책 복원 API (GET /walks/active) 구현
- [x] 서버 백그라운드 주기 점검 루프 등록 및 .env.example 갱신
- [ ] 기존 테스트(51m 허용) 수정 및 F-06 완료 기준 검증 테스트 작성
