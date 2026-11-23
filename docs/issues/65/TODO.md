# Issue #65 남은 작업

갱신: 2026-11-24 (KST). 구현 내용은 [HANDOFF.md](HANDOFF.md)를 본다.

## PR 병합 전

- [ ] Draft PR 리뷰. Draft 해제와 병합은 별도 승인
- [ ] 병합 직전 최신 `dev` 반영 후 `python scripts/check_migrations.py`, `tests/test_signup_postgres.py`, 전체 `tests/` 재실행
- [ ] #66이 먼저 병합되면 남은 실패 목록에서 `test_walk_domain` 3건이 빠졌는지 확인

## 병합 후

- [ ] #59의 회귀 항목 해결 확인 후 #59 종료 검토
- [ ] #30, #31, #32 현황 절의 "F-14 회귀로 실패" 문구를 병합 결과로 갱신
- [ ] 병합 뒤 `dev`에서 `python scripts/check_migrations.py`와 `tests/test_signup_postgres.py` 재실행

## 온보딩 스택과 합칠 때

- [ ] PR #50, #52, #53, #57의 `tests/test_signup_postgres.py` 변경과 병합 충돌 해결
- [ ] 그 브랜치가 `users`에 추가하는 컬럼(`account_status`, `age_review_required_at` 등)과 마이그레이션을 `USER_COLUMNS_AFTER_018`에 추가

## 통합 마이그레이션 때

- [ ] 016~019를 적용한 뒤 새 코드 기동(019 없으면 가입과 로그인 500)
- [ ] 실제 개발 DB 적용은 모든 기능 개발과 PR 병합 뒤 별도 승인으로 진행
