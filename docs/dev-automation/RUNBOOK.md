# 승인된 Issue 한 건 전달 절차

## 현재 상태와 전환 조건

2026-11-24 조사 기준 dev와 main 모두 보호되지 않았고 활성 ruleset과 등록된 Actions workflow가 없었다. Merge commit은 허용됐고 GitHub auto-merge와 병합 후 브랜치 자동 삭제는 꺼져 있었다. 개인 CLI 계정의 관리 권한은 확인했으나 보호 규칙이나 승인용 변수를 바꾸지 않았다. 이 구축 PR은 사람이 검토하고 병합하는 PR이다.

이 구성은 GitHub Actions에서 읽기 전용 검증을 실행한다. 자동 Issue 작성과 최종 병합은 인증된 작업 세션에서 할 수 있다. 구현과 코드 리뷰는 별도 에이전트 컨텍스트에서 실행한다. 무인 운영에는 별도 런너, 승인 입력, 독립 리뷰 에이전트 실행 환경, 최소 권한 GitHub 자격 증명, Issue별 상태 보관이 필요하다. CI runner에는 쓰기 자격 증명을 넣지 않는다.

### 관리자 승인 후 한 번 적용할 설정

1. 사람이 구축 PR과 hosted CI의 실제 결과를 확인한다. 다음으로 dev에 GitHub Actions 앱의 `Dev quality gate`를 지정해 필수 상태 검사로 만들고 최신 Base 반영을 요구한다. 동일 이름의 다른 앱이나 일반 커밋 상태는 허용하지 않는다.
2. dev와 main 모두 classic 브랜치 보호에서 Require a pull request before merging을 켜고 Required approving reviews를 0으로 둔다. 마지막 Push 승인과 CODEOWNERS 승인 요구는 끈다. 관리자 포함 적용, 직접 Push, Force Push 및 브랜치 삭제 차단을 유지하고 예외 주체를 두지 않는다. dev에서는 Merge commit을 허용한다. PR 설정 객체가 없거나 보호를 확인할 수 없으면 CLI가 중단한다. 현재 CLI는 classic 보호 응답을 검증한다.
3. 관리자가 사용자 착수 승인자의 GitHub 로그인을 `TOI_ISSUE_APPROVERS`에 설정한다. 신규 Issue는 운영용 intake Issue를 지정해 `TOI_ISSUE_APPROVAL_INBOX`에 그 번호를 설정한다. 별도 GitHub 리뷰 계정과 `TOI_ISSUE_REVIEWERS`는 필요하지 않다.
4. 활성화 승인 이후에만 `TOI_ISSUE_DELIVERY_ENABLED=true`를 설정한다. 값이 없거나 false면 CLI의 신규 Issue 등록, PR 생성, 병합이 차단된다. 승인 전에 `merge` 미리보기와 차단 경로를 확인한다. GitHub auto-merge는 사용하지 않는다. 브랜치 보호는 개인 세션의 Issue 승인 근거까지 판정하지 않으므로, 운영 계정은 승인된 배포 CLI를 통해 병합한다.

병합 CLI는 보호된 dev에서 배포한 스크립트를 독립된 작업 디렉터리에서 실행한다. CI는 코드와 테스트를 실행하는 읽기 전용 GitHub Actions 작업이다. 설치 PR의 워크플로와 비교기, 초기 실패 기준은 후보에 있으므로 사람이 기존 dev 재현 증거와 diff를 검토해 승인한다. 설치 중에는 기존 dev 트리도 별도로 수집해 보존할 ID를 확인한다. 설치 후 비교기, 수집기, 기준 목록을 이벤트의 정확한 dev Base에서 읽는다. PR 워크플로 자체와 테스트는 후보 코드에서 실행되므로, 워크플로와 테스트 설정 변경은 일반 Issue의 조건부 병합에서 차단하고 별도 유지보수로 승인한다. 새 PR은 같은 저장소의 승인된 브랜치에서만 전달한다.

