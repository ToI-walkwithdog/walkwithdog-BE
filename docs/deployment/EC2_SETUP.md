# EC2 배포 설치

## 사전 조건

- Linux `amd64` 호스트에서 Bash, GNU coreutils, `flock`, `curl`, Python 3, Docker Engine과 시스템 전역 Compose 플러그인, AWS CLI v2가 필요하다. Compose는 JSON 설정 출력과 `up --wait --wait-timeout --pull never`를 지원해야 한다.
- `/var/run/docker.sock`으로 로컬 Docker 엔진에 접속한다. Docker 소켓 접근자와 `/opt/toi` 및 도구 설치 경로에 쓸 수 있는 운영자는 root와 같은 신뢰 범위로 취급한다. Docker 데몬 설정, 시스템 전역 플러그인 경로, AWS CLI 실행 파일 및 실제 링크 대상까지 root만 수정할 수 있도록 관리한다.
- SSM Run Command에서 실행하려면 Linux용 Command Document 2.2와 SSM Agent 3.3.2746.0 이상이 필요하다. `ToI-DeployBackend`는 `ENV_VAR`로 전달된 digest만 받는다. 해당 값이 없으면 종료한다. [AWS의 파라미터 문제 해결](https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-troubleshooting.html)
- `toi-be` Compose 프로젝트의 PostgreSQL 16.16 컨테이너, `toi-be_pgdata`, `toi-be_uploads` 볼륨을 별도 변경 절차로 준비한다. 업로드 볼륨에는 UID 10001이 쓸 수 있는 `dogs` 디렉터리가 필요하다. 비어 있는 DB의 초기화, 통합 마이그레이션, 기존 API의 인수와 DB 복구는 승인된 작업으로 다룬다.
- 배포 또는 복구 중에는 동일 프로젝트에 대해 수동 Compose 작업, 스키마 변경, 접근권한 변경과 기록 교체를 멈춘다. 별도 운영 작업도 같은 배포 잠금으로 직렬화한다.

## 파일 설치

| 경로 | 소유자와 권한 | 내용 |
| --- | --- | --- |
| `/opt/toi`, `/opt/toi/scripts` | root:root, 0755 또는 0700 | 상위 경로는 root 소유이고 그룹이나 다른 사용자에게 쓰기 권한이 없어야 한다. 심볼릭 링크는 사용하지 않는다. |
| `/opt/toi/scripts/{deploy,rollback,deployment_common}.sh` | root:root, 0755 | 같은 승인 커밋의 파일을 함께 설치하고 SHA-256을 대조한다. |
| `/opt/toi/deploy`, `/opt/toi/deploy/approvals`, `/opt/toi/deployment`, `/opt/toi/logs` | root:root, 0700 | 비밀값과 운영 기록을 보관한다. |
| `/opt/toi/deploy/docker-compose.yml` | root:root, 0600 | 승인 커밋의 Compose 파일을 사용한다. |
| `/opt/toi/deploy/registry` | root:root, 0600 | 승인된 서울 리전 ECR의 `ACCOUNT_ID.dkr.ecr.ap-northeast-2.amazonaws.com` 호스트명 한 줄을 기록한다. |
| `/opt/toi/deploy/.env`, `/opt/toi/deploy/.env.api` | root:root, 0600 | DB 준비 설정과 API 접속 정보를 분리한다. |
| `/opt/toi/deploy/schema-record.json`, `/opt/toi/deploy/approvals/<digest-hex>.json` | root:root, 0600 | 별도 승인 절차에서 발급한 실제 DB 적용 기록과 이미지별 호환성 근거다. |
| `/opt/toi/deployment/deploy.lock` | root:root, 0600 | 설치 시 한 번 생성한 일반 파일이다. 실행 중이거나 다시 배포할 때 교체하지 않는다. |

## 환경변수와 비밀값 (#128, 16-3)

실제 EC2 설정과 DB 변경은 별도 승인 작업이다. 아래는 시연 서버용 준비 절차이며 실제 값이나 적용 증빙이 아니다.

