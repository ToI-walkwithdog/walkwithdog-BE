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
- [x] 신고 접수 스키마 및 서비스 확장 (유형 7종, 멱등, 종료 산책, 위치대체, 3분 중복)
- [x] 보호소 알림/확인/이력 API 라우터 구현 (GET/POST /shelters/me/incident-alerts, GET /shelters/me/incidents)
- [x] 기존 테스트(description 공백) 수정 및 F-07 완료 기준 검증 테스트 작성

# F-08 실시간 산책 확인 TODO

- [x] DB 마이그레이션 (014_f08_live_walk.sql) 및 User 모델 phone 컬럼 추가
- [x] 응답 스키마 (app/walk_live_schemas.py) 정의 (카드, 상세, 증분, 마커, 경로)
- [x] 실시간 산책 서비스 (app/walk_live_service.py) 구현 (목록 조회, 상세, 증분, stale 판정, 마커 조립)
- [x] 보호소 실시간 산책 라우터 (app/routers/shelter_live_walks.py) 구현 (3개 엔드포인트)
- [x] app/main.py 라우터 등록 순서 (shelters.router 앞) 및 라우트 충돌 방지
- [x] .env.example에 LIVE_WALK_STALE_SECONDS=60 추가
- [x] 권한 제어 (보호소 격리, 봉사자 403, 타 보호소 403, 관리자 shelter_id 파라미터 지원)
- [x] 보안 규칙 준수 (산책자 전화번호는 IN_PROGRESS 상세에만 노출, 목록/증분 제외)
- [x] F-08 완료 기준 검증 테스트 작성 (tests/test_f08_live_walk.py 8건 통과)
- [x] F-05~F-07 및 도메인 회귀 테스트 통과 (총 101건 통과)

# F-09 산책 완료·평가 TODO

- [x] DB 마이그레이션 (015_f09_walk_review.sql) 및 WalkReview, WalkReviewTag 모델 추가
- [x] 응답/요청 스키마 (app/walk_review_schemas.py) 정의 (요약, 평가 생성/수정, 평가 응답)
- [x] 산책 요약 및 평가 서비스 (app/walk_review_service.py) 구현 (요약, 다운샘플링, 보호소 링크, 평가 upsert 및 조회)
- [x] 산책 요약 및 평가 라우터 (app/routers/walk_reviews.py) 구현 (GET summary, PUT review, GET review)
- [x] 산책 이력 API 확장 (app/walk_map_schemas.py, app/walk_map_service.py) rating, has_review 반영
- [x] app/main.py에 walk_reviews.router 등록
- [x] .env.example에 WALK_SUMMARY_MAX_POINTS=2000 추가
- [x] 권한 제어 (본인 봉사자만 평가/요약 조회 가능, 타인 403, 미종료 산책 409)
- [x] F-09 완료 기준 검증 테스트 작성 (tests/test_f09_walk_review.py 14건 통과)
- [x] test_walk_domain.py DDL/스키마 계약 검증 스냅샷 보정 및 100% 통과
- [x] F-05~F-08 전체 회귀 테스트 통과 (총 115건 통과)
