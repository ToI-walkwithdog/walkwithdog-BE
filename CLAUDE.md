# 이 저장소에서 Claude Code로 작업하기

기능 착수 전에 [Issue 전달 절차](docs/dev-automation/RUNBOOK.md)와 [기능 개발 계획](docs/dev-automation/FEATURE_PLAN.md)을 읽는다. 현재 세션이나 GitHub 연동에서 확인 가능한 팀 AGENTS.md와 조직 지침도 적용한다. 테스트나 보호 설정을 건너뛰기 위해 이 파일을 변경하지 않는다.

- [전체 개발 로드맵](docs/dev-automation/FEATURE_PLAN.md)은 미리 관리한다. 착수 전 Issue 제목, 제목과 본문의 해시, 변경 경로와 검증 범위를 `toi-approval-request` 블록과 함께 제시하고 사용자가 대화에서 직접 승인할 때까지 기다린다.
- 한 번에 기능 Issue 한 건을 진행한다. 현재 PR이 dev에 병합되면 다음 Issue의 초안을 제시한다. 그 전에는 다음 Issue를 등록하거나 구현하지 않으며 초안은 사용자가 착수를 승인한 뒤에만 실행한다. 다른 세션의 동시 착수는 사용자가 관리한다. 저장소 전체의 동시 실행을 기술적으로 차단하는 잠금은 없다.
- 사용자가 이 Claude Code 세션에서 직접 보낸 승인 메시지와 직전 범위 제안을 `prepare ... --claude-approval`로 검증하거나, Codex 세션 승인(`--codex-approval`) 또는 기존 GitHub 승인 댓글(`--approval-comment`)을 검증한 뒤 전용 worktree를 만든다. 승인 문장은 직접 입력해 단독 메시지로 받아야 하며 붙여넣기, 인용, 다른 세션의 승인은 쓰지 않는다. 재개, PR 생성, 병합도 승인을 받은 같은 세션에서 실행한다. 대화, Issue, 로드맵, 예시 또는 AI 문장에서 승인을 추론하거나 사용자 승인 이벤트를 대신 작성하지 않는다. 승인 원문과 범위를 Issue별 상태 파일에 기록하고 로그나 범위를 다시 검증할 수 없으면 멈춘다. 개발 DB와 운영 DB를 직접 변경하거나 공용 서버에 배포하지 않는다.
- 구현과 별도의 리뷰 에이전트 컨텍스트에 Issue, Base, Head, diff와 테스트 결과를 전달한다. 차단 사항을 수정한 후 같은 Head에서 다시 검토하고 PR 본문에 검토 근거와 차단 사항을 기록한다.
- 병합 게이트는 Issue 착수 승인, 정확한 변경 경로, PR 본문의 독립 리뷰 기록, 필수 CI, 최신 dev와 전체 Head SHA를 검증한다. GitHub 승인 표결은 0건으로 설정한다. Merge commit은 활성화된 실행 모드에서만 생성한다.
- 안전하지 않은 상태나 확인할 수 없는 근거가 있으면 중단하고 보고한다. 완료 후 다음 Issue는 자동 시작하지 않는다.
- 브랜치명, PR 제목, 커밋 메시지에는 Codex, Claude, ChatGPT 등 특정 AI 개발 도구 이름을 넣지 않고 작업 목적과 기능을 기준으로 쓴다. 세부 규칙은 [Issue 전달 절차의 이름 규칙](docs/dev-automation/RUNBOOK.md#이름-규칙)을 따른다.
