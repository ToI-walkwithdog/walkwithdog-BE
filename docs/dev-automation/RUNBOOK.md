# 승인된 Issue 한 건 전달 절차

## 현재 상태와 전환 조건

2026-11-24 조사 기준 dev와 main 모두 보호되지 않았고 활성 ruleset과 등록된 Actions workflow가 없었다. Merge commit은 허용됐고 GitHub auto-merge와 병합 후 브랜치 자동 삭제는 꺼져 있었다. 개인 CLI 계정의 관리 권한은 확인했으나 보호 규칙이나 승인용 변수를 바꾸지 않았다. 이 구축 PR은 사람이 검토하고 병합하는 PR이다.

이 구성은 GitHub Actions에서 읽기 전용 검증을 실행한다. 자동 Issue 작성과 최종 병합은 인증된 작업 세션에서 할 수 있다. 구현과 코드 리뷰는 별도 에이전트 컨텍스트에서 실행한다. 무인 운영에는 별도 런너, 승인 입력, 독립 리뷰 에이전트 실행 환경, 최소 권한 GitHub 자격 증명, Issue별 상태 보관이 필요하다. CI runner에는 쓰기 자격 증명을 넣지 않는다.

### 관리자 승인 후 한 번 적용할 설정

1. 사람이 구축 PR과 hosted CI의 실제 결과를 확인한다. 다음으로 dev에 GitHub Actions 앱의 `Dev quality gate`를 지정해 필수 상태 검사로 만들고 최신 Base 반영을 요구한다. 동일 이름의 다른 앱이나 일반 커밋 상태는 허용하지 않는다.
2. dev와 main 모두 classic 브랜치 보호에서 Require a pull request before merging을 켜고 Required approving reviews를 0으로 둔다. 마지막 Push 승인과 CODEOWNERS 승인 요구는 끈다. 관리자 포함 적용, 직접 Push, Force Push 및 브랜치 삭제 차단을 유지하고 예외 주체를 두지 않는다. dev에서는 Merge commit을 허용한다. PR 설정 객체가 없거나 보호를 확인할 수 없으면 CLI가 중단한다. 현재 CLI는 classic 보호 응답을 검증한다.
3. GitHub 댓글 승인 경로를 사용할 때만 승인자 GitHub 로그인을 `TOI_ISSUE_APPROVERS`에 설정한다. 신규 Issue의 GitHub 승인 댓글은 운영용 intake Issue를 지정해 `TOI_ISSUE_APPROVAL_INBOX`에 그 번호를 설정한다. 별도 GitHub 리뷰 계정과 `TOI_ISSUE_REVIEWERS`는 필요하지 않다.
4. 활성화 승인 이후에만 `TOI_ISSUE_DELIVERY_ENABLED=true`를 설정한다. 값이 없거나 false면 CLI의 신규 Issue 등록, PR 생성, 병합이 차단된다. 승인 전에 `merge` 미리보기와 차단 경로를 확인한다. GitHub auto-merge는 사용하지 않는다. 브랜치 보호는 개인 세션의 Issue 승인 근거까지 판정하지 않으므로, 운영 계정은 승인된 배포 CLI를 통해 병합한다.

병합 CLI는 보호된 dev에서 배포한 스크립트를 독립된 작업 디렉터리에서 실행한다. CI는 코드와 테스트를 실행하는 읽기 전용 GitHub Actions 작업이다. 설치 PR의 워크플로와 비교기, 초기 실패 기준은 후보에 있으므로 사람이 기존 dev 재현 증거와 diff를 검토해 승인한다. 설치 중에는 기존 dev 트리도 별도로 수집해 보존할 ID를 확인한다. 설치 후 비교기, 수집기, 기준 목록을 이벤트의 정확한 dev Base에서 읽는다. PR 워크플로 자체와 테스트는 후보 코드에서 실행되므로, 워크플로와 테스트 설정 변경은 일반 Issue의 조건부 병합에서 차단하고 별도 유지보수로 승인한다. 새 PR은 같은 저장소의 승인된 브랜치에서만 전달한다.

