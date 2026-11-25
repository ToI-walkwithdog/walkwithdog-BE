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

배포 호스트의 환경 파일에 `API_IMAGE`로 대상 이미지의 전체 ECR URI를 넣는다. `API_ENV_FILE=./deploy/.env.api`를 지정하고 해당 파일에 앱 전용 DB 계정의 `DATABASE_URL`과 필요한 공급자 설정을 넣는다. DB 부트스트랩 자격 증명은 Compose 환경 파일에만 둔다. `APP_ENV=production`에서는 실제 연동이 승인된 공급자 설정을 주입한다. 실제 환경 파일은 접근을 제한하고 커밋하지 않는다. 지원할 DB 스키마와 계정이 준비되고 호스트가 이미지를 읽을 수 있는 상태에서 실행한다.

```bash
docker compose --project-name toi-be --env-file deploy/.env config --quiet
docker compose --project-name toi-be --env-file deploy/.env up -d --wait --no-build --pull always
```

기동 실패 시 서비스 상태를 확인한다. 스키마 호환성이 확인된 직전 이미지로 되돌릴 때는 같은 프로젝트에서 `API_IMAGE` 값을 직전 URI로 바꾸고 다시 기동한다. 데이터 볼륨을 유지한다.

## 병합 전 검증

선행 기능 PR을 먼저 병합하고 최신 `dev`를 반영한다. 격리 컨테이너 검증과 기존 Dev quality gate를 다시 실행하고 동일한 변경 내용의 독립 리뷰 결과를 확인한다. [기존 검증 및 독립 검토 절차](../dev-automation/RUNBOOK.md)
