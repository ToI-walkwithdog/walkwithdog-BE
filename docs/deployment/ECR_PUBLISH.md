# ECR 이미지 발행 운영 가이드 (#136)

## 1. 현재 상태와 작업 범위

이 문서는 #136의 발행 준비 단계에 대한 운영 기준이다. **현재 실제 발행은 비활성화되어 있다.**
`ecr-image-publish.yml`에는 AWS 인증 및 이미지 복사 단계가 없으며 Publish Job은 강제로 건너뛴다. Validate Job도 공식 승인 Gate에서 실패한다. `ecr_image_publish.py`의 발행·증빙 함수는 주입된 모의 실행기를 사용하는 테스트용이며 실제 CLI 발행 경로는 없다. 검증 결과의 `publication_authorized=false`를 승인으로 해석하지 않는다.

- 기존 Dev CI, AWS Connection Test, Release CI(#125), EC2, DB 및 SSM 문서는 변경하지 않는다.
- 신규 GitHub Environment, ECR 저장소, OIDC Provider, 승인 관리 시스템 및 별도 증빙 저장소는 만들지 않는다.
- 기존 IAM Role과 Policy는 변경하지 않는다. 신규 발행 전용 Role과 Inline Policy는 별도 승인 후 적용한다.
- #125의 공식 검증 및 이미지 비게시 정책 변경 승인, 실제 Artifact 계약 검증, 독립 리뷰와 관리자 승인이 선행되어야 한다.

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

## 3. Release CI 전달 계약 (제안 확정, 생산자 연동 미완료)

#125의 공식 `workflow_dispatch` 실행에서 전체 회귀, 마이그레이션, 별도 PostgreSQL 82개, FastAPI 스모크 및 이미지 보안 검사를 통과한 동일 이미지를 전달한다. 스모크 이미지와 OCI Config/Manifest 및 모든 레이어의 동일성은 생산자 측에서도 입증해야 한다. 후보 코드가 승인·검증 증빙이나 업로드를 통제하지 않도록 한다.

Artifact 이름: `release-image-{run_id}-{run_attempt}`. ZIP의 루트에는 정확히 `image.oci.tar`와 `release-evidence.json`을 둔다. 두 파일은 같은 Artifact이므로 보존 기간은 함께 7일이다. 별도 발행 성공 증빙은 90일 보존을 목표로 하며 저장소 허용 범위를 확인한다.

`release-evidence.json`의 v1 필수 정보:

- `schema_version`, `repository`
- `release`: `pr_number`, `base_sha`, `head_sha`, `merge_tree_sha`
- `workflow`: `run_id`, `run_attempt`, `workflow_sha`
- `verification`: `regression`, `migration`, `postgresql`, `postgresql_passed`, `container_smoke`, `image_security`
- `image`: `platform`, `archive`, `archive_sha256`, `config_digest`, `manifest_digest`

위 값 자체는 승인 근거가 아니다. 공식 실행의 이벤트/경로/SHA/시도/성공 여부, Artifact ID/출처/ZIP Digest, PR 병합 및 독립 승인 기록을 GitHub API와 대조해야 한다. 승인된 Workflow SHA는 관리자 검토를 거쳐 `TRUSTED_RELEASE_WORKFLOW_SHA`에 등록할 예정이며, 변수 자체는 독립 승인 증명이 아니다. 최신 시도와 현재 main HEAD를 확인한다. 현재 검증기는 정확한 Base·Head를 부모로 갖는 일반 Merge Commit만 지원하며 Squash/Rebase는 차단한다. 압축 ZIP/OCI Archive 4GiB 및 전체 레이어 압축 해제 합계 16GiB는 초기 안전 한도다. 실제 이미지 크기와 Runner 자원에 맞춰 연동 승인 시 재검토한다. 이미지나 증빙 만료·누락 시 발행을 보류한다. 이미 병합된 PR의 Artifact 재생성은 #125의 별도 승인된 재검증 경로가 확정되기 전까지 지원한다고 가정하지 않는다.

## 4. IAM 구성안 (적용 금지: 별도 승인 전)

`deploy/iam/ecr-publish-policy.json`은 발행 전용 Role의 **권한 정책 초안**이며 Trust Policy가 아니다. ECR 인증 토큰은 리소스 수준 지정이 불가능해 `*`를 사용하고, 모든 ECR API는 서울 리전, 나머지 권한은 `toi-be` ARN으로 제한한다. `sts:GetCallerIdentity`는 별도의 Allow 권한이 필요하지 않으므로 추가하지 않는다.

Push 레이어 업로드, Manifest 등록/확인, 필요한 대상 레이어 조회 및 저장소 설정 조회만 허용한다. SSM, EC2, IAM 변경, ECR 삭제, 저장소 생성/설정 변경, 스캔 시작/설정 및 스캔 결과 조회 권한은 포함하지 않는다. 최초 스캔 상태는 운영자 권한으로 별도 조회한다. 실제 Registry 동작과 IAM 검증 후 불필요한 권한은 제거한다.

기존 Provider를 재사용하는 예정 Trust Policy (문서용이며 미적용):

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

기존 Repository Variables `AWS_REGION`, `AWS_ACCOUNT_ID`, `ECR_REPOSITORY`, `AWS_ROLE_ARN`은 유지한다. 발행 전용 `ECR_PUBLISH_ROLE_ARN` 및 `TRUSTED_RELEASE_WORKFLOW_SHA`는 연동 승인 후 구성한다. `AWS_ROLE_ARN`을 신규 Role로 덮어쓰지 않는다. Access Key, Secret Key, ECR 로그인 토큰을 저장소 변수나 Artifact에 저장하지 않는다.

## 5. 실행 절차

### 현재: 로컬 단위 테스트

```powershell
python -m unittest discover -s .github/tests -p test_ecr_image_publish.py -v
```

표준 라이브러리 테스트만 실행하며 AWS 또는 GitHub 실제 호출은 하지 않는다.

### 향후: 공식 연동 완료 이후에만

1. #125 공식 결과, 검증 이미지 Artifact, 독립 리뷰 및 관리자 승인, 정확한 PR 병합 결과를 확인한다.
2. 보호된 main의 `ECR Image Publish`를 관리자가 수동 실행한다. 입력은 `release_run_id`, `release_run_attempt`, `release_pr_number` 세 가지다. 태그는 실행의 `github.sha`를 사용한다.
3. Validate Job은 OIDC 없이 검증한다. 독립 승인 계약 검증 및 이미지 전달·재검증까지 모두 성공한 경우에만 Publish Job을 실행하도록 추후 연결한다.
4. Publish Job은 신뢰된 발행 코드만 실행한다. 검증된 Archive와 Digest를 다시 확인한 후 전용 Role로 OIDC 인증하고 계정/Role/리전/저장소 불변성을 확인한다. 앱 코드 실행·빌드·DB 접근은 금지한다.
5. 기존 SHA 태그가 없다면 Skopeo `copy --preserve-digests`로 동일 이미지 발행을 한 번 시도한다. Digest 변경이 필요한 경우 실패한다. 동일 SHA/Digest가 존재하면 발행 생략, 다른 Digest이면 중단한다.
6. Push 응답 이후 ECR의 등록 Digest를 확인하고 실제 승인 및 원격 확인 증빙을 저장한다. 이후 자동 EC2 배포는 하지 않는다.

현재 Workflow는 위 운영 절차를 수행하지 못하며 강제 차단이 정상이다. 승인 없이 잠금 해제하거나 발행 함수를 실제 실행기에 연결하지 않는다.

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

현재 테스트용 `image-publish-evidence.simulation.json`은 `simulation=true`, `publication_authorized=false`, `deployable=false`를 기록한다. 실제 배포 증빙으로 사용하지 않는다.

운영용 `image-publish-evidence.json`은 승인 계약 및 실제 ECR 등록 확인이 모두 검증된 후에만 생성하는 별도 경로로 연결한다. Source SHA/Tree, PR Base/Head, Release Run ID/Attempt/Workflow SHA, Archive/Config/Manifest Digest, ECR 계정/리전/태그/Digest, Publish Run ID/Attempt/Workflow SHA, UTC 확인 시각과 결과를 기록한다. 후속 SSM에서는 증빙의 출처 및 승인과 실제 ECR 상태를 별도로 검증하고 `repository@sha256:...`로 배포한다. 단순히 JSON의 성공 표시를 믿지 않는다. 실제 운영용 스키마 및 SSM 소비 계약은 후속 연동에서 확정한다.

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

Phase 8 기본 실행 기준: 89개 수집, 88개 통과, 실제 Registry 선택 테스트 1개 건너뜀. Phase 9 검토 후에는 96개 수집, 95개 통과, 선택 테스트 1개 건너뜀을 기준으로 한다. 신규 통합 테스트는 아래를 확인한다.

- ZIP의 출처 및 OCI 검증과 루프백 HTTP 이미지 전송, 등록 Manifest·Config·Layer 바이트 확인, 모의 증빙 생성
- 동일 SHA/Digest 재실행 시 등록 상태 재조회 및 Push 생략
- 동일 SHA/다른 Digest 충돌 시 기존 바이트 보존 및 신규 증빙 미생성
- 이미지 등록 직후 응답 유실 시 상태 재조회 및 재전송 방지
- 변조된 Archive의 전송 차단 및 증빙 미생성

모의 증빙은 여전히 `publication_authorized=false`, `deployable=false`이며 운영용 증빙을 생성하지 않는다. 위 결과는 공식 승인 검증 및 실제 GitHub Artifact 다운로드 결과를 대체하지 않는다.

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

Phase 8 작성 환경에서는 도구 부재로 선택 테스트를 실행하지 못했다. 2026-11-25 사용자 로컬 Docker Desktop (Linux/amd64, Ubuntu 24.04 테스트 컨테이너)에서 `TOI_TEST_LOCAL_REGISTRY=1`로 실행하여 96개 전체 통과 (0.717초, 실패 및 Skip 0개)를 확인했다. 임시 Registry는 종료 및 정리했다. 실제 AWS ECR 및 IAM 검증은 별도다.

## 10. 전체 코드 검토 (Phase 9)

기존 다섯 파일의 발행 경계, 승인 및 Artifact 출처 검증, OCI 무결성, 모의 발행 멱등성, IAM 권한 범위를 검토했다. 다음 보완은 기존 발행 정책을 변경하지 않는다.

- JSON 표준에 없는 NaN 및 Infinity를 증빙과 GitHub API 응답에서 거부한다.
- 파일 해시 및 레이어 압축 해제 크기를 제한한다. 전체 레이어 압축 해제 합계는 초기 16GiB로 제한한다.
- 이미지 검증 직후 최신 Release 실행과 보호된 main HEAD를 재조회해 검증 도중 변경된 경우 차단한다. 실제 발행 활성화 시 승인 및 실행 상태를 발행 직전에도 확인해야 한다.
- 다운로드 대상의 배타적 파일 생성에 실패했을 때 다른 실행이 만든 파일을 삭제하지 않는다.
- 신규 회귀 테스트 7개를 추가했다. Python 테스트 96개 실행(95개 성공, 1개 건너뜀), Workflow YAML 구조, Bash 문법 및 IAM JSON 문법 검사를 수행했다.

현재의 독립 리뷰 관찰 결과와 Repository Variable은 공식 관리자 승인 증명이 아니며, 검증 JSON의 성공 표시 및 82개 통과 수치만으로 실제 테스트 수행을 입증하지 않는다. CLI 읽기 전용 검증 성공(종료 코드 0)도 발행 승인과 다르다. Workflow의 승인 Gate는 실패하고 Publish Job은 강제로 비활성화된다. 발행 전용 Role은 아직 생성하지 않았다. 실제 GitHub Actions/API Artifact 검증은 미완료다. 실제 Registry 선택 테스트 및 actionlint 재검사 결과는 아래에 기록한다. 현 단계는 독립 리뷰를 위한 Draft PR 준비이며 운영 발행 완료가 아니다.

### 추가 검증 기록 (2026-11-25)

- 사용자 로컬 실제 Registry 통합 테스트: 96개 전체 통과, 실패 및 Skip 0개.
- actionlint 1.7.12 최초 검사: 의도적인 Publish Job `if: ${{ false }}` 조건에 대한 `if-cond` 진단 1건, 종료 코드 1.
- 해당 메시지만 제외한 재검사: 종료 코드 0. Workflow의 강제 차단 조건은 유지한다.
- 실제 발행 활성화 시 예외 옵션 없이 actionlint를 재실행한다.

```powershell
docker run --rm `
  --mount "type=bind,source=$((Get-Location).Path),target=/repo,readonly" `
  --workdir /repo `
  rhysd/actionlint:1.7.12 `
  -color `
  -ignore '^constant expression .false. in condition[.] remove the if: section$' `
  .github/workflows/ecr-image-publish.yml
```

## 11. 남은 완료 조건

- #125 공식 실행 및 승인 증빙 계약, OCI 전달 방식에 대한 별도 승인
- 실제 GitHub Artifact 통합 검증 (격리 Registry 검증은 완료)
- 신뢰된 Workflow SHA 및 관리자 실행 승인 근거 확정
- 발행 전용 IAM Role 별도 승인 및 실제 권한 검증
- 검증 Job에서 발행 Job으로 안전하게 검증 결과와 이미지 전달 및 재검증
- 실제 발행 실행기, 운영용 증빙 생성·업로드 및 OIDC 활성화에 대한 독립 리뷰
- 보호된 main에 필요한 소스 병합 후 최초 승인 이미지 발행, ECR Digest 및 자동 스캔 확인
- SSM 배포는 별도 Issue에서 진행

## Reference

- #125, #136, PR #127
- `docs/deployment/ARCHITECTURE.md`, `EC2_SETUP.md`, `RUNBOOK.md`, `ROLLBACK.md`
- [AWS ECR Push IAM permissions](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html)
- [AWS ECR Service Authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecr.html)
- [AWS STS GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html)
