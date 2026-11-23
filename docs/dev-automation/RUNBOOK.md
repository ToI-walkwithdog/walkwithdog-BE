# 승인된 Issue 한 건 전달 절차

## 현재 상태와 전환 조건

2026-11-24 조사 기준 dev와 main 모두 보호되지 않았고 활성 ruleset과 등록된 Actions workflow가 없었다. Merge commit은 허용됐고 GitHub auto-merge와 병합 후 브랜치 자동 삭제는 꺼져 있었다. 개인 CLI 계정의 관리 권한은 확인했으나 보호 규칙이나 승인용 변수를 바꾸지 않았다. 이 구축 PR은 사람이 검토하고 병합하는 PR이다.

이 구성은 GitHub Actions에서 읽기 전용 검증을 실행한다. 자동 Issue 작성과 최종 병합은 인증된 작업 세션에서만 할 수 있다. 프롬프트가 세션을 상시 유지하거나 리뷰 에이전트를 자동으로 실행시키는 기능은 없다. 무인 운영이 필요하면 별도 승인 후 신뢰된 작업 런너, 승인 입력, 독립 검토 주체, 최소 권한 GitHub 자격 증명, Issue별 단일 실행 잠금과 작업 상태 보관을 준비한다. CI runner에는 쓰기 자격 증명을 넣지 않는다.

### 관리자 승인 후 한 번 적용할 설정

1. 사람이 구축 PR과 hosted CI의 실제 결과를 확인한다. 다음으로 dev에 GitHub Actions 앱의 `Dev quality gate`를 지정해 필수 상태 검사로 만들고 최신 Base 반영을 요구한다. 동일 이름의 다른 앱이나 일반 커밋 상태는 허용하지 않는다.
2. dev에는 PR, 독립 승인 최소 1건, 새 커밋 시 기존 승인 취소, 마지막 Push를 하지 않은 주체의 승인, 관리자 포함 규칙 강제, Force Push와 브랜치 삭제 금지를 설정한다. 직접 Push 또는 예외 주체를 허용하지 않는다. main에는 PR 사용과 직접 Push, Force Push, 삭제 차단을 따로 설정한다. 보호를 확인할 수 없으면 CLI가 중단한다. 현재 CLI는 REST의 classic dev 보호 응답을 검증하며 이와 동등한 ruleset만 사용하는 경우 호환 검증을 먼저 추가해야 한다.
3. 관리자가 승인자와 검토자 GitHub 로그인을 각각 `TOI_ISSUE_APPROVERS`, `TOI_ISSUE_REVIEWERS` 저장소 변수로 설정한다. 검토자는 구현 PR 작성자 및 병합 실행 계정과 달라야 하고 저장소 쓰기 이상 권한이 있어야 한다. 신규 Issue에 동일한 시작 승인을 쓰려면 팀 운영용 intake Issue를 지정하고 그 번호를 `TOI_ISSUE_APPROVAL_INBOX`에 설정한다.
4. 먼저 `merge` 미리보기와 실패 경로를 실제 권한으로 검증한다. 승인 이후에만 `TOI_ISSUE_DELIVERY_ENABLED=true`를 설정한다. 값이 없거나 false면 CLI의 신규 Issue 작성 및 병합이 차단된다. GitHub auto-merge 기능은 이 절차에 사용하지 않는다. 인증된 운영자는 게이트를 통하지 않는 수동 병합 명령을 사용하지 않는다. 브랜치 보호 자체가 개인 세션의 승인 근거를 판정하지는 않는다.

