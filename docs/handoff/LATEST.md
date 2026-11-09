# 인수인계: F-04 산책 예약

- 날짜: 2026-11-09
- 브랜치: feature/f-04-walk-booking
- 작업 요약: 지역 기반으로 산책 가능한 강아지를 조회하고, PostgreSQL의 FOR UPDATE 락을 활용해 1인 1슬롯 동시성 제어를 보장하며 예약(대기 상태)하는 기능 구현 완료.

## 1. 변경 파일
- migrations/002_add_f04_walk_booking.sql
- app/models.py (또는 walk_models.py)
- app/services/walk_request_service.py
- app/routers/walk_requests.py
- docs/TODO.md, docs/handoff/LATEST.md

## 2. 결정 사항
- 지역 필터링 방식: FusedLocationProvider로 좌표 반환 후 클라이언트가 Geocoder로 텍스트 변환하여 백엔드에 쿼리스트링(`?region=`)으로 전달. `shelters.address ILIKE`로 필터링.
- 동시성 제어: Firestore 트랜잭션 대신 PostgreSQL의 비관적 락(`SELECT ... FOR UPDATE`) 적용하여 여러 요청이 와도 먼저 락을 획득한 1건만 예약 승인.
- 스키마 변경: `walk_requests`에 `date_slot_id`를 추가하여 F-03 슬롯과 연결.

## 3. 실행한 테스트 명령과 결과
- 실행 명령: `pytest tests/test_f04_walk_booking.py -v` (멀티스레딩 활용 동시성 테스트 포함)
- 결과: 작성된 N개 테스트 통과

## 4. 완료 기준 대조표
| 완료 기준 | 테스트 항목 | 결과 |
|---|---|---|
| 지역 기반 강아지 조회 | `test_get_available_dogs_by_region` | - |
| 슬롯 예약 PENDING 저장 | `test_create_walk_request_success` | - |
| 동시성 제어 (1건만 성공, 나머지 409 반환) | `test_concurrent_walk_requests_lock` | - |
| 동일 사용자 시간대 중복 검사 (400 반환) | `test_overlapping_user_request` | - |

## 5. 로컬 실행 준비
- 프로젝트 가상환경(`.venv`) 활성화 및 `requirements.txt` 확인.
- PostgreSQL 기동 및 신규 마이그레이션 SQL 실행.

## 6. 확인하지 못한 것
- 클라이언트(앱)와 실제 통합 시 Geocoder 텍스트 값이 `shelters` 테이블의 주소 형식과 얼마나 일치하는지에 대한 검증.