## 관련 Issue

Related to #

아래 메타데이터의 issue에는 기존 Issue 번호를, paths에는 실제 변경 경로를 모두 적는다. 운영 체계나 게이트 자체를 검토하는 수동 PR만 null을 사용한다.

<!-- toi-scope
{"issue":null,"paths":[]}
-->

## 변경과 완료 조건

- 구현 범위와 API 계약
- SQL 파일명, 체크섬, 선행 관계 및 격리 PostgreSQL 검증
- 실행 명령, 결과 및 기준 실패 비교
- 미확정 정책과 운영 제한
- 독립 검토에서 발견한 차단 사항과 처리 결과

## 독립 검토 기록

기능 PR에는 별도 리뷰 에이전트의 실제 작업 참조, 검토한 전체 Head SHA, 검토 범위와 결과를 기록한다. 변경 후에는 리뷰를 다시 받고 이 블록을 갱신한다. 수동 유지보수 PR은 아래 예시를 제거한다.

<!-- toi-review
{"issue":null,"head":"","context":"","summary":"","blockers":[]}
-->
