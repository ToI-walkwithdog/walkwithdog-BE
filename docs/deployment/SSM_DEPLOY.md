# SSM 백엔드 배포 Workflow (#142)

## 1. 현재 상태

`Backend Deploy` Workflow는 보호된 `main`에서 관리자가 수동 실행한다. ECR 발행 증빙과 Image Digest를 확인한 뒤 기존 SSM 문서로 EC2의 `deploy.sh`(#109)를 호출한다.

**현재는 실행해도 Validate Job에서 항상 차단된다.** 운영용 `image-publish-evidence.json` 계약은 #136에서 확정한다. 이 Workflow는 확정된 계약을 그대로 재사용할 예정이며, 연동 전까지 `scripts/backend_deploy.py`의 `evidence_contract_pending`이 증빙 확인 단계에서 실행을 막는다. 연동은 같은 경로 안의 별도 변경으로 진행한다.

이 Workflow는 다음을 하지 않는다.

- DB 접근, 마이그레이션, 호환성 근거(`schema-record.json`, 이미지별 승인 파일)의 발급과 업로드
- ECR 발행, 이미지 빌드, SSM 문서 등록과 수정, EC2 스크립트 설치
- 롤백 문서 호출이나 이전 이미지 자동 재배포
- `main` Push 또는 ECR 발행 직후의 자동 배포

## 2. 구성

| Job | 권한 | 내용 |
| --- | --- | --- |
| `validate` | `contents: read`, `actions: read`. OIDC와 AWS 접근 없음 | 실행 이벤트와 보호된 `main`, 실행자 관리자 권한, 입력 형식, Repository Variables, ECR 발행 실행, Source SHA의 `main` 포함 여부, 발행 증빙 |
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
2. 발행 실행은 같은 저장소 `main`의 `.github/workflows/ecr-image-publish.yml`이 `workflow_dispatch`로 실행되어 성공한 최신 시도여야 한다. 실행의 `head_sha`를 Source SHA로 쓰고, 현재 `main`에 포함된 커밋인지 compare API로 확인한다.
3. 발행 증빙이 요청한 Digest와 Source SHA를 확인해야 다음 Job으로 값을 넘긴다. 현재는 이 단계에서 차단된다.
4. `deploy`는 `configure-aws-credentials`로 배포 전용 Role을 사용한다. `sts get-caller-identity`의 계정과 Role 이름을 확인하고, ECR에서 Digest로 이미지 한 개를 조회해 저장소와 Source SHA 태그를 확인한다.
5. `ToI-DeploymentStatus`로 배포 전 상태를 읽고 아래 기준으로 구분한다.
6. `preflight`는 여기서 성공으로 끝나며 서버를 변경하지 않는다.
7. `deploy`는 `ToI-DeployBackend`에 `ImageDigest` 하나를 전달한다. 결과를 최대 1080초까지 조회하고, 종료 코드를 기록한 뒤 배포 후 상태를 다시 읽는다.

## 5. 배포 전 상태 구분

| 구분 | 조건 | 처리 |
| --- | --- | --- |
| 최초 배포 전 미실행 | `status=ready current=- previous=- last_result=none`, `api absent`, DB 컨테이너 `running healthy` | 배포 허용 |
| 정상 운영 | `status=ready`, `current`에 Digest가 있고 API와 DB가 `running healthy` | 배포 허용 |
| 작업 중 | `deployment=busy` | 중단, 진행 중인 작업이 끝난 뒤 재실행 |
| 장애 | `in_progress`, `blocked`, 기록된 `current`가 있는데 API가 없거나 비정상, 기록이 없는데 API나 실패 이력이 있음, 최초 배포 전인데 DB 미준비 | 중단, [롤백과 수동 복구](https://github.com/ToI-walkwithdog/walkwithdog-BE/blob/dev/docs/deployment/ROLLBACK.md) 절차로 확인 |
| 조회 실패 | 상태 문서의 실패, 0이 아닌 응답 코드, 출력 형식 오류 | 중단 |

상태 출력 형식 검증은 AWS Connection Test(#121)의 규칙을 그대로 따른다. 체크섬 줄은 `deploy`, `rollback`, `deployment_common` 순서여야 한다. Summary에는 상태 줄의 값, 컨테이너 상태와 헬스, 설치 스크립트 SHA-256만 남긴다.

Job Summary에는 실행자, 발행 실행 ID와 시도, Source SHA, Digest, 각 SSM Command ID, 배포 결과와 종료 코드 의미를 기록한다. GitHub API 오류는 HTTP 상태 코드만 표시한다.

## 6. SendCommand 응답 유실

- `send-command`는 AWS CLI 자동 재시도를 끄고(`AWS_MAX_ATTEMPTS=1`) 한 번만 호출한다. 재호출하면 배포가 두 번 실행될 수 있다.
- 명령마다 `toi-backend-deploy <단계> run <Run ID> attempt <시도>` Comment를 붙인다. 단계는 `pre-status`, `deploy`, `post-status`다.
- 호출이 오류나 시간 초과로 끝나거나 Command ID가 없으면 다시 보내지 않는다. `ssm:ListCommands`로 같은 인스턴스와 문서, 호출 2분 전 이후의 명령 중 Comment가 같은 명령을 찾는다. 즉시, 5초 뒤, 10초 뒤 최대 세 번 조회한다.
- 정확히 하나가 있고 문서, 문서 버전, 인스턴스, 파라미터가 요청과 같을 때만 그 명령을 추적한다.
- 찾지 못하거나, 둘 이상이거나, 내용이 다르거나, 조회 자체가 실패하면 중단한다. 운영자는 재실행 전에 `ToI-DeploymentStatus`로 실제 상태를 확인한다.

## 7. 결과 처리

`deploy.sh` 종료 코드는 [배포 RUNBOOK](https://github.com/ToI-walkwithdog/walkwithdog-BE/blob/dev/docs/deployment/RUNBOOK.md)의 의미로 Summary에 기록한다.

| 결과 | Workflow 판정 |
| --- | --- |
| `Success`, 0 | 배포 후 상태가 `ready`, `current`가 요청 Digest, `last_result`가 `success` 또는 `unchanged`, API와 DB가 `running healthy`일 때만 성공 |
| 2~7, 9 | 실패. 의미와 배포 후 상태를 기록 |
| 8 | 직전 정상 서비스로 복구됐지만 배포는 실패 |
| `TimedOut`, `Cancelled`, 조회 시간 초과 | 실패. 명령은 취소하지 않으며 상태 조회로 실제 결과를 확인 |

헬스체크는 `deploy.sh`가 수행하는 Docker 헬스체크, 루프백 `/db-test`, 실행 이미지 ID 대조와 배포 후 상태 조회로 확인한다. 공개 HTTPS 엔드포인트 점검은 범위 밖이다.

SSM 표준출력과 표준오류 원문, AWS CLI 오류 본문, 자격 증명은 로그와 Summary에 남기지 않는다. AWS 오류는 `AccessDeniedException` 같은 오류 코드만 표시한다.

## 8. 롤백

롤백 절차는 바꾸지 않는다.

- 배포 실패 시 `deploy.sh`가 현재 DB와 호환되는 직전 이미지로 자동 복구하고 종료 코드 8을 반환한다.
- 운영자 롤백과 상태 재조정은 EC2에서 `rollback.sh`와 `ROLLBACK.md` 절차로 수행한다. 이 Workflow에는 롤백 문서나 자동 재배포가 없다.
- 이전에 승인된 Digest를 이 Workflow로 다시 배포하려면, 그 Digest의 발행 증빙과 현재 DB에 유효한 호환성 승인 파일이 있어야 한다.

## 9. IAM

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

## 10. 실행 전 선행 조건

실제 `preflight` 성공에는 다음이 필요하다. `deploy`는 별도 승인으로 실행한다.

- #136 운영용 발행 증빙 계약과 실제 이미지 발행, 이 Workflow의 증빙 검증 연동
- 배포 전용 Role 생성과 `SSM_DEPLOY_ROLE_ARN` 등록
- EC2의 배포 스크립트 설치와 `ToI-DeployBackend` 등록([EC2 설치](https://github.com/ToI-walkwithdog/walkwithdog-BE/blob/dev/docs/deployment/EC2_SETUP.md))
- DB 컨테이너 준비. `deploy`에는 추가로 #115의 적용 기록과 대상 이미지의 호환성 승인 파일이 필요하다

## 11. 검증

로컬에서 AWS와 GitHub에 접근하지 않고 실행한다.

```bash
python3 -I -B -m unittest discover -s .github/tests -p test_backend_deploy.py -v
docker run --rm --mount "type=bind,source=$PWD,target=/repo,readonly" --workdir /repo \
  rhysd/actionlint:1.7.12 -color .github/workflows/backend-deploy.yml
```

단위 테스트는 합성 GitHub API 응답과, 호출을 기록하는 모의 AWS CLI 실행기를 사용한다. 실행 조건, 입력, 설정, 발행 실행, 증빙 차단, 상태 구분, SSM 호출과 조회, 응답 유실, 종료 코드, 비밀값 비노출, Workflow 권한과 IAM 범위를 검사한다.

## Reference

- #109, #121, #125, #136, #115
- [AWS SSM SendCommand](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_SendCommand.html), [ListCommands](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_ListCommands.html)
- [AWS SSM 권한](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssystemsmanager.html)