[GitHub 보호 브랜치 문서](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)

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
- 전체 명령은 `python -m scripts.dev_test_report --out report.json` 및 `python -m scripts.dev_gate tests report.json --base BASELINE.json`이다. BASELINE.json은 검토된 dev에서 별도로 꺼낸 판본이다. 셸이 pytest 종료 1을 무시하는 방식으로 통과시키지 않는다. 수집 및 실행 오류는 통과할 수 없고, 기존 실패/skip 각각의 식별자, 단계, 하위 테스트와 오류가 맞아야 한다. 현재는 가입과 지역 PostgreSQL 그룹을 실행한다. 다른 DB 그룹의 82개 skip은 기존 테스트 ID와 사유를 고정해 재현하며 미실행 상태로 보고한다.
- #66 등 회귀를 해결하면 동일 테스트 또는 동일 하위 테스트가 실제 통과했음을 보여 주고 해당 기준 항목만 삭제한다. 대상 실패 테스트를 삭제, 이름 변경, skip 또는 xfail 전환하면 통과가 증명되지 않아 차단된다. 새 실패 등록이나 기준 확대가 필요하면 기능 PR에서 처리하지 않고 별도의 사람 검토를 거친다.
- 검토 시 다른 컨텍스트에 Issue, Notion 및 FE 계약, Base, Head, diff와 테스트 결과를 전달한다. 요구사항 누락, 인증 및 인가 우회, 민감 정보, 트랜잭션과 동시성, 제약과 데이터 보존, 예외 응답, 클라이언트 호환성, 회귀 증거를 검토한다. 구현자가 리뷰 결과를 대신 승인하지 않는다. 리뷰 에이전트가 도구로 제공되지 않으면 검토 가능한 독립 담당자가 필요하다.
- 차단 항목 수정 후 영향 테스트와 CI를 다시 실행한다. 리뷰 승인 본문에 아래 블록을 포함한다. PROTECTED 경로를 수정하는 운영 도구 PR은 별도 사람 병합 대상으로 둔다.

```text
<!-- toi-review
{"issue":44,"head":"<검증한 전체 Head SHA>","blockers":[]}
-->
```

## PR과 병합 및 복구

- 기존 브랜치에는 일반 merge로 최신 dev를 반영한다. 이력을 재작성하지 않는다. `gh pr list --repo ToI-walkwithdog/walkwithdog-BE --head issue/ISSUE --state all`로 기존 PR을 우선 복구한다. 없으면 `gh pr create --repo ToI-walkwithdog/walkwithdog-BE --base dev --head issue/ISSUE --draft --body-file PR.md`를 사용한다. CI가 바뀐 경로를 PR의 `toi-scope`와 비교한다. 실제 전체 경로는 승인 목록의 부분집합이어야 한다. 새 SQL은 정식 등록과 예약 상태를 PR에 설명한다.
- GitHub CI 결과와 독립 검토를 확인한 뒤 Ready로 전환한다. `python -m scripts.issue_delivery merge PR --state STATE.json`은 쓰기 없는 미리보기다. 브랜치 보호, 최신 dev 포함, CLEAN, 독립 리뷰, 정확한 Base와 Head용 CI 성공이 모두 확인돼야 한다. 승인된 운영 모드에서만 같은 명령에 `--execute`를 붙인다. Merge commit과 전체 Head SHA를 사용하고, 병합 커밋 부모, 트리와 dev 반영을 다시 읽는다.
- 셸 오류나 종료가 있으면 새 Issue나 PR을 먼저 만들지 않는다. 상태 파일을 다시 읽고 GitHub의 Issue, Head 브랜치, PR과 CI 실행을 기준으로 복구한다. 등록 호출의 결과가 불명확하면 같은 제목과 승인 본문을 서버에서 찾아 바인딩한다. 서버에서도 확인이 불가능하면 중복 생성 대신 멈춘다. merge 재실행은 같은 PR의 병합 결과와 현재 dev 도달 가능성을 확인한다. 모호한 응답 또는 예상 밖 트리는 자동으로 되돌리거나 강제 Push하지 않는다.
- CI나 리뷰 불통과, 새로운 회귀, 정책 미확정, 보안 또는 데이터 손실 위험, Notion과 FE의 중대한 충돌, 승인 밖 경로, 충돌이 있으면 중단한다. 실제 유료 공급자 호출, 개인정보 삭제 정책 변경, 공유 DB에 SQL 적용, 공용 서버 배포는 별도 승인 범위다. 진행 상황과 변경 근거는 해당 PR과 전용 상태 파일에 남긴다. 완료 보고에는 Issue, PR, 실제 병합 SHA, 구현 결과, 검증, 운영 대기 항목과 다음 후보를 적고 다음 Issue는 착수하지 않는다.