운영 CLI에는 전용 GitHub 인증이 필요하다. 조회에는 저장소 메타데이터, Contents, Issues, Pull requests, Actions, Variables와 dev/main classic 보호 규칙 조회 권한이 필요하다. 쓰기에는 기능 브랜치 Push 및 병합용 Contents와 PR 쓰기가 필요하다. 신규 Issue를 등록하는 경우에만 Issues 쓰기를 추가한다. 보호 규칙과 변수 편집은 별도 관리자가 맡는다. 리뷰 에이전트에는 GitHub 쓰기 권한을 부여할 필요가 없다. CI에는 운영 자격 증명을 넣지 않는다.

[GitHub 보호 브랜치 문서](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches), [GitHub Actions 이벤트 보안](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)

## 한 번에 한 Issue

[전체 개발 로드맵](FEATURE_PLAN.md)은 미리 관리한다. 한 번에 사용자가 승인한 기능 Issue 하나만 착수한다. 현재 PR이 dev에 병합되면 완료를 보고하고 다음 Issue의 초안을 제시한다. 병합 전에는 다음 Issue를 등록하거나 구현하지 않는다. 다음 Issue는 사용자가 착수를 승인한 뒤에만 등록하고 진행한다. 다른 세션에서 동시에 착수하지 않도록 사용자가 관리한다. 저장소 전체의 동시 실행을 기술적으로 차단하는 공통 잠금은 없으며, Issue별 상태 파일과 중복 생성 방지 기록은 유지한다.

## 이름 규칙

- 브랜치명, PR 제목, 커밋 메시지에는 Codex, Claude, ChatGPT 등 특정 AI 개발 도구 이름을 넣지 않는다. 작업 목적과 기능을 기준으로 쓴다.
- 기능 Issue 브랜치는 CLI가 요구하는 `issue/ISSUE`를 쓴다. 수동 유지보수 브랜치는 `chore/<목적>` 형식을 쓴다. 예를 들어 브랜치 `chore/issue-approval`, PR 제목 `[CHORE] Issue 착수 승인 방식 개선`, 커밋 메시지 `chore: Issue 착수 승인 방식 개선`처럼 쓴다.
- 도구별 세션 검증 로직, CLI 옵션(`--codex-approval`, `--claude-approval`), 환경 변수(`CODEX_THREAD_ID`, `CLAUDE_CODE_SESSION_ID`), 기술 문서에서는 정확한 구분을 위해 필요한 도구명을 쓴다.
- GitHub Actions 워크플로 이름과 실행 이름은 `CI`, 필수 Job 이름은 `Dev quality gate`로 유지한다. 이미 병합된 PR과 과거 브랜치 이름은 바꾸지 않는다.

## 승인과 작업 준비

1. Notion, 최신 FE, dev, 기존 Issue와 PR을 검색해 겹치는 기능과 선행 조건을 확인한다. 기존 Issue의 제목과 본문을 확인하고 요구사항, 검증 범위와 실제로 바꿀 정확한 경로를 제시한다. 접근 권한 또는 정책 결정이 빠지면 착수를 보류한다.
2. 현재 제목과 본문을 `{"title":"...","body":"..."}` JSON 파일에 넣고 `python -m scripts.issue_delivery digest ISSUE.json`으로 제목과 본문의 SHA-256을 계산한다. 사용자에게 제시하는 Codex 또는 Claude Code 최종 답변에 검토할 범위 설명과 아래 코드 블록을 한 번 포함한다. 같은 답변에 이 블록을 두 번 넣거나 도구 호출 중간의 설명에 넣지 않는다. 예시 값은 현재 Issue 데이터로 채운다.

````toi-approval-request
{"repository":"ToI-walkwithdog/walkwithdog-BE","issue":44,"title":"<현재 GitHub Issue 제목>","issue_digest":"<제목과 본문의 전체 SHA-256>","paths":["app/routers/example.py","tests/test_example.py"]}
````

3. 사용자가 같은 대화에서 아래 문장을 독립된 메시지로 직접 보내면 한 번의 착수 승인으로 처리한다. Issue 번호는 승인 대상에 맞춘다. 인용이나 예시를 포함한 다른 문장, 이미 진행 중인 일에 대한 요약, AI가 쓴 문장은 승인 증빙으로 사용하지 않는다. 중간에 질문이나 변경이 있었다면 최종 범위를 다시 제시한다.

