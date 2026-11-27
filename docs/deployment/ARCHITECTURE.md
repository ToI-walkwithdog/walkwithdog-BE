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

로컬에서는 Compose가 이미지를 빌드한다. 배포 호스트에서는 같은 Compose 설정에 `API_IMAGE`로 ECR 이미지 URI를 지정하고 빌드를 끈다. 볼륨은 Compose 프로젝트 이름에 묶이므로 같은 호스트에서 서비스를 교체할 때 프로젝트 이름을 유지한다. EC2의 접근 경로, 이미지 발행, 실제 DB 초기화 및 통합 마이그레이션은 별도 작업에서 준비한다. [로컬 실행 절차](RUNBOOK.md)

## EC2 배포와 상태 전환

EC2는 서울 리전의 승인된 ECR 저장소 `toi-be`에서 `linux/amd64` 이미지를 digest로 가져온다. 배포 스크립트는 `toi-be` 프로젝트에서 DB와 두 볼륨을 확인한 후 API만 교체한다. DB 교체 없이 API Docker 헬스체크, 루프백 `/db-test`, 실제 이미지 ID, 승인된 DB 호환성 근거를 대조한다. 같은 digest를 요청하면 실행 중인 API와 설정을 다시 확인하고, 성공하면 기존 컨테이너를 유지한다.

| 상태 | 의미 |
| --- | --- |
| 상태 파일 없음 | 기존 API 컨테이너가 없는 호스트에서 첫 배포를 준비할 수 있다. |
| `ready` | 현재와 이전의 확인된 digest를 기록한다. 마지막 배포가 실패했어도 이전 API 복구가 확인되면 이 상태를 유지한다. |
| `in_progress` | 변경 직전에 기록한다. 이 상태가 남으면 운영자가 상태를 재조정할 때까지 자동 작업을 중단한다. |
| `blocked` | 최초 배포 실패 또는 복구 실패로 운영자 확인이 필요하다. |

기록은 `/opt/toi/deployment/state.json` 한 곳에 둔다. 형식과 기존 상태가 유효한 요청은 API 교체 전 점검에서 실패해도 시도 결과를 기록한다. 같은 디렉터리에서 임시 파일을 쓰고 파일 동기화, 원자적 교체, 디렉터리 동기화 순서로 저장한다. 배포와 롤백은 동일한 `flock`을 사용한다. 상태 조회는 공유 잠금을 사용하며 다른 작업이 실행 중이면 `deployment=busy`를 반환한다. Docker와 파일의 변경은 하나의 트랜잭션이 아니므로, 비정상 종료 뒤에는 [복구 절차](ROLLBACK.md)에 따라 실제 상태를 확인한다.

스키마 호환성 승인은 이미지별로 발급한다. 배포는 실제 DB의 읽기 전용 스냅샷과 운영자가 보관하는 적용 기록을 승인 근거에 대조한다. 릴리스 승인 기록과 실제 적용 기록은 별도 파일로 관리한다. [EC2 설치와 호환성 기록](EC2_SETUP.md)
