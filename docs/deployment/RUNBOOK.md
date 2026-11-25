# 백엔드 컨테이너 실행과 검증

Docker의 Linux 엔진과 Compose 플러그인을 사용한다. 명령은 저장소 루트의 Bash에서 실행한다. Windows에서는 Git Bash와 Linux 컨테이너 모드를 사용한다.

## 격리 검증

```bash
bash scripts/verify_container.sh
```

스크립트는 로컬 Docker 엔진에 임시 Compose 프로젝트, 합성 자격 증명과 새 볼륨 두 개를 만든다. Compose 설정과 API 이미지 빌드를 확인하고, 빈 DB 한 개에만 기존 SQLAlchemy 모델로 스키마를 만든다. API 이미지의 플랫폼, Python 버전, 런타임 설정 및 자격 증명 파일 경로, 서비스 UID, 루프백 바인딩과 PostgreSQL 버전을 확인한다. 합성 DB 행을 쓰고 기존 업로드 서비스로 PNG를 저장한 뒤 HTTP로 내려받는다. 두 컨테이너를 정지하고 시작한 뒤, 다시 강제 재생성해 행과 이미지 바이트를 대조한다. 종료 시 이 검증 프로젝트의 컨테이너, 볼륨, API 이미지 태그와 임시 환경 파일을 정리한다.

## 로컬 Compose 실행

```bash
test -e deploy/.env || cp deploy/.env.example deploy/.env
docker compose --project-name toi-local --env-file deploy/.env config --quiet
docker compose --project-name toi-local --env-file deploy/.env build api
docker compose --project-name toi-local --env-file deploy/.env up -d --wait db
```

새 DB에서 API까지 확인할 때는 위 격리 검증 스크립트를 사용한다. 로컬 스택을 계속 사용할 때는 같은 프로젝트의 DB에 검증된 스키마가 준비된 뒤 API를 기동한다.

```bash
docker compose --project-name toi-local --env-file deploy/.env up -d --wait --no-build
docker compose --project-name toi-local --env-file deploy/.env ps
curl --fail http://127.0.0.1:8000/db-test
```

파일을 수정할 때는 `deploy/.env`를 사용한다. 포트 충돌 시 `API_PORT`를 바꾸고 curl URL에도 새 포트를 지정한다. Mock SMS 검증에는 합성 `PHONE_VERIFICATION_HMAC_KEY`가 필요하다. Mock 위치 조회는 격리 DB에 활성 지역 코드 `11680`을 준비한 뒤 실행한다. [SMS 환경변수](../../.env.example), [위치 연동 조건](../issues/46/HANDOFF.md)

격리 검증 스크립트는 자신이 만든 프로젝트의 새 DB에만 자동 생성을 사용한다. 실제 DB의 초기화, 기존 스키마 갱신, 통합 마이그레이션은 후속 단계에서 별도 검증한다. 빈 DB에 과거 SQL을 일괄 적용하지 않는다. [마이그레이션 절차](../migrations/README.md)

## 정지와 상태 확인

```bash
docker compose --project-name toi-local --env-file deploy/.env stop
docker compose --project-name toi-local --env-file deploy/.env start --wait
docker compose --project-name toi-local --env-file deploy/.env logs --tail 50 api db
```

`down`으로 컨테이너와 네트워크를 제거해도 다음에 같은 프로젝트 이름을 쓰면 볼륨을 재사용한다. `down --volumes`는 PostgreSQL 데이터와 업로드 이미지를 삭제하므로 검증 스크립트가 생성한 일회용 프로젝트 정리에만 사용한다. API가 `unhealthy`이면 `/db-test`, DB의 상태, DB 접속 설정과 스키마를 확인한다. 환경 파일을 수정한 뒤에는 `up -d --wait --no-build`로 설정을 다시 읽는다. `POSTGRES_*` 설정은 PostgreSQL이 새 볼륨을 초기화할 때 사용된다.

## ECR 이미지로 실행

승인된 digest가 준비되면 EC2의 `/opt/toi/scripts/deploy.sh` 또는 SSM의 `ToI-DeployBackend`를 실행한다. SSM에는 승인된 `ImageDigest` 값 하나를 넘긴다. 설치, 호환성 근거 및 IAM 전환은 [EC2 설치](EC2_SETUP.md), 실패와 재조정은 [롤백 절차](ROLLBACK.md)를 따른다.