```text
Issue #44의 확정된 요구사항과 변경 경로를 확인했어. 해당 Issue의 구현, 독립 리뷰, 테스트, PR 생성, 조건부 dev 병합까지 승인해.
````

4. 승인 직후 승인을 받은 세션에서 도구에 맞는 명령을 실행한다. 두 방식 모두 전용 worktree 밖의 개인 상태 파일에 원문, 세션과 시각, Issue 번호와 제목, 제목과 본문의 해시, 허용 경로를 보관한다. 재개, PR 생성 및 병합 때 같은 원본 기록과 현재 GitHub Issue를 다시 확인한다. 저장소, Issue 내용, 승인 범위가 달라지거나 로그를 찾을 수 없으면 멈춘다. 추가 경로가 필요하면 구현 전에 확장된 범위를 다시 승인받는다. 이후 변경 범위, 테스트, 독립 리뷰, CI와 병합 게이트는 승인 방식과 관계없이 같다.
   - Codex: `python -m scripts.issue_delivery prepare ISSUE ../toi-issue-ISSUE --codex-approval --state STATE.json`. CLI는 현재 `CODEX_THREAD_ID`의 표준 로컬 세션 로그(`~/.codex/sessions`)에서 실제 사용자 이벤트와 직전 제안을 읽는다.
   - Claude Code: `python -m scripts.issue_delivery prepare ISSUE ../toi-issue-ISSUE --claude-approval --state STATE.json`. CLI는 Claude Code가 설정한 `CLAUDE_CODE_SESSION_ID`로 `~/.claude/projects/<프로젝트 폴더>/<세션 ID>.jsonl` 세션 기록을 정확히 하나 찾는다. 아래 기준은 Claude Code 2.1.330의 VS Code 확장과 CLI 기록에서 확인했다.
     - 승인 근거는 그 기록의 마지막 사람 입력 레코드다. `type`이 `user`, `origin`이 `{"kind":"human"}`, `turnOrigin`이 `human`, `userType`이 `external`, `promptSource`가 `sdk`(VS Code) 또는 `typed`(CLI)이고 `isSidechain`이 false인 레코드만 사람 입력으로 본다. 도구 결과(`toolUseResult`), 압축 요약, 메타 레코드, 작업 알림, 하위 에이전트 기록은 제외한다.
     - 입력 내용은 승인 문장과 앞뒤 공백을 빼고 정확히 같아야 한다. VS Code 확장이 별도 블록으로 붙이는 `<ide_opened_file>`, `<ide_selection>` 블록만 제외한다. 붙여넣은 텍스트는 `<pasted_content>` 태그째 기록되므로 승인으로 인정되지 않는다. 승인 문장은 직접 입력해 단독 메시지로 보낸다. 이미지나 추가 문장이 섞여도 거부한다.
     - 이 입력의 `parentUuid`는 같은 세션에서 `stop_reason`이 `end_turn`인 최종 답변을 가리켜야 한다. 그 답변에 `toi-approval-request` 블록이 한 번 있어야 한다. 그 사이에 허용하는 레코드는 `turn_duration`, `away_summary` 시스템 레코드뿐이다.
     - 상태 파일의 `claude_approval`에는 원문, 세션 ID, 기록 파일 경로, 사람 입력 레코드의 UUID와 시각, Issue 번호와 제목, 해시, 허용 경로를 보관한다. 재개, PR 생성, 병합 때는 현재 `CLAUDE_CODE_SESSION_ID`가 기록된 세션과 같아야 하고 같은 레코드를 다시 읽어 저장값과 비교한다. 압축 뒤에도 세션 ID와 이전 기록은 유지된다. `/clear`나 새 대화로 세션이 바뀌면 새 세션에서 범위를 다시 제시하고 새 상태 파일로 승인받는다.
     - 기록 파일이 없거나(예: `cleanupPeriodDays` 보존 기간이 지나 삭제), 같은 세션 ID의 기록이 둘 이상이거나, 레코드 UUID가 중복되거나, Claude Code 기록 형식이 바뀌어 위 조건으로 사람 입력을 구분할 수 없으면 승인 검증이 실패한다. 이때는 GitHub 댓글 승인을 사용한다.

대화 승인은 로컬 Codex 또는 Claude Code 기록과 실행 환경을 신뢰한다. GitHub는 메시지 작성자의 신원을 독립적으로 검증할 수 없다. 같은 로컬 사용자 권한으로 세션 기록이나 상태 파일을 변경할 수 있으므로 서명된 증빙으로 사용하지 않는다. 운영 세션은 승인 로그를 직접 작성, 수정하거나 임의 텍스트로 대체하지 않는다. 다른 대화, 복사본, 계획 문서, 추론으로 만든 승인을 사용하지 않는다. 상태 파일과 대화 원문은 Git이나 PR에 올리지 않는다.

기존 GitHub 방식도 지원한다. 승인자는 기존 Issue 또는 신규 Issue용 intake에 아래 메타데이터가 담긴 댓글을 남긴다. 신규 등록은 전체 초안을 승인받고 issue를 null로 둔 뒤 `python -m scripts.issue_delivery register draft.json --approval-comment COMMENT_ID --state STATE.json`으로 진행한다. 등록 전 중복을 확인한다. 댓글 방식의 착수와 재개에는 `prepare ISSUE ../toi-issue-ISSUE --approval-comment COMMENT_ID --state STATE.json`을 사용한다.

```text
<!-- toi-approval
{"issue":44,"issue_digest":"<전체 SHA-256>","paths":["app/routers/example.py","tests/test_example.py"]}
-->
```

## 구현, 테스트, 독립 검토

- 승인한 경로 안에서 구현한다. API가 인증부터 예외 응답까지 실제 FE 계약을 유지하는지 비교한다. DDL이 필요하면 manifest 번호, 체크섬, 의존성, 재실행과 실패 복구를 격리 DB에서 확인한다.
- 최소한 직접 기능 테스트, 영향받는 회귀 테스트, `python scripts/check_migrations.py --check-reservations`를 실행한다. 전체 수집과 테스트는 Actions에서 강제한다. 로컬 전체 게이트를 실행할 때는 `DATABASE_URL=sqlite://`와, 서로 다른 빈 로컬 PostgreSQL의 `SIGNUP_TEST_DATABASE_URL`, `REGION_TEST_DATABASE_URL`을 지정한다. 호스트는 127.0.0.1, DB 이름은 `toi_walk_test_`로 시작하며 포트를 명시한다. 프로젝트의 로컬 격리 PostgreSQL 환경 외의 환경 변수는 넘기지 않는다.
- 전체 명령은 `python -m scripts.dev_test_report --out report.json`과 `python -m scripts.dev_gate tests report.json --base BASELINE.json`이다. BASELINE.json은 검증된 dev에서 꺼낸 기준 파일이다. 기존 테스트의 수집 ID와 실행 결과가 있어야 하며 실패 및 스킵은 정확한 식별자, 실행 단계, 하위 테스트 인자와 정규화한 오류 또는 사유가 같아야 한다. 새 스킵과 xfail은 차단한다. PostgreSQL 가입과 지역 그룹을 실행하며, 나머지 82건은 기존 스킵 사유를 검증한다.
- 새 테스트를 추가하면 `python -m scripts.dev_gate inventory report.json`으로 후보 수집 목록을 갱신한다. 게이트는 후보 목록과 실제 수집을 일치시켜 다음 PR에서도 새 테스트를 보호한다. 의도적 삭제와 이름 변경은 정확한 이전 ID와 변경 근거를 별도 승인받고 수동 유지보수 PR에 기록한다. `toi-scope`의 `issue`는 `null`로 두고 아래 블록에 사라지는 ID만 적는다. 실패 또는 스킵 기준을 제거하려면 기존 ID로 실제 통과해야 한다. 블록 등록은 승인 증빙이 아니며 유지보수 PR은 사람이 검토하고 병합한다.

