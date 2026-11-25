# SSM 백엔드 배포 Workflow (#142)

## 1. 현재 상태

`Backend Deploy` Workflow는 보호된 `main`에서 관리자가 수동 실행한다. ECR 발행 증빙과 Image Digest를 확인한 뒤 기존 SSM 문서로 EC2의 `deploy.sh`(#109)를 호출한다.

**현재는 실행해도 Validate Job에서 항상 차단된다.** ECR Publish와 SSM Deploy 사이의 인터페이스 계약 v1(#136 작업 B)에 따라 발행 증빙을 검증한다. 다만 리뷰와 관리자 승인을 거친 ECR 발행기 파일 해시(`TRUSTED_PUBLISHERS`)가 아직 비어 있어 증빙 확인 첫 단계에서 실행을 막는다. 해시 목록은 PR #137의 최종 구현과 리뷰 이후 별도 변경으로 등록한다.

이 Workflow는 다음을 하지 않는다.

- DB 접근, 마이그레이션, 호환성 근거(`schema-record.json`, 이미지별 승인 파일)의 발급과 업로드
- ECR 발행, 이미지 빌드, SSM 문서 등록과 수정, EC2 스크립트 설치
- 롤백 문서 호출이나 이전 이미지 자동 재배포
- `main` Push 또는 ECR 발행 직후의 자동 배포

## 2. 구성

| Job | 권한 | 내용 |
| --- | --- | --- |
| `validate` | `contents: read`, `actions: read`, `pull-requests: read`. OIDC와 AWS 접근 없음 | 실행 이벤트와 보호된 `main`, 실행자 관리자 권한, 입력 형식, Repository Variables, ECR 발행 실행과 Job, 발행기 신뢰 해시, 증빙 Artifact, Release 병합 기록, Source SHA의 `main` 포함 여부 |
| `deploy` | `contents: read`, `id-token: write` | 배포 전용 Role 인증, 호출 주체 확인, ECR Digest와 Source SHA 태그 확인, 배포 전 상태 조회, `operation=deploy`일 때 배포, 배포 후 상태 조회 |

- 최상위 권한은 `permissions: {}`다. OIDC 토큰은 `deploy` Job에서만 발급할 수 있다.
- `concurrency: toi-be-backend-deploy`, `cancel-in-progress: false`로 실행을 직렬화한다. GitHub는 대기 중인 실행을 하나만 유지하므로, 대기 중에 새 실행이 들어오면 이전 대기 실행은 취소된다. 진행 중인 실행은 취소되지 않는다. EC2의 `flock` 잠금(종료 코드 4)도 그대로 적용된다.
- 재실행은 "Re-run all jobs"만 허용한다. 개별 Job 재실행이나 "Re-run failed jobs"는 이전 시도의 `validate` 결과를 재사용하므로, `deploy`가 실행 시도와 `validate` 시도가 다르거나 재실행자가 원래 실행자와 다르면 AWS 호출 전에 중단한다. 전체 재실행에서는 `validate`가 관리자 권한과 증빙을 다시 확인한다.
- `deploy` Job의 제한 시간은 25분이다. Workflow가 취소되거나 시간을 넘겨도 이미 보낸 SSM 명령은 취소하지 않는다. 배포 Role에는 `ssm:CancelCommand` 권한이 없다.

## 3. 입력과 설정

| 입력 | 형식 |
| --- | --- |
| `operation` | `preflight`(기본값) 또는 `deploy` |
| `publish_run_id`, `publish_run_attempt` | `ECR Image Publish` 실행 ID와 최신 시도 번호 |
| `image_digest` | `sha256:` 뒤 소문자 16진수 64자 |

입력값은 셸 스크립트에 직접 넣지 않고 환경변수로 전달한 뒤 Python에서 검증한다.

| Repository Variable | 요구값 |
| --- | --- |
| `AWS_REGION` | `ap-northeast-2` |
| `AWS_ACCOUNT_ID` | 12자리 계정 ID |
| `ECR_REPOSITORY` | `toi-be` |
| `EC2_INSTANCE_ID` | 기존 AWS Connection Test와 같은 인스턴스 |
| `SSM_DEPLOY_ROLE_ARN` | `arn:aws:iam::<계정>:role/toi-github-ssm-deploy` (신규, 별도 승인 후 등록) |

기존 `AWS_ROLE_ARN`과 `toi-github-actions-role`, AWS Connection Test는 변경하지 않는다.

SSM 문서 버전은 코드에 고정한다. `ToI-DeploymentStatus`는 `1`(#121에서 확인)이다. `ToI-DeployBackend`는 `1`로 가정했으며 등록 여부와 버전은 아직 확인하지 않았다. 등록된 버전이 다르면 리뷰를 거친 코드 변경으로 맞춘다.

## 4. 실행 흐름

1. `validate`는 `workflow_dispatch`, `refs/heads/main`, 보호 브랜치, 저장소 ID, Workflow 경로, 실행자와 재실행자의 일치를 확인한다. 실행자의 저장소 권한이 `admin`이어야 한다.
2. 발행 실행은 같은 저장소(ID `1409697696`) `main`의 `ECR Image Publish`(`.github/workflows/ecr-image-publish.yml`)가 `workflow_dispatch`로 실행되어 성공한 최신 시도여야 한다. 실행자와 재실행자가 같고 실행자가 `admin`이어야 하며, 그 시도의 `Publish image` Job이 성공해야 한다. 실행의 `head_sha`를 Source SHA로 쓰고, 현재 `main`에 포함된 커밋인지 compare API로 확인한다.
3. 6장의 기준으로 발행 증빙을 검증한 뒤에만 `image_digest`, `source_sha`, `digest_uri`를 다음 Job으로 넘긴다. 현재는 발행기 신뢰 해시가 없어 이 단계에서 차단된다.
4. `deploy`는 `digest_uri`가 승인된 저장소와 Digest로 구성됐는지 확인한 뒤 `configure-aws-credentials`로 배포 전용 Role을 사용한다. `sts get-caller-identity`의 계정과 Role 이름을 확인하고, 배포 직전 ECR에서 Digest로 이미지 한 개를 조회해 저장소, 실제 `imageDigest`, Source SHA 태그를 확인한다. `latest`나 SHA 태그는 배포 대상으로 쓰지 않는다.
5. `ToI-DeploymentStatus`로 배포 전 상태를 읽고 아래 기준으로 구분한다. 최초 배포 전 미실행, 최초 배포 재시도, 정상 운영에서만 다음 단계로 진행한다.
6. `preflight`는 여기서 성공으로 끝나며 서버를 변경하지 않는다.
7. `deploy`는 `ToI-DeployBackend`에 `ImageDigest` 파라미터로 `ecr.digest` 하나만 전달한다. 결과를 최대 1080초까지 조회하고, 종료 코드를 기록한 뒤 배포 후 상태를 다시 읽는다.

## 5. 배포 전 상태 구분

| 구분 | 조건 | 처리 |
| --- | --- | --- |
| 최초 배포 전 미실행 | `status=ready current=- previous=- last_result=none`, `api absent`, DB 컨테이너 `running healthy` | 배포 허용 |
| 최초 배포 재시도 | `status=ready current=- previous=- last_result=failed`, `api absent`, DB 컨테이너 `running healthy`를 모두 만족 | 관리자가 수동으로 다시 실행한 경우에 한해 배포 허용 |
| 정상 운영 | `status=ready`, `current`에 Digest가 있고 API와 DB가 `running healthy` | 배포 허용 |
| 작업 중 | `deployment=busy` | 중단, 진행 중인 작업이 끝난 뒤 재실행 |
| 장애 | `in_progress`, `blocked`, 기록된 `current`가 있는데 API가 없거나 비정상, `current`가 없는데 `previous`나 API가 있거나 `last_result`가 `none`, `failed` 이외의 값, 최초 배포 전인데 DB 미준비 | 중단, [롤백과 수동 복구](https://github.com/ToI-walkwithdog/walkwithdog-BE/blob/dev/docs/deployment/ROLLBACK.md) 절차로 확인 |
| 조회 실패 | 상태 문서의 실패, 0이 아닌 응답 코드, 출력 형식 오류 | 중단 |

최초 배포 재시도 상태는 `deploy.sh`가 최초 배포의 사전 점검(종료 코드 3, 6, 7 등)에서 API를 바꾸기 전에 중단하면서 기록한다. `deploy.sh`도 이 상태에서 재시도를 허용한다. Workflow는 이 상태를 자동으로 재시도하지 않고 서버 상태 파일을 초기화하지도 않는다. 관리자가 원인(승인 파일, ECR Pull 등)을 해결한 뒤 Workflow를 새로 실행하거나 전체 재실행해야 한다. 재시도에서도 `deploy.sh`가 DB 호환성 근거와 배포 사전 검증을 처음부터 다시 수행한다. 하나라도 조건이 어긋나면 장애로 보고 기존 차단 정책을 따른다.

상태 출력 형식 검증은 AWS Connection Test(#121)의 규칙을 그대로 따른다. 체크섬 줄은 `deploy`, `rollback`, `deployment_common` 순서여야 한다. Summary에는 상태 줄의 값, 컨테이너 상태와 헬스, 설치 스크립트 SHA-256만 남긴다.

Job Summary에는 실행자, 발행 실행 ID와 시도, Source SHA, Digest, 각 SSM Command ID, 배포 결과와 종료 코드 의미를 기록한다. GitHub API 오류는 HTTP 상태 코드만 표시한다.

## 6. 발행 증빙 검증 (계약 v1)

증빙은 ECR 발행 실행이 만든 Artifact `image-publish-{run_id}-{run_attempt}`다. ZIP 루트에 `image-publish-evidence.json` 하나만 있어야 한다. 기존 증빙 구조를 그대로 사용하며, 값 자체를 승인 근거로 믿지 않고 GitHub 기록과 대조한다. `deployable=true`는 배포 후보라는 뜻일 뿐이며 SSM 배포는 별도의 관리자 수동 실행으로만 진행한다.

| 단계 | 확인 내용 |
| --- | --- |
| 발행기 신뢰 | Publish Run의 실행 Commit에서 `.github/workflows/ecr-image-publish.yml`, `scripts/ecr_image_publish.py`의 바이트 SHA-256을 계산해 `TRUSTED_PUBLISHERS`의 리뷰된 항목과 대조한다. 목록이 비어 있으면 차단한다. 현재 `main`과 같다는 이유로 자동 갱신하지 않고, 증빙 JSON의 값을 신뢰 기준으로 쓰지 않는다. 증빙의 `verifier.files`는 Release CI 보호 파일 4개의 해시이며 발행기 해시가 아니다. |
| Artifact | 해당 실행의 정확한 이름 1개, 미만료, `workflow_run`의 실행 ID, 저장소 ID, `head_sha`, `main` 일치. API의 `digest`와 다운로드한 ZIP의 SHA-256 일치. 다운로드는 API 토큰 없이 저장소가 발급한 HTTPS URL에서 받는다. |
| ZIP과 JSON | 파일 1개와 정확한 파일명, 중첩 경로와 중복 파일 차단, 크기는 기존 증빙 소비자(`scripts/ecr_image_publish.py`)와 같은 8 MiB 제한. UTF-8 JSON에서 중복 키, `NaN`, `Infinity`를 거부한다. 객체마다 정해진 필드만 허용하고 문자열, 정수, Boolean을 엄격히 구분한다. |
| 운영용 표시 | `schema_version` 1, `record_type` `production`, `simulation` false, `publication_authorized` true, `deployable` true, `ecr.remote_verified` true, `outcome` `published`, `already_present`, `reconciled` 중 하나, 검사 5개 `passed`, `postgresql_passed` 82, `image.platform` `linux/amd64`. 모의 증빙은 차단한다. |
| 필드 정합성 | `source.sha`와 `ecr.tag`, `publish_workflow.workflow_sha`, Publish Run `head_sha` 일치. `source.tree`와 `release.merge_tree_sha` 일치. `publish_workflow`의 실행 ID, 시도, 실행자 일치. `release_workflow.workflow_sha`와 `release.base_sha`, `approval.review.head`와 `release.head_sha` 일치, `blocking` 0. `image.manifest_digest`, `ecr.digest`, 요청 Digest 일치, `ecr.digest_uri`는 `repository_uri@digest`. `release_artifact.name`은 Release 실행 ID와 시도로 구성. `confirmed_at`은 `Publish image` Job 실행 시간(앞뒤 60초 허용) 안. |
| Release 병합 | Source Commit의 Tree가 `source.tree`와 같고 부모가 `release.base_sha`, `release.head_sha` 순서의 두 개다. Release PR은 `main`에 병합됐고 `merge_commit_sha`가 `source.sha`, PR Head가 `release.head_sha`다. |
| ECR | 계정 `478478097368`, 리전 `ap-northeast-2`, 저장소 `toi-be`, 태그는 40자리 Source SHA. 배포 Job에서 실제 ECR의 `imageDigest`와 태그를 다시 확인한다. |

Release CI 이미지 Artifact(`release_artifact`)의 만료는 배포 차단 조건이 아니며, OCI Archive를 다시 내려받거나 빌드하지 않는다. ECR 스캔 확인은 #136이 담당하며 이 Workflow는 스캔 결과로 차단하지 않는다.

## 7. SendCommand 응답 유실

- `send-command`는 AWS CLI 자동 재시도를 끄고(`AWS_MAX_ATTEMPTS=1`) 한 번만 호출한다. 재호출하면 배포가 두 번 실행될 수 있다.
- 명령마다 `toi-backend-deploy <단계> run <Run ID> attempt <시도>` Comment를 붙인다. 단계는 `pre-status`, `deploy`, `post-status`다.
- 호출이 오류나 시간 초과로 끝나거나 Command ID가 없으면 다시 보내지 않는다. `ssm:ListCommands`로 같은 인스턴스와 문서, 호출 2분 전 이후의 명령 중 Comment가 같은 명령을 찾는다. 즉시, 5초 뒤, 10초 뒤 최대 세 번 조회한다.
- 정확히 하나가 있고 문서, 문서 버전, 인스턴스, 파라미터가 요청과 같을 때만 그 명령을 추적한다.
- 찾지 못하거나, 둘 이상이거나, 내용이 다르거나, 조회 자체가 실패하면 중단한다. 운영자는 재실행 전에 `ToI-DeploymentStatus`로 실제 상태를 확인한다.

## 8. 결과 처리

`deploy.sh` 종료 코드는 [배포 RUNBOOK](https://github.com/ToI-walkwithdog/walkwithdog-BE/blob/dev/docs/deployment/RUNBOOK.md)의 의미로 Summary에 기록한다.

| 결과 | Workflow 판정 |
| --- | --- |
| `Success`, 0 | 배포 후 상태가 `ready`, `current`가 요청 Digest, `last_result`가 `success` 또는 `unchanged`, API와 DB가 `running healthy`일 때만 성공 |
| 2~7, 9 | 실패. 의미와 배포 후 상태를 기록 |
| 8 | 직전 정상 서비스로 복구됐지만 배포는 실패 |
| `TimedOut`, `Cancelled`, 조회 시간 초과 | 실패. 명령은 취소하지 않으며 상태 조회로 실제 결과를 확인 |

헬스체크는 `deploy.sh`가 수행하는 Docker 헬스체크, 루프백 `/db-test`, 실행 이미지 ID 대조와 배포 후 상태 조회로 확인한다. 공개 HTTPS 엔드포인트 점검은 범위 밖이다.

SSM 표준출력과 표준오류 원문, AWS CLI 오류 본문, 자격 증명은 로그와 Summary에 남기지 않는다. AWS 오류는 `AccessDeniedException` 같은 오류 코드만 표시한다.

## 9. 롤백

롤백 절차는 바꾸지 않는다.

- 배포 실패 시 `deploy.sh`가 현재 DB와 호환되는 직전 이미지로 자동 복구하고 종료 코드 8을 반환한다.
- 운영자 롤백과 상태 재조정은 EC2에서 `rollback.sh`와 `ROLLBACK.md` 절차로 수행한다. 이 Workflow에는 롤백 문서나 자동 재배포가 없다.
- 이전에 승인된 Digest를 이 Workflow로 다시 배포하려면, 그 Digest의 발행 증빙과 현재 DB에 유효한 호환성 승인 파일이 있어야 한다.

## 10. IAM

`deploy/iam/ssm-deploy-policy.json`은 `toi-github-ssm-deploy` Role의 Inline 권한 정책 초안이다. 적용 전에 `REPLACE_WITH_EC2_INSTANCE_ID`를 실제 인스턴스 ID로 바꾼다.

| 권한 | 범위 |
| --- | --- |
| `ssm:SendCommand` | 지정 EC2 인스턴스, `ToI-DeployBackend`, `ToI-DeploymentStatus` 문서 |
| `ssm:GetCommandInvocation`, `ssm:ListCommands` | 리소스 수준 제한이 불가능해 `*`, 서울 리전 조건 |
| `ecr:DescribeImages` | `toi-be` 저장소 |

`AWS-RunShellScript`를 비롯한 다른 문서, `ssm:CancelCommand`, ECR Push와 삭제, ECR 인증 토큰, EC2와 IAM 변경 권한은 없다. `sts:GetCallerIdentity`는 별도 권한이 필요 없다.

신뢰 정책은 기존 OIDC Provider와 `main` Subject를 재사용한다. 문서용이며 아직 적용하지 않았다.

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

Role 생성, 정책 연결, `SSM_DEPLOY_ROLE_ARN` 등록은 별도 승인 후 운영자가 수행한다. 장기 Access Key는 사용하지 않는다.

### 같은 OIDC Subject의 한계

기존 `toi-github-actions-role`, ECR 발행 Role(#136, 생성 예정)과 이 Role은 모두 같은 `main` Subject를 신뢰한다. 따라서 `main`에서 OIDC 권한을 가진 Workflow라면 세 Role 중 어느 것이든 인증할 수 있다. Role을 나누는 것은 권한 범위를 줄일 뿐, 특정 Workflow만 특정 Role을 쓰도록 AWS에서 강제하지는 않는다. 기존 Role에는 SSM 실행 권한도 남아 있다.

이 한계는 다음으로 보완한다.

- 보호된 `main`과 Workflow 변경 리뷰
- Workflow 안의 관리자 실행자 확인
- OIDC 권한을 `deploy` Job에만 부여
- 호출 주체가 `toi-github-ssm-deploy`인지 실행 중 확인

기존 Role의 `AWS-RunShellScript` 권한 제거는 #121의 후속 작업으로 별도 진행한다.

## 11. 실행 전 선행 조건

실제 `preflight` 성공에는 다음이 필요하다. `deploy`는 별도 승인으로 실행한다.

- PR #137 최종 구현과 리뷰 이후 ECR 발행기 파일 해시를 `TRUSTED_PUBLISHERS`에 등록하는 별도 변경(독립 리뷰와 관리자 승인)
- 계약 v1의 운영용 증빙을 만드는 실제 ECR 발행 실행
- 배포 전용 Role 생성과 `SSM_DEPLOY_ROLE_ARN` 등록
- EC2의 배포 스크립트 설치와 `ToI-DeployBackend` 등록([EC2 설치](https://github.com/ToI-walkwithdog/walkwithdog-BE/blob/dev/docs/deployment/EC2_SETUP.md))
- DB 컨테이너 준비. `deploy`에는 추가로 #115의 적용 기록과 대상 이미지의 호환성 승인 파일이 필요하다

## 12. 검증

로컬에서 AWS와 GitHub에 접근하지 않고 실행한다.

```bash
python3 -I -B -m unittest discover -s .github/tests -p test_backend_deploy.py -v
docker run --rm --mount "type=bind,source=$PWD,target=/repo,readonly" --workdir /repo \
  rhysd/actionlint:1.7.12 -color .github/workflows/backend-deploy.yml
```

단위 테스트는 합성 GitHub API 응답과, 호출을 기록하는 모의 AWS CLI 실행기를 사용한다. 실행 조건, 입력, 설정, 발행 실행과 Job, 발행기 신뢰 해시, 증빙 Artifact와 ZIP, 엄격한 JSON과 자료형, 운영용 표시, 필드 정합성, Release 병합 기록, 상태 구분, SSM 호출과 조회, 응답 유실, 종료 코드, 비밀값 비노출, Workflow 권한과 IAM 범위를 검사한다.

## Reference

- #109, #121, #125, #136, #115
- [AWS SSM SendCommand](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_SendCommand.html), [ListCommands](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_ListCommands.html)
- [AWS SSM 권한](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssystemsmanager.html)
