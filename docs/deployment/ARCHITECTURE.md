# 백엔드 컨테이너 구성

Docker Compose는 FastAPI와 PostgreSQL을 같은 프로젝트 네트워크에서 실행한다. 호스트는 `127.0.0.1`의 API 포트(기본 8000)로 접속한다. PostgreSQL에는 호스트 포트를 할당하지 않는다. FastAPI의 `DATABASE_URL`은 Compose 네트워크의 `db:5432`를 사용한다.

| 구성 | 실행 조건 |
| --- | --- |
| FastAPI | Python 3.12.10, `linux/amd64`, UID/GID 10001, Uvicorn worker 1개, reload 비활성화, 컨테이너 포트 8000 |
| PostgreSQL | 16.16, `linux/amd64`, TCP `pg_isready` 통과 후 API 기동 |
| API 헬스체크 | 기존 `GET /db-test`로 API 응답과 `dogs` 테이블 조회를 확인한다. 빈 DB의 API는 이 검사를 통과할 수 없다. |
| 재시작과 로그 | 두 서비스에 `unless-stopped`를 적용한다. Docker local 로그 드라이버는 컨테이너마다 파일당 10MB, 최대 3개로 회전한다. 헬스체크 실패는 `unhealthy` 상태로 표시된다. |
| 데이터 | PostgreSQL은 `pgdata` 볼륨을 사용한다. 업로드는 `uploads` 볼륨의 `dogs/`에 저장한다. 이미지에 디렉터리와 소유권을 준비해 새 볼륨을 UID 10001로 쓸 수 있게 한다. |

[Dockerfile](../../Dockerfile)은 `requirements.txt`를 먼저 설치해 의존성 레이어를 재사용한다. 빌드 컨텍스트와 이미지의 애플리케이션 파일은 의존성 목록, Python 코드, 정책 문서로 제한한다. [배포 환경변수 템플릿](../../deploy/.env.example)으로 관리하는 실제 값은 컨테이너 생성 시 주입한다. 일반 API 서비스는 `AUTO_CREATE_SCHEMA=false`로 고정한다.

로컬에서는 Compose가 이미지를 빌드한다. 배포 호스트에서는 같은 Compose 설정에 `API_IMAGE`로 ECR 이미지 URI를 지정하고 빌드를 끈다. 볼륨은 Compose 프로젝트 이름에 묶이므로 같은 호스트에서 서비스를 교체할 때 프로젝트 이름을 유지한다. 배포 호스트의 접근 경로, ECR 인증과 이미지 발행, 실제 DB의 초기화 및 통합 마이그레이션은 후속 작업에서 준비한다. [로컬 실행 절차](RUNBOOK.md)