```text
<!-- toi-test-removals
{"nodeids":["tests/test_example.py::test_obsolete"]}
-->
```
- #66 등 회귀를 해결하면 동일 테스트 또는 동일 하위 테스트가 실제 통과했음을 보여 주고 해당 기준 항목만 삭제한다. 대상 실패 테스트를 삭제, 이름 변경, skip 또는 xfail 전환하면 통과가 증명되지 않아 차단된다. 새 실패 등록이나 기준 확대가 필요하면 기능 PR에서 처리하지 않고 별도의 사람 검토를 거친다.
- 구현과 별도의 에이전트 컨텍스트에 Issue, Notion 및 FE와 BE 계약, Base, 전체 Head, diff와 테스트 결과를 전달한다. 리뷰 에이전트는 요구사항, 접근 권한, 민감 정보, 트랜잭션, 데이터 보존, 오류 응답, 클라이언트 호환성과 회귀 증거를 검토한다. 별도 리뷰 컨텍스트를 실행할 수 없거나 결과가 없으면 조건부 병합을 멈춘다.
- 차단 사항과 수정 근거를 PR 본문에 기록한다. 수정하면 관련 테스트와 독립 리뷰를 다시 실행하고 최종 Head의 결과를 아래 한 블록에 갱신한다. `context`에는 실제 리뷰 작업이나 세션을 식별할 수 있는 참조를, `summary`에는 검토한 범위와 관찰 결과를 넣는다. 차단 사항이 남아 있는 동안에는 `blockers`에 기록한다. 별도의 GitHub APPROVE는 필요하지 않다.