- 로컬 uvicorn은 저장소 루트의 [`.env.example`](../../.env.example), 로컬 Compose는 [`deploy/.env.example`](../../deploy/.env.example)를 참고한다. EC2 API는 신규 [`deploy/.env.api.example`](../../deploy/.env.api.example)를 사용한다. 예제는 비밀값을 채우지 않으므로 그대로 기동하지 않는다.
- EC2 `.env`에는 기존 DB의 `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`만 기록한다. DB 이름은 영문자, 숫자, 밑줄로 구성한다. 기존 볼륨의 비밀번호를 env 파일 편집만으로 변경할 수 없다.
- EC2 `.env.api`의 `DATABASE_URL`은 같은 DB 이름과 `db:5432`를 사용하며 API 전용 계정으로 접속한다. URL의 자격 증명은 퍼센트 인코딩 규칙에 맞춘다. `POSTGRES_*`는 API 파일에 넣지 않는다. Compose가 `AUTO_CREATE_SCHEMA=false`, `UPLOAD_DIR=/srv/uploads/dogs`를 고정한다.
- `APP_ENV=development`, `SMS_PROVIDER=mock`, `SMS_MOCK_FIXED_CODE`(ASCII 숫자 6자리), `PHONE_VERIFICATION_HMAC_KEY`를 설정한다. 실제 SMS 공급자는 연동하지 않는다. 고정 OTP 값은 승인된 팀원에게 별도 비공개 채널로 전달한다. 합성 전화번호와 실제 사용자 데이터가 없는 시연 DB를 사용한다.
- #116은 공개 데이터 수집 키와 매칭 정책, #117은 위치 공급자 및 고지·동의와 외부 전송 승인, #118은 보호소 HMAC 키, 서약·개인정보·약관 문서 및 레벨 정책을 확정한다. `PRIVACY_REQUIRED_CONSENTS`의 빈 값은 필수 동의 없음이라는 의미이므로 미결정 상태에서 가입을 열지 않는다. `ACCOUNT_EXIT_RECORD_POLICY`는 미설정, `MATCHING_INCLUDE_DEMO=false`, Gemini는 미설정을 기본으로 한다.

### 비밀값 생성과 저장

- 승인된 EC2에서 root로 작업하고 `set +x`, `umask 077`을 적용한다. 실제 값은 세션 명령 인자, 화면, Session Manager 로그, 저장소, Issue, PR, 채팅에 남기지 않는다. 외부 API Key는 승인된 비공개 전달 경로로 받는다. Parameter Store 등 신규 인프라는 이 단계에서 구성하지 않는다.
- 실제 파일은 `/opt/toi/deploy/.env`와 `.env.api`를 root:root 0600, 상위 디렉터리를 root:root 0700으로 유지한다. 새 환경의 키를 준비할 때만 승인된 API 템플릿을 같은 디렉터리의 root 전용 0600 임시 파일 `.env.api.new`로 준비하고 두 HMAC 키의 빈 줄을 각각 독립적으로 채운다. 기존 활성 파일을 덮어쓰거나 중복 키를 추가하지 않는다.
- 다음 예시는 **새로 준비한 빈 키가 있는 임시 파일**에만 적용한다. 활성 파일에는 실행하지 않는다. 출력되는 비밀값은 없다.

```bash
set +x
umask 077
python3 - <<'PY'
from pathlib import Path
import secrets
import stat

path = Path('/opt/toi/deploy/.env.api.new')
info = path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
    raise SystemExit('unsafe staging file')
text = path.read_text(encoding='utf-8')
lines = text.splitlines()
for key in ('PHONE_VERIFICATION_HMAC_KEY', 'SHELTER_CODE_HMAC_KEY'):
    matches = [i for i, line in enumerate(lines) if line.startswith(key + '=')]
    if len(matches) != 1 or lines[matches[0]] != key + '=':
        raise SystemExit('missing, duplicate or nonempty key: ' + key)
    lines[matches[0]] = key + '=' + secrets.token_urlsafe(48)
path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY
```

- 두 HMAC 키는 각각 32자 이상이며 서로 달라야 한다. 환경마다 새 값을 생성한다. 준비 파일과 DB 파일은 16-10의 `scripts/check_runtime_env.py` 실제 배포 설정 검증 및 별도 운영 승인 후 설치한다. 설정 파일 검증은 DB 접속, 정책 문서 내용, 공급자 실제 호출 또는 서버 접근 제한을 대신하지 않는다. 임시 파일은 승인된 설치 또는 실패 정리 절차에서 처리한다.
- 전화번호 HMAC 키 교체는 기존 인증번호 digest 확인을 무효화하고 새 번호 HMAC을 사용하므로 기존 번호별 일일 카운터를 더 이상 참조하지 않는다(기존 행은 삭제되지 않음). 보호소 HMAC 키를 교체하면 기존 발급 코드의 digest 확인이 실패한다. 이미 확인된 증빙과 진행 중 예약까지 자동으로 모두 무효화한다고 가정하지 않는다. 실제 교체는 기존 요청 및 증빙의 만료·영향을 확인하고 별도 승인으로 진행한다.

