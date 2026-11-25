# EC2 백엔드 롤백과 수동 복구

## 자동 복구와 운영자 롤백

새 API가 검증에 실패하면 배포 시작까지 현재로 기록된 로컬 이미지의 호환성과 실제 DB를 다시 확인한다. 이 이미지로 API를 복구하고 이미지 ID, Docker 헬스체크 및 `/db-test`가 일치하면 상태를 `ready`로 확정한다. 원래 배포는 코드 8로 실패하고 `attempt.recovery`에는 `rolled_back`을 남긴다.

`sudo /opt/toi/scripts/rollback.sh`는 `previous`에 기록된 digest로 API를 교체한다. 이전 이미지가 로컬에 있고 현재 DB에 대한 승인이 유효해야 한다. 롤백하기 전의 현재 버전이 건강하고 승인된 상태였다면 새 `previous`로 보존한다. 롤백 또는 복구에 실패하거나 첫 배포에 복구할 대상이 없으면 API 정지를 시도하고 `blocked`를 기록한다. API가 정지했는지 상태 조회와 Docker에서 확인한다. PostgreSQL 및 업로드 볼륨은 유지한다.

## 이미지 보존

현재와 이전 정상 이미지의 digest 및 로컬 이미지가 다른 작업에서 삭제되지 않도록 관리한다. Docker 전역 정리, 특히 모든 미사용 이미지 삭제는 중단된 호스트에서 필요한 롤백 이미지까지 제거할 수 있다. ECR의 Lifecycle Policy는 이미지 수, 경과 시간, 태그 조건 등에 따라 정상 이미지도 만료시키거나 아카이브할 수 있다. 배포에 고정한 digest도 보호 정책이 없다면 이후 재획득할 수 없게 된다. [ECR Lifecycle Policy](https://docs.aws.amazon.com/AmazonECR/latest/userguide/LifecyclePolicies.html)

정책 변경 전 EC2의 `state.json`에 기록된 두 digest를 확인한다. 두 digest가 로컬 `docker image inspect`로 조회되고 ECR 활성 저장소에서 digest로 조회되는지 확인한다. 수명 주기 정책 미리보기 결과에서 두 digest가 만료나 아카이브 대상으로 선택되는지도 확인한다. 태그를 사용해 보호하는 경우 예외에 해당하는 태그 조건을 미리보기로 검증한다. 미리보기와 현재 적용 정책이 서로 다르면 둘 다 확인한다. 보호가 확인된 다음에만 정책 변경을 진행한다.

## 수동 재조정

1. 신규 배포 호출을 중지시키고 `deployment_common.sh status`로 상태를 확인한다. 복구 과정 전체에서 `/opt/toi/deployment/deploy.lock`의 독점 잠금을 획득하고 유지한다. 동일 파일에서 잠금이 풀릴 때까지 기다린다. 신호 뒤에는 자식 프로세스와 Docker 작업까지 정리됐는지 확인한다.
2. 기존 `state.json`을 root 전용 위치에 복사하고 시도 digest, 상태, 현재 및 이전 digest를 사고 기록에 남긴다. 루프백 응답, API와 DB의 컨테이너 ID, 실제 이미지 ID, 두 볼륨, 승인 기록을 대조한다. 승인된 운영 절차로 대상 DB 식별자, 카탈로그 지문과 적용 체크섬을 확인한다.
3. 현재 DB와 호환성이 입증된 로컬 digest를 선택한다. API가 다른 이미지라면 아래 예시의 고정 설정으로 API만 다시 시작한다. 실패하거나 근거를 확보하지 못하면 차단 상태를 유지하고 별도로 복구를 승인받는다. DB 초기화, 복원 및 마이그레이션은 이 절차의 범위 밖이다.
4. 실제 이미지 ID와 승인 digest의 로컬 이미지 ID, API 실행 상태, Docker 헬스체크 및 `/db-test`, DB 컨테이너 ID, 볼륨과 DB 근거를 다시 확인한다. 확인한 운영자, 시각, 승인 기록, 사유와 전후 ID를 운영 기록에 남긴다.
5. 운영자가 기존 상태 파일과 같은 디렉터리에서 root 소유 0600인 새 파일을 작성한다. 상태 JSON은 `version: 1`, `status: "ready"`, `current`에 확인된 digest, `previous`에 로컬 보존 및 호환성을 확인한 다른 정상 digest 또는 `null`을 사용한다. `attempt`는 `operation: "reconcile"`, `digest`에 확인된 digest, `started_at`과 `finished_at`에 UTC 초 단위 시각, `result: "success"`, `recovery: "none"`, `reason: "operator-verified"`를 기록한다. 파일을 동기화한 뒤 원자적으로 `state.json`을 교체하고 디렉터리도 동기화한다. 검증 전에는 기존 파일을 보존한다.
6. 잠금을 해제한 후 상태 문서와 동일 digest 재요청으로 기록과 실제 실행이 일치하는지 확인한다. 첫 시도가 실패했고 복구할 기존 API가 없는 경우에는 운영 승인과 기록을 남기고 잠금 아래 실패한 API 컨테이너만 정리한다. API가 없음을 확인한 후 막힌 상태 파일을 감사용으로 보관하고 최초 시도를 다시 허용한다.

API가 필요한 경우 3단계에서 사용할 명령이다. root가 고정된 배포 경로, 승인된 레지스트리, digest, 잠금을 확인한 다음 실행한다.

```bash
/usr/bin/env -i PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LC_ALL=C TZ=UTC \
  API_IMAGE="${approved_registry}/toi-be@${approved_digest}" API_ENV_FILE=/opt/toi/deploy/.env.api API_PORT=8000 \
  /usr/bin/timeout --kill-after=3 120 /usr/bin/docker --config /opt/toi/deployment --host unix:///var/run/docker.sock compose \
  --project-name toi-be --project-directory /opt/toi/deploy \
  --env-file /opt/toi/deploy/.env --file /opt/toi/deploy/docker-compose.yml \
  up --detach --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 110 api
```