```text
<!-- toi-review
{"issue":44,"head":"<검토한 전체 Head SHA>","context":"<실제 리뷰 작업 참조>","summary":"<검토 범위와 결과>","blockers":[]}
-->
```

CLI는 동일 Issue와 Head, 리뷰 참조와 요약, 빈 차단 목록을 확인한다. GitHub는 PR 본문 작성자 또는 편집자를 기록한다. 별도 에이전트가 실제로 실행됐는지와 리뷰 참조의 진위는 PR 본문이나 GitHub API만으로 인증할 수 없다. 작업 세션이 리뷰 결과를 보관하고 본문에 옮길 책임을 진다. `PROTECTED` 경로를 수정하는 운영 도구 PR은 별도 유지보수로 사람이 검토하고 병합한다.

## PR과 병합 및 복구

- 기존 브랜치에는 일반 merge로 최신 dev를 반영하고 승인된 `issue/ISSUE` 브랜치를 Push한다. `python -m scripts.issue_delivery pr --title "PR 제목" --body PR.md --state STATE.json`은 GitHub에서 같은 브랜치의 기존 PR을 먼저 찾는다. 없으면 정확한 파일 범위와 승인 증빙을 확인한 뒤 Draft를 생성한다. 결과가 모호하면 동일 상태 파일과 생성 요청 기록을 기준으로 GitHub에서 복구한다. 확인 전 재생성은 차단한다. 같은 Issue에는 단일 상태 파일과 실행 잠금을 공유한다. 새 SQL은 정식 등록과 예약 상태를 PR에 설명한다.
- GitHub CI 결과와 별도 컨텍스트의 리뷰 기록을 확인한 뒤 Ready로 전환한다. `python -m scripts.issue_delivery merge PR --state STATE.json`은 쓰기 없는 미리보기다. 사용자 착수 승인과 경로, 브랜치 보호, 최신 dev 포함, CLEAN, 정확한 Head의 리뷰 기록과 동일 Base 및 Head에서 필수 CI 성공이 모두 필요하다. 승인된 운영 모드에서만 `--execute`를 붙인다. Merge commit과 전체 Head SHA를 사용하고, 병합 커밋 부모, 트리와 dev 반영을 다시 읽는다.
- 셸 오류나 종료가 있으면 새 Issue나 PR을 먼저 만들지 않는다. 상태 파일을 다시 읽고 GitHub의 Issue, Head 브랜치, PR과 CI 실행을 기준으로 복구한다. 등록 호출의 결과가 불명확하면 같은 제목과 승인 본문을 서버에서 찾아 바인딩한다. 서버에서도 확인이 불가능하면 중복 생성 대신 멈춘다. merge 재실행은 같은 PR의 병합 결과와 현재 dev 도달 가능성을 확인한다. 모호한 응답 또는 예상 밖 트리는 자동으로 되돌리거나 강제 Push하지 않는다.
- CI나 리뷰 불통과, 신규 회귀, 정책 미확정, 보안 또는 데이터 손실 위험, Notion과 FE의 중대한 충돌, 승인 밖 경로, 충돌이 있으면 중단한다. 기능 Issue의 착수 승인으로는 실제 유료 공급자 호출, 개인정보 삭제 정책 변경, 개발 DB와 운영 DB의 직접 변경, 공용 서버 배포를 수행할 수 없다. 진행 상황과 변경 근거는 PR과 Issue별 상태 파일에 남긴다. 완료 보고에 Issue, PR, 실제 병합 SHA, 구현 결과, 검증과 운영 대기 항목을 적고 다음 Issue 초안을 제시한다.
