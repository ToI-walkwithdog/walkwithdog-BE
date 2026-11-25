# ECR 이미지 발행 운영 가이드 (#136)

## 1. 현재 상태와 작업 범위

Workflow는 보호된 `main`에서 수동 실행되며 Validate와 Publish Job을 분리한다. Validate는 공식 Release CI 실행·관리자 승인·독립 리뷰·Artifact 출처와 이미지 무결성을 확인한다. 검증 Bundle이 전달되고 재검증된 경우에만 Publish Job이 OIDC로 발행 전용 Role을 얻어 ECR에 이미지를 복사한다. 승인·설정·증빙 중 하나라도 누락되거나 불일치하면 발행은 차단된다.

운영 활성화에는 `TRUSTED_RELEASE_WORKFLOW_SHA`, 기존 AWS Repository Variables 및 `ECR_PUBLISH_ROLE_ARN` 설정과 IAM Role 구성이 필요하다. 이 저장소 변경은 AWS 인프라를 생성하거나 변경하지 않는다.

- 기존 Dev CI, AWS Connection Test, Release CI(#125), EC2, DB 및 SSM 문서는 변경하지 않는다.
- 신규 GitHub Environment, ECR 저장소, OIDC Provider, 승인 관리 시스템 및 별도 증빙 저장소는 만들지 않는다.
- 기존 ECR 저장소와 OIDC Provider를 재사용한다. 발행 Job에만 OIDC 권한을 부여한다.
- 이미지 재빌드, 애플리케이션 실행, DB 접근, EC2/SSM 배포는 수행하지 않는다.

## 2. 인프라 기준

| 항목 | 값 |
| --- | --- |
| AWS 계정 | `478478097368` |
| 리전 | `ap-northeast-2` |
| ECR | `toi-be` |
| URI | `478478097368.dkr.ecr.ap-northeast-2.amazonaws.com/toi-be` |
| 플랫폼 | `linux/amd64` |
| 태그 | 병합 후 main의 전체 Commit SHA 40자리 |
| 태그 불변성 | `IMMUTABLE`, 제외 필터 없음 |
| 암호화 | `AES256` |
| 스캔 | Registry BASIC / `SCAN_ON_PUSH` |
| Lifecycle | untagged 이미지 Push 후 7일 경과 시 만료 |
| 기존 GitHub Role | `toi-github-actions-role` (유지) |
| 발행 Role | `toi-github-ecr-publish` (미생성, 예정) |

기존 ECR 설정 변경은 운영자 수행 기준이며 적용 상태 및 최초 이미지 자동 스캔은 후속 검증 대상이다. Registry 스캔 정책의 유효 `scanFrequency`를 확인한다. ECR 스캔은 Release CI의 비밀값 및 레이어 검사를 대체하지 않는다.

## 3. Release CI 전달 계약

#125의 공식 `workflow_dispatch` 실행에서 전체 회귀, 마이그레이션, 별도 PostgreSQL 82개, FastAPI 스모크 및 이미지 보안 검사를 통과한 동일 이미지를 전달한다. 스모크 이미지와 OCI Config/Manifest 및 모든 레이어의 동일성은 생산자 측에서도 입증해야 한다. 후보 코드가 승인·검증 증빙이나 업로드를 통제하지 않도록 한다.

Artifact 이름: `release-image-{run_id}-{run_attempt}`. ZIP의 루트에는 정확히 `image.oci.tar`와 `release-evidence.json`을 둔다. 두 파일은 같은 Artifact이므로 보존 기간은 함께 7일이다. 별도 발행 성공 증빙은 90일 보존을 목표로 하며 저장소 허용 범위를 확인한다.

`release-evidence.json`의 v1 필수 정보:

- `schema_version`, `repository`
- `release`: `pr_number`, `base_sha`, `head_sha`, `merge_tree_sha`
- `workflow`: `run_id`, `run_attempt`, `workflow_sha`
- `verification`: `regression`, `migration`, `postgresql`, `postgresql_passed`, `container_smoke`, `image_security`
- `image`: `platform`, `archive`, `archive_sha256`, `config_digest`, `manifest_digest`

위 값 자체는 승인 근거가 아니다. 공식 성공 실행은 trusted `release_ci.py`가 dispatcher와 승인 작성자의 관리자 권한을 검사했다는 근거다. 이 Workflow는 성공한 verifier job의 로그에서 승인 댓글 ID와 작성자 login을 읽고, 그 ID의 현재 댓글이 수정되지 않았으며 승인 레코드 본문이 일치하는지 확인한다. 댓글 본문 SHA-256도 검증 Bundle에 고정하고 Publish가 AWS 인증 전에 다시 조회·대조한다. 실행 이벤트/경로/SHA/시도, Artifact ID/출처/ZIP Digest, PR 병합, 독립 리뷰 및 exact Head도 GitHub API와 대조한다. `TRUSTED_RELEASE_WORKFLOW_SHA`는 검토된 Release CI의 40자리 Workflow SHA만 허용하며 변수 자체는 독립 승인 증명이 아니다. 최신 시도와 현재 protected `main` HEAD를 확인한다. 일반 2-parent Merge Commit만 지원하며 Squash/Rebase는 차단한다. 압축 ZIP/OCI Archive 4GiB 및 전체 레이어 압축 해제 합계 16GiB는 안전 한도다. 이미지나 증빙 만료·누락 시 발행을 보류한다.

검증된 OCI 이미지와 Release 증빙은 `ecr-verified-image-{publish_run_id}-{publish_run_attempt}` 임시 Artifact로 Validate에서 Publish로 전달한다. Publish는 AWS 인증 전에 다시 압축 아카이브, OCI Manifest, Config, 모든 Layer와 증빙의 관계를 검사한다. 실제 Release Artifact의 이름은 `release-image-{run_id}-{run_attempt}`이며 루트에 `image.oci.tar`와 `release-evidence.json`만 허용한다.

### 운영용 `image-publish-evidence.json` 계약 v1

운영 증빙은 SSM 배포 검증기(`backend_deploy.py`)와 같은 필드 집합·중첩 구조를 사용한다. 추가 필드는 허용하지 않는다.

```json
{
  "schema_version": 1,
  "record_type": "production",
  "simulation": false,
  "publication_authorized": true,
  "deployable": true,
  "repository": "ToI-walkwithdog/walkwithdog-BE",
  "source": {"sha": "<40-hex>", "tree": "<40-hex>"},
  "release": {
    "pr_number": 1, "base_sha": "<40-hex>", "head_sha": "<40-hex>",
    "merge_tree_sha": "<40-hex>", "change_digest": "<64-hex>"
  },
  "release_workflow": {
    "run_id": 1, "run_attempt": 1, "workflow_sha": "<40-hex>",
    "dispatcher": "<GitHub login>", "official_job_id": 1
  },
  "approval": {
    "comment_id": 1, "approver": "<GitHub login>", "record_sha256": "<64-hex>",
    "review": {"head": "<40-hex>", "record": "https://github.com/...", "blocking": 0}
  },
  "verifier": {"files": {
    ".github/workflows/release-ci.yml": "<64-hex>",
    ".github/release-ci-trust.json": "<64-hex>",
    "scripts/release_ci.py": "<64-hex>",
    ".github/tests/test_release_ci.py": "<64-hex>"
  }},
  "release_artifact": {"id": 1, "digest": "sha256:<64-hex>", "name": "release-image-1-1"},
  "verification": {
    "regression": "passed", "migration": "passed", "postgresql": "passed",
    "container_smoke": "passed", "image_security": "passed", "postgresql_passed": 82
  },
  "image": {
    "platform": "linux/amd64", "archive_sha256": "<64-hex>",
    "config_digest": "sha256:<64-hex>", "manifest_digest": "sha256:<64-hex>"
  },
  "ecr": {
    "account_id": "478478097368", "region": "ap-northeast-2",
    "repository_uri": "478478097368.dkr.ecr.ap-northeast-2.amazonaws.com/toi-be",
    "tag": "<40-hex>", "digest": "sha256:<64-hex>",
    "digest_uri": "478478097368.dkr.ecr.ap-northeast-2.amazonaws.com/toi-be@sha256:<64-hex>",
    "remote_verified": true
  },
  "publish_workflow": {
    "run_id": 1, "run_attempt": 1, "workflow_sha": "<40-hex>", "dispatcher": "<GitHub login>"
  },
  "outcome": "published",
  "confirmed_at": "2026-11-26T00:00:00Z"
}
```

`approval.record_sha256`는 승인 댓글 본문 UTF-8 바이트의 SHA-256이다. 승인 댓글의 필드·Head·변경 목록 해시는 Release CI의 `approval_record`/`change_digest` 규칙으로 검증한다. `release_artifact.digest`는 GitHub API가 반환한 ZIP Digest이며 `image.archive_sha256`는 ZIP 안 OCI 파일의 해시다. `ecr.remote_verified=true`와 `outcome`은 ECR에서 SHA 태그와 Manifest Digest를 조회해 일치시킨 뒤에만 기록한다. 생성 파일은 `image-publish-{run_id}-{run_attempt}` Artifact로 업로드한다.

## 4. IAM 권한 및 OIDC 운영 설정

`deploy/iam/ecr-publish-policy.json`은 발행 전용 Role의 **권한 정책**이며 Trust Policy가 아니다. 운영자는 별도 승인 후 정책을 `toi-github-ecr-publish` Role에 연결한다. ECR 인증 토큰은 리소스 수준 지정이 불가능해 `*`를 사용하고, 모든 ECR API는 서울 리전, 나머지 권한은 `toi-be` ARN으로 제한한다. `sts:GetCallerIdentity`는 별도의 Allow 권한이 필요하지 않으므로 추가하지 않는다.

Push 레이어 업로드, Manifest 등록/확인, 필요한 대상 레이어 조회 및 저장소 설정 조회만 허용한다. SSM, EC2, IAM 변경, ECR 삭제, 저장소 생성/설정 변경, 스캔 시작/설정 및 스캔 결과 조회 권한은 포함하지 않는다. 최초 스캔 상태는 운영자 권한으로 별도 조회한다. 실제 Registry 동작과 IAM 검증 후 불필요한 권한은 제거한다.

기존 Provider를 재사용하는 발행 Role Trust Policy 기준:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::478478097368:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:ToI-walkwithdog@339425868/walkwithdog-BE@1409697696:ref:refs/heads/main"
      }
    }
  }]
}
```

동일한 Subject를 신뢰하는 기존 Role에는 SSM 실행 권한도 있다. 따라서 전용 Role을 사용한다고 해서 특정 Workflow의 기존 Role 인증까지 AWS에서 차단되는 것은 아니다. 보호된 main, Workflow 변경 리뷰, 관리자 실행자 검증 및 OIDC 권한을 발행 Job에만 부여하는 통제가 필요하다. AWS 권한 정책은 독립 승인이나 Workflow 출처 검증을 대체하지 않는다. 별도 Environment는 사용하지 않는다.

기존 Repository Variables `AWS_REGION`, `AWS_ACCOUNT_ID`, `ECR_REPOSITORY`, `AWS_ROLE_ARN`은 유지한다. 운영자는 다음 변수를 확인·설정한다.

| Variable | 값/형식 | 용도 |
| --- | --- | --- |
| `AWS_REGION` | `ap-northeast-2` | 기존 AWS Region |
| `AWS_ACCOUNT_ID` | `478478097368` | 기존 AWS 계정 |
| `ECR_REPOSITORY` | `toi-be` | 기존 immutable ECR 저장소 |
| `ECR_PUBLISH_ROLE_ARN` | `arn:aws:iam::478478097368:role/toi-github-ecr-publish` | 발행 전용 OIDC Role |
| `TRUSTED_RELEASE_WORKFLOW_SHA` | 검토된 Release CI의 소문자 40자리 Commit SHA | Release verifier pin |

`AWS_ROLE_ARN`을 신규 Role로 덮어쓰지 않는다. Access Key, Secret Key, ECR 로그인 토큰을 저장소 변수나 Artifact에 저장하지 않는다.

## 5. 실행 절차

### 로컬 단위 및 모의 통합 테스트

```powershell
python -m unittest discover -s .github/tests -p test_ecr_image_publish.py -v
```

표준 라이브러리 테스트는 합성 GitHub API·OCI·격리 Registry를 사용하며 실제 AWS ECR에는 접근하지 않는다.

### 운영 실행

1. Release CI Workflow SHA를 검토해 Repository Variable `TRUSTED_RELEASE_WORKFLOW_SHA`에 등록하고, ECR Role/Policy 및 위 Variables를 준비한다.
2. 보호된 `main`에서 관리자가 `ECR Image Publish`를 수동 실행한다. 입력은 `release_run_id`, `release_run_attempt`, `release_pr_number`다. `release_run_attempt`는 최신 시도와 일치해야 한다. 태그 대상은 현재 `github.sha`다.
3. Validate는 OIDC 없이 GitHub API의 공식 Release CI 성공 기록과 성공한 verifier job 로그를 확인한다. 로그의 승인 댓글 ID/작성자와 현재 수정되지 않은 관리자 댓글을 결속하고, trusted Workflow SHA, 최신 시도, exact-head 독립 승인, 병합된 PR/Base·Head·Tree, Artifact ID/ZIP Digest/출처, OCI Archive와 모든 이미지 Digest를 검증한다. 검증 후 현재 protected `main`과 run attempt를 다시 확인한다.
4. Validate가 전달한 단일 OCI 이미지, 증빙 및 승인 본문 SHA-256을 Publish가 GitHub에서 다시 조회·대조해 AWS 인증 전에 검사한다. Publish Job만 OIDC 권한을 가지며 고정된 계정·Role·리전·`toi-be` 저장소 설정을 확인한다.
5. 기존 SHA 태그가 없다면 Skopeo `copy --preserve-digests`로 검증된 OCI 이미지 그대로 발행한다. 동일 SHA/Digest는 멱등 성공, 다른 Digest는 차단한다. 복사 실패/응답 유실은 제한된 조회로 원격 상태를 재조정하며 맹목적 재전송은 하지 않는다.
6. ECR의 SHA 태그와 등록 Manifest Digest를 조회해 OCI Manifest와 일치할 때에만 계약 v1 `image-publish-evidence.json`을 생성하고 `image-publish-{run_id}-{run_attempt}` Artifact로 업로드한다. 이후 자동 EC2 배포는 하지 않는다.

누락된 변수, 권한, 승인, Artifact 또는 무결성 근거는 모두 실패 폐쇄한다. 실제 실행 전 운영자는 ECR Role Trust Policy, immutable 설정 및 Registry Scan on Push를 별도로 확인한다.

## 6. 실패 대응 및 재실행

| 상황 | 처리 |
| --- | --- |
| 승인 근거 / CI / PostgreSQL 결과 누락 | 발행 금지, 공식 검증 재확인 |
| Artifact 누락·만료·변조 | 발행 금지, 승인된 재검증 경로 확보 |
| Source SHA / Tree 불일치 | 발행 금지, 실제 병합 대상 재검토 |
| ECR IAM 조회 실패 | 미등록으로 간주하지 않고 중단 |
| 동일 태그·Digest | 멱등 처리 `already_present` |
| 동일 태그·다른 Digest | 충돌, 삭제·덮어쓰기 금지 |
| Push 성공 | 등록 Digest 일치 확인 후 `published` |
| Push 실패·Timeout·응답 유실 | 최대 3회 등록 조회 (즉시, 1초 후, 추가 2초 후) |
| 응답 유실 후 Digest 일치 | `reconciled` |
| 등록 미확인 | 중단, 운영자가 원격 상태 확인 후 동일 승인 대상 재실행 검토 |
| 증빙 저장 실패 | 이미지 자동 삭제 금지, 등록 상태 확인 후 승인된 동일 이미지 재실행 검토 |

초기 Timeout은 AWS 명령 30초, Skopeo 복사 600초다. 현재 모의 실행에서 검증된 설정이며 실제 실행기 연결 후 검증한다. 무조건적인 Push 재시도는 하지 않는다.

## 7. 발행 증빙과 후속 SSM

`image-publish-evidence.simulation.json`은 테스트 전용이며 `simulation=true`, `publication_authorized=false`, `deployable=false`로 생성한다. 운영용 `image-publish-evidence.json`은 Validate 승인 정보와 실제 ECR 등록 확인을 모두 통과한 경우에만 생성된다. 출력 필드 집합은 위 계약 v1과 정확히 일치한다.

후속 SSM 검증기는 Artifact 출처·Digest, Publish 실행 및 Job 시각, 승인된 Release Merge Commit, Release verifier 파일 해시, immutable ECR 이미지와 요청된 Digest를 별도로 확인한다. JSON의 성공 플래그만으로 배포를 허용하지 않는다. `TRUSTED_PUBLISHERS`가 비어 있거나 게시 코드 해시가 등록본과 다르면 배포 검증은 차단된다.

## 8. 운영자 조회 및 보존 정책

아래는 조회 전용 PowerShell 명령이다. 신규 Role 생성이나 ECR 변경 명령은 포함하지 않는다.

```powershell
aws ecr describe-repositories --repository-names toi-be --region ap-northeast-2 --output json --no-cli-pager
aws ecr batch-get-repository-scanning-configuration --repository-names toi-be --region ap-northeast-2 --output json --no-cli-pager
aws ecr get-lifecycle-policy --repository-name toi-be --region ap-northeast-2 --output json --no-cli-pager
aws ecr describe-images --repository-name toi-be --region ap-northeast-2 --output json --no-cli-pager
```

최초 실제 발행 이후 스캔 결과는 운영자 권한으로 다음과 같이 확인한다 (예시 SHA를 실제 값으로 교체).

```powershell
$sourceSha = "REPLACE_WITH_FULL_COMMIT_SHA"
aws ecr describe-image-scan-findings --repository-name toi-be --image-id "imageTag=$sourceSha" --region ap-northeast-2 --output json --no-cli-pager
```

스캔 진행 중에는 완료로 간주하지 않는다. 현재 및 이전 정상 배포 이미지의 SHA 태그와 Digest를 유지한다. 태그 없는 이미지의 7일 만료는 Push 시각 기준이며 태그가 제거된 시각 기준이 아니다. SHA 태그 이미지 누적으로 비용이 증가할 수 있으므로 운영 상태·롤백 대상·미배포 승인 후보를 대조하고 별도 승인 후 운영자가 불필요한 이미지만 삭제한다. 발행 Workflow에는 삭제 권한을 부여하지 않는다.

## 9. 격리 환경 통합 검증 (Phase 8)

기본 테스트는 AWS, 실제 GitHub, Docker 또는 Skopeo에 접근하지 않는다. 합성 GitHub API 응답과 실제 ZIP/OCI 파일, `127.0.0.1`의 임시 HTTP Registry 테스트 더블을 연결해 이미지의 바이트 전송·조회 및 증빙 생성을 검증한다. 테스트 더블의 blob PUT은 단순화된 방식이며 OCI Distribution 서버나 실제 Skopeo가 아니다. 실제 AWS 자격 증명 및 발행 권한은 필요하지 않다.

```powershell
python -m unittest discover -s .github/tests -p test_ecr_image_publish.py -v
```

테스트는 아래를 확인하며 실제 GitHub API와 AWS ECR을 호출하지 않는다.

- Release CI 실행·승인 댓글·독립 리뷰·Artifact ZIP 출처 및 OCI Archive 검증
- Publish 입력 재검증, SSM 계약 v1의 필드 집합과 증빙 생성
- 루프백 HTTP 이미지 전송, 등록 Manifest·Config·Layer 바이트 확인 및 멱등 처리
- 동일 SHA/Digest 재실행 시 등록 상태 재조회 및 Push 생략
- 동일 SHA/다른 Digest 충돌 시 기존 바이트 보존 및 신규 증빙 미생성
- 이미지 등록 직후 응답 유실 시 상태 재조회 및 재전송 방지
- 변조된 Archive의 전송 차단 및 증빙 미생성

모의 발행 결과는 계속 `publication_authorized=false`, `deployable=false`이며 운영용 증빙을 만들지 않는다. 선택 테스트는 임시 루프백 Registry만 사용하고 AWS 권한을 필요로 하지 않는다.

### 선택 테스트: 실제 Skopeo 및 임시 Registry

Linux/WSL 등 Docker와 Skopeo가 이미 준비된 테스트 환경에서만 실행한다. 운영 Registry는 사용하지 않는다. `127.0.0.1:5000`이 비어 있는지 확인하고, 실제 ECR 자격 증명을 사용하지 않는다. 아래 Registry 컨테이너는 이 검증을 위한 임시 리소스이며 영구 인프라가 아니다.

```bash
docker run --rm -d --name toi-ecr-test-registry -p 127.0.0.1:5000:5000 registry:2
# Registry가 준비되었는지 확인
curl --fail http://127.0.0.1:5000/v2/
TOI_TEST_LOCAL_REGISTRY=1 python3 -m unittest discover -s .github/tests -p test_ecr_image_publish.py -v
docker stop toi-ecr-test-registry
```

선택 테스트는 새 임시 Repository에 OCI Archive를 실제 Skopeo로 복사하고 Registry의 Manifest 응답 헤더·바이트 및 모든 참조 blob의 Digest/크기를 확인한다. 테스트 종료 후 임시 Registry 컨테이너를 정리한다. 테스트 실패 시에도 컨테이너 정리를 수행한다. HTTP/TLS 검증 예외는 로컬 선택 테스트에만 적용하며 운영용 발행 코드의 TLS 요구사항을 변경하지 않는다.

## 10. PR #143의 TRUSTED_PUBLISHERS 해시 등록

배포 검증기의 `TRUSTED_PUBLISHERS`는 비어 있으면 배포를 차단한다. PR #137의 검토·승인·`main` 병합이 끝난 뒤, 같은 병합된 `main` Commit에서 발행 Workflow와 Python Publisher 두 파일의 **Git blob 원문 바이트** SHA-256을 계산한다. 체크아웃 시 줄바꿈 변환이 개입하지 않도록 `git show` 출력을 직접 해시한다.

```powershell
python -c "import hashlib,subprocess; h=subprocess.check_output(['git','rev-parse','origin/main']).decode().strip(); paths=['.github/workflows/ecr-image-publish.yml','scripts/ecr_image_publish.py']; [print(p,hashlib.sha256(subprocess.check_output(['git','show',f'{h}:{p}'])).hexdigest()) for p in paths]"
```

1. 최종 병합된 `origin/main` SHA를 확인하고 두 해시가 동일 Commit에서 산출됐는지 기록한다.
2. PR #143에서 `TRUSTED_PUBLISHERS`의 단일 항목에 두 경로와 각 SHA-256을 함께 등록한다. 경로 집합은 정확히 `.github/workflows/ecr-image-publish.yml`, `scripts/ecr_image_publish.py`여야 한다.
3. PR #143에서 독립 리뷰와 필수 CI로 검토한다. 한 파일이라도 수정되면 새 `main` Commit에서 두 파일의 해시를 다시 계산하고 별도 승인된 변경으로 갱신한다.
4. 게시 브랜치의 작업 트리 해시나 GitHub의 SHA-1 blob ID를 등록하지 않는다. 병합된 `main`의 바이트 해시만 사용한다. 해시 미등록·불일치 시 backend deploy는 계속 차단된다.

## 11. 남은 완료 조건

- 독립 리뷰, PR #137 CI, `main` 병합
- `TRUSTED_RELEASE_WORKFLOW_SHA`, 발행용 IAM Role/Policy 및 Repository Variables 운영자 설정
- PR #143에서 병합된 Publisher 파일 해시 등록 후 배포 증빙 소비 연동 확인
- 최초 승인 이미지 발행, ECR Digest 및 자동 Scan on Push 확인
- SSM 배포는 별도 Issue에서 진행

## Reference

- #125, #136, PR #127
- `docs/deployment/ARCHITECTURE.md`, `EC2_SETUP.md`, `RUNBOOK.md`, `ROLLBACK.md`
- [AWS ECR Push IAM permissions](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html)
- [AWS ECR Service Authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecr.html)
- [AWS STS GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html)