운영 CLI에는 전용 GitHub 인증이 필요하다. 조회에는 저장소 메타데이터, Contents, Issues, Pull requests, Actions, Variables와 dev/main classic 보호 규칙 조회 권한이 필요하다. 쓰기에는 기능 브랜치 Push 및 병합용 Contents와 PR 쓰기가 필요하다. 신규 Issue를 등록하는 경우에만 Issues 쓰기를 추가한다. 보호 규칙과 변수 편집은 별도 관리자가 맡는다. 리뷰 에이전트에는 GitHub 쓰기 권한을 부여할 필요가 없다. CI에는 운영 자격 증명을 넣지 않는다.

[GitHub 보호 브랜치 문서](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches), [GitHub Actions 이벤트 보안](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)

## 한 번에 한 Issue

[전체 개발 로드맵](FEATURE_PLAN.md)은 미리 관리한다. 한 번에 사용자가 승인한 기능 Issue 하나만 착수한다. 현재 PR이 dev에 병합되면 완료를 보고하고 다음 Issue의 초안을 제시한다. 병합 전에는 다음 Issue를 등록하거나 구현하지 않는다. 다음 Issue는 사용자가 착수를 승인한 뒤에만 등록하고 진행한다. 다른 세션에서 동시에 착수하지 않도록 사용자가 관리한다. 저장소 전체의 동시 실행을 기술적으로 차단하는 공통 잠금은 없으며, Issue별 상태 파일과 중복 생성 방지 기록은 유지한다.

## 승인과 작업 준비

1. Notion, 최신 FE, dev, 기존 Issue와 PR을 검색해 겹치는 기능과 선행 조건을 확인한다. 기존 Issue면 현재 제목과 본문을 제시하고, 신규면 템플릿을 따르는 제목과 전체 본문을 제시한다. 범위에 요구사항, 테스트 설계와 정확한 변경 경로를 포함한다. 접근 권한이나 정책 결정이 빠지면 착수를 보류한다.
2. 현재 제목과 본문을 `{"title":"...","body":"..."}` JSON 파일에 넣고 `python -m scripts.issue_delivery digest draft.json`의 SHA-256을 확인한다. 승인자가 기존 Issue 또는 신규 Issue용 intake에 아래 메타데이터가 담긴 댓글을 남기면 그것이 **한 번의 착수 승인**이다. 기존 Issue는 번호를 넣고, 신규 초안은 issue를 null로 둔다. 새 정책이나 승인 밖 경로가 필요해진 경우에만 변경 범위를 다시 승인받는다.

```text
<!-- toi-approval
{"issue":44,"issue_digest":"<전체 SHA-256>","paths":["app/routers/example.py","tests/test_example.py"]}
-->
```

현재 게이트는 GitHub의 인증된 댓글을 승인 증빙으로 사용한다. 채팅의 텍스트만으로 GitHub 승인자로 가장할 수 없으며, 채팅 1회 승인을 그대로 사용하려면 신뢰된 승인 기록 연동이 별도로 필요하다. 사용자가 GitHub 댓글에서 한 번 승인하는 경로에는 병합 시 추가 사용자 승인이 없다.

3. 신규 초안만 `python -m scripts.issue_delivery register draft.json --approval-comment COMMENT_ID --state STATE.json`으로 등록한다. 등록 전에 의미상 중복을 검색한다. 기존 Issue는 내용을 쓰지 않고 진행한다.
4. 프로젝트 전용 복제본에서 `python -m scripts.issue_delivery prepare ISSUE ../toi-issue-ISSUE --approval-comment COMMENT_ID --state STATE.json`을 실행한다. 승인이 바인딩된 Issue와 최신 원격 dev SHA가 일치하는 상태에서 `issue/ISSUE` worktree를 만든다. STATE.json은 전용 worktree 밖의 개인 작업 폴더에 보관한다. 다른 Issue는 별도 상태 파일을 쓴다.

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