스크립트는 `/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`으로 고정한 `PATH`와 로컬 Docker 소켓을 사용한다. Compose 프로젝트, 파일, env 파일, API 이미지와 루프백 포트는 스크립트에서 지정한다. 외부 Docker Context, Compose, 프록시, AWS 관련 환경변수는 실행 명령에 전달되지 않는다. EC2 IAM Role 자격 증명을 사용하며 AWS 프로필과 공유 자격 증명 파일은 읽지 않는다. 전용 임시 Docker 설정에 `--password-stdin`으로 로그인하고 Pull 직후 정리한다. 로그인이나 Pull에서 오류가 나면 종료 시에도 정리를 시도한다. 강제 종료 후 남은 `.docker-auth.*`는 배포 잠금을 획득하고 잔여 작업이 없는 상태에서 디렉터리의 소유자, 모드, 이름, 내용을 확인한 후 해당 디렉터리만 정리한다. Docker 인증 설정에 저장된 토큰은 출력하지 않는다.

## DB 적용 기록과 이미지별 승인

다음 네 항목이 적용 기록과 이미지별 승인에 모두 있어야 하며 두 파일의 값이 일치해야 한다.

| 필드 | 기록할 내용 |
| --- | --- |
| `db_identity` | PostgreSQL 시스템 식별자 `system_identifier`, DB 이름 `database`, DB OID `database_oid`를 문자열로 기록한다. |
| `algorithm` | `pg16-public-v1`을 기록한다. |
| `fingerprint` | 읽기 전용 카탈로그 스냅샷의 정규화 SHA-256을 소문자 16진수 64자로 기록한다. |
| `migrations` | 실제 적용 순서대로 배열을 쓰고, 각 원소에 전체 파일명 `file`과 원본 파일의 SHA-256인 `sha256`을 기록한다. |

승인 파일에는 `image_digest`와 `approval`을 추가한다. 파일명에는 digest의 `sha256:`을 제외한 64자를 사용한다. `approval`에는 `approved_by`, UTC 초 단위의 `approved_at` (`YYYY-MM-DDTHH:MM:SSZ`), 감사 가능한 승인 기록 `reference`와 이미지 및 DB 조합에서 수행한 테스트 자료 `verification`을 기록한다. 파일은 root가 보호하는 승인 채널로 전달하고 내용과 승인 기록을 함께 검토한다.

`pg16-public-v1`은 PostgreSQL 16.16의 읽기 전용 repeatable-read 스냅샷에서 `deployment_common.sh`의 `catalog_snapshot` SQL을 사용한다. `public`의 관계와 열 정의, 제약, 인덱스, enum, 시퀀스 정의, 함수, 뷰, 트리거, RLS 정책과 설치된 확장 버전을 행 단위 JSON 배열로 구성한다. 각 배열을 ASCII 이스케이프를 적용한 공백 없는 JSON으로 직렬화하고 문자열 사전순으로 정렬한다. 각 줄을 LF로 잇고 끝에도 LF를 추가한 바이트의 SHA-256이 지문이다. 릴리스 검증 시 DB 계정 권한, 애플리케이션 동작과 데이터 호환성도 별도로 확인한다.

발급자는 대상 DB에 실제 적용되었다는 작업 기록을 같은 리비전의 마이그레이션 manifest 및 원본 SQL 체크섬과 대조한다. DB의 적용 기록, 검증 결과 또는 승인 중 하나라도 확인할 수 없으면 배포 호환성을 승인하지 않는다. manifest만으로 적용 여부를 판정하거나, 배포 스크립트로 근거를 발급 및 갱신하지 않는다. 현재와 이전 버전의 호환성은 각각 검증한다. 알고리즘의 대상이나 직렬화 규칙이 바뀌면 새 버전의 지문을 정의하고 별도 승인 절차로 근거를 다시 발급한다.

## 향후 IAM 전환

1. EC2 인스턴스 프로필에 SSM 관리 통신을 위한 권한과 ECR Pull 전용 권한을 설정한다. ECR은 `ecr:GetAuthorizationToken`에 전체 리소스를 사용하고, `ecr:BatchCheckLayerAvailability`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchGetImage`는 대상 저장소로 제한한다. [ECR 권한](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelasticcontainerregistry.html)
2. SSM에 두 Command Document를 별도 승인 절차로 등록한다. 배포 호출 역할은 `ssm:SendCommand`를 승인한 EC2 인스턴스와 두 사용자 정의 문서로 제한한다. 조회 전용 역할은 상태 문서만 호출하도록 제한한다. 결과 확인에 필요한 `ssm:GetCommandInvocation`은 ARN 수준으로 제한할 수 없으므로 별도 권한 문장으로 관리한다. 문서 버전과 호출 역할의 신뢰 정책도 운영 변경 시 고정한다. [SSM 권한](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ssm.html)
3. 새 역할에서 배포와 상태 문서만 허용되고 일반 셸 문서, 다른 대상, 비승인 역할의 호출이 거부되는지 검증한다. 기존 AWS 연결 테스트가 사용하던 권한은 새 경로의 검증과 별도 변경 승인 후 제거한다.