```bash
sudo /opt/toi/scripts/deploy.sh "$approved_digest"
sudo /opt/toi/scripts/deployment_common.sh status
```

SSM에서는 `ToI-DeploymentStatus`를 사용한다. 파라미터 없이 설치 스크립트 SHA-256, 기록된 현재 및 이전 digest와 마지막 결과, API 및 DB 컨테이너 ID와 상태를 확인한다. 작업 중에는 `deployment=busy`가 표시된다. 상태 조회는 DB와 AWS를 호출하지 않는다.

| 종료 코드 | 의미 |
| --- | --- |
| 0 | 새 digest 검증 성공 또는 같은 digest 유지 성공 |
| 2 | 입력 형식 또는 인자 개수 오류 |
| 3 | 사용자, 설치 파일, Compose 설정 또는 로컬 Docker 오류 |
| 4 | 다른 배포 또는 롤백이 잠금을 보유함 |
| 5 | 상태 파일, 실제 컨테이너 또는 이전 이미지가 기록과 불일치하거나 재조정 필요 |
| 6 | ECR 인증, 정확한 digest Pull 또는 이미지 플랫폼 확인 실패 |
| 7 | DB 준비 상태, 호환성 근거 또는 동일 digest 헬스체크 확인 실패 |
| 8 | 배포 실패 후 직전 정상 서비스로 복구 성공 |
| 9 | 최초 배포 실패, 복구 실패, 상태 기록 오류 또는 작업 중단 |

ECR 인증은 30초, Pull은 300초, API 정지는 컨테이너 유예 시간 30초로 제한한다. 기동과 확인은 최대 120초, 정지를 포함한 교체 시도는 최대 170초다. 전체 배포의 사전 점검이 400초를 넘으면 복구 시간을 확보하기 위해 API 교체를 시작하지 않는다. SSM 제한 시간은 배포 900초, 상태 조회 60초다.

`/opt/toi/logs`의 시도별 root 전용 로그는 정해진 짧은 진행 문구만 기록하고 4 KiB보다 짧게 유지한다. 성공 후 운영 확인은 SSM 결과와 상태 조회를 함께 사용한다. 로그는 다음 시도에서 14일과 최대 20개 기준으로 정리된다. Docker의 컨테이너 로그는 Compose의 순환 정책을 사용한다. 오류 조사 중에는 환경 파일, 전체 Compose 설정, 인증 토큰을 출력하거나 공유하지 않는다.

## 배포 자동화 검증

```bash
bash scripts/verify_deployment.sh --static
shellcheck scripts/deploy.sh scripts/rollback.sh scripts/deployment_common.sh scripts/verify_deployment.sh
```

전체 검증은 Docker Engine, Compose 플러그인, Buildx, Bash, Python 3, curl, GNU coreutils, flock이 있는 일회용 Linux VM에서 실행한다. Docker 리소스가 없는 VM에 `postgres:16.16`, `python:3.12.10-slim-bookworm`, `registry:2`를 먼저 내려받고, 테스트 레지스트리 주소 `111111111111.dkr.ecr.ap-northeast-2.amazonaws.com`만 Docker의 HTTP 허용 목록에 넣는다. 모든 업링크를 끊고 IPv4와 IPv6 기본 경로가 없음을 확인한 VM에서만 `/run/toi-verify-isolated`에 `toi-disposable-vm`을 기록한 뒤 root로 `bash scripts/verify_deployment.sh`를 실행한다.

검증은 합성 DB와 실제 Docker 볼륨, 로컬 레지스트리, 모의 AWS CLI와 테스트용 API 이미지를 사용한다. API 교체 전후 DB 행과 업로드 파일을 비교하고 실제 Docker의 컨테이너 상태와 HTTP 헬스체크를 검사한다. STEP 3의 실제 FastAPI 검증에는 `scripts/verify_container.sh`를 사용한다. 검증 결과를 수집한 뒤 VM을 폐기한다.

## 병합 전 검증

선행 기능 PR을 먼저 병합하고 최신 `dev`를 반영한다. 격리 컨테이너 검증과 기존 Dev quality gate를 다시 실행하고 동일한 변경 내용의 독립 리뷰 결과를 확인한다. [기존 검증 및 독립 검토 절차](../dev-automation/RUNBOOK.md)
