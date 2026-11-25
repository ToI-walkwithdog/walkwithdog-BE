#!/bin/bash
set -euo pipefail
umask 077
trap 'printf "Verification failed at line %s\n" "$LINENO" >&2' ERR
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

for script in deploy rollback deployment_common verify_deployment; do
    bash -n "scripts/$script.sh"
done
python3 - <<'PY'
import json
from pathlib import Path
import re
import subprocess

base = Path("deploy/ssm")
deploy = json.loads((base / "ToI-DeployBackend.json").read_text())
status = json.loads((base / "ToI-DeploymentStatus.json").read_text())
for doc, limit in ((deploy, "900"), (status, "60")):
    assert doc["schemaVersion"] == "2.2"
    step, = doc["mainSteps"]
    assert step["action"] == "aws:runShellScript"
    assert step["precondition"] == {"StringEquals": ["platformType", "Linux"]}
    assert step["inputs"]["timeoutSeconds"] == limit
    assert "{{" not in json.dumps(doc)
parameter, = deploy["parameters"].values()
assert list(deploy["parameters"]) == ["ImageDigest"]
assert parameter["type"] == "String" and parameter["interpolationType"] == "ENV_VAR"
assert re.fullmatch(parameter["allowedPattern"], "sha256:" + "a" * 64)
for invalid in ("sha256:" + "a"*63, "sha256:" + "A"*64, "sha256:" + "0"*64 + "\n", "$(id)"):
    assert not re.fullmatch(parameter["allowedPattern"], invalid)
commands = deploy["mainSteps"][0]["inputs"]["runCommand"]
assert commands[-1] == 'exec /opt/toi/scripts/deploy.sh "$SSM_ImageDigest"'
assert subprocess.run(["/bin/sh", "-c", "\n".join(commands)], env={}, capture_output=True).returncode == 2
assert not status.get("parameters")
assert status["mainSteps"][0]["inputs"]["runCommand"] == ["exec /opt/toi/scripts/deployment_common.sh status"]
PY
if [[ ${1:-} == --static && $# == 1 ]]; then
    printf '%s\n' 'PASS Bash syntax and SSM contracts'
    exit 0
fi

# Preload public images and disconnect all uplinks in a disposable VM.
[[ $# == 0 && $EUID == 0 && $(</run/toi-verify-isolated) == toi-disposable-vm &&
   ! -e /opt/toi && ! -L /opt/toi && -z $(ip -4 route show default) &&
   -z $(ip -6 route show default) ]] || {
    printf '%s\n' 'Use a fresh, network-disconnected verification VM or --static.' >&2
    exit 1
}
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG COMPOSE_FILE COMPOSE_PROJECT_NAME
d() { /usr/bin/docker --host unix:///var/run/docker.sock "$@"; }
[[ -z $(d ps -aq) && -z $(d volume ls -q) ]]
for tag in registry:2 postgres:16.16 python:3.12.10-slim-bookworm; do d image inspect "$tag" >/dev/null; done
[[ ! -e /usr/local/bin/aws && ! -L /usr/local/bin/aws ]]
readonly root=/opt/toi conf=/opt/toi/deploy journal=/opt/toi/deployment/state.json
readonly registry=111111111111.dkr.ecr.ap-northeast-2.amazonaws.com
readonly deploy=/opt/toi/scripts/deploy.sh rollback=/opt/toi/scripts/rollback.sh
readonly canary=TOI_SYNTHETIC_PASSWORD_65AD token=TOI_SYNTHETIC_TOKEN_44CE
readonly evidence=/tmp/toi-deployment-verification
install -d -m 0700 "$root" "$root/scripts" "$conf" "$conf/approvals" "$root/logs" "$root/deployment" "$evidence"
install -m 0755 scripts/deploy.sh scripts/rollback.sh scripts/deployment_common.sh "$root/scripts/"
install -m 0600 docker-compose.yml "$conf/"
printf '%s\n' "$registry" >"$conf/registry"
printf 'POSTGRES_USER=toi\nPOSTGRES_DB=toi_fixture\nPOSTGRES_PASSWORD=%s\n' "$canary" >"$conf/.env"
printf 'DATABASE_URL=postgresql://app:%s@db:5432/toi_fixture\n' "$canary" >"$conf/.env.api"
touch "$root/deployment/deploy.lock"
printf '127.0.0.1 %s\n' "$registry" >>/etc/hosts
cat >/usr/local/bin/aws <<'AWS'
#!/bin/bash
set -eu
[[ $* == 'ecr get-login-password --region ap-northeast-2' ]]
[[ $AWS_CONFIG_FILE == /dev/null && $AWS_SHARED_CREDENTIALS_FILE == /dev/null ]]
[[ $AWS_EC2_METADATA_DISABLED == false && $PATH == /usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin ]]
for key in AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN \
    AWS_ENDPOINT_URL_ECR AWS_EC2_METADATA_SERVICE_ENDPOINT AWS_WEB_IDENTITY_TOKEN_FILE \
    AWS_CONTAINER_CREDENTIALS_FULL_URI BASH_ENV HTTP_PROXY; do
    [[ ! -v $key ]]
done
printf '%s\n' aws >>/tmp/toi-deployment-verification/aws-calls
[[ ! -e /tmp/toi-deployment-verification/fail-auth ]]
printf '%s\n' TOI_SYNTHETIC_TOKEN_44CE
AWS
chmod 0755 /usr/local/bin/aws
d run --detach --pull never --name toi-fixture-registry -p 127.0.0.1:80:5000 registry:2 >/dev/null
for ((attempt = 0; attempt < 30; attempt++)); do
    if curl -fsS --max-time 2 "http://$registry/v2/" >/dev/null 2>&1; then break; fi
    sleep 1
done
curl -fsS --max-time 2 "http://$registry/v2/" >/dev/null

mkdir "$evidence/build"
cat >"$evidence/build/Dockerfile" <<'DOCKERFILE'
FROM python:3.12.10-slim-bookworm
ARG VARIANT
LABEL toi.test.variant=$VARIANT
ENV FIXTURE_VARIANT=$VARIANT
COPY server.py /server.py
USER 10001:10001
CMD ["python", "/server.py"]
DOCKERFILE
cat >"$evidence/build/server.py" <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import time

variant = os.environ["FIXTURE_VARIANT"]
if variant == "slow":
    time.sleep(12)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if variant == "bad" or Path("/srv/uploads/dogs/bad-health").exists():
            self.send_error(503)
            return
        if self.path == "/db-test":
            try:
                with socket.create_connection(("db", 5432), timeout=2):
                    content = b"database available"
            except OSError:
                self.send_error(503)
                return
        elif self.path == "/fixture-upload":
            content = Path("/srv/uploads/dogs/probe.png").read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(content)

ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
PY

build() {
    local tag="$registry/toi-be:$1" manifest
    d build --pull=false --network=none --platform linux/amd64 \
        --build-arg "VARIANT=$1" --tag "$tag" "$evidence/build" >&2
    manifest=$(d push "$tag")
    printf '%s\n' "$manifest" | sed -n 's/.*digest: \(sha256:[a-f0-9]\{64\}\).*/\1/p'
}
first=$(build first)
second=$(build second)
broken=$(build bad)
slow=$(build slow)
missing="sha256:$(printf '%064d' 1)"
for digest in "$first" "$second" "$broken" "$slow"; do [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]]; done
c() {
    local ref=$1
    shift
    API_IMAGE="$registry/toi-be@$ref" API_ENV_FILE="$conf/.env.api" API_PORT=8000 \
        COMPOSE_ANSI=never COMPOSE_PROGRESS=plain d compose --project-name toi-be --project-directory "$conf" \
        --env-file "$conf/.env" --file "$conf/docker-compose.yml" "$@"
}
psql_fixture() {
    c "$first" exec -T db psql -X -qAt -U toi -d toi_fixture -v ON_ERROR_STOP=1 "$@"
}
c "$first" up -d --no-build --pull never --wait db
psql_fixture <<SQL
CREATE ROLE app LOGIN PASSWORD '$canary';
CREATE TABLE persistence_probe (id int PRIMARY KEY, content text NOT NULL);
INSERT INTO persistence_probe VALUES (1, 'db-marker');
GRANT SELECT ON persistence_probe TO app;
SQL
db_id=$(c "$first" ps -q db)
[[ -z $(d port "$db_id") ]]
d volume create --label com.docker.compose.project=toi-be --label com.docker.compose.volume=uploads toi-be_uploads >/dev/null
d volume create unrelated_fixture_volume >/dev/null
d run --rm --network none --volume toi-be_uploads:/srv/uploads python:3.12.10-slim-bookworm \
    sh -ec 'mkdir -p /srv/uploads/dogs; printf "%s" image-marker > /srv/uploads/dogs/probe.png; chown -R 10001:10001 /srv/uploads/dogs'
# The fixture acts as the separate approver and writes synthetic evidence.
sed -n '/^BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;$/,/^COMMIT;$/p' \
    scripts/deployment_common.sh | psql_fixture >"$evidence/catalog.json"
python3 - "$evidence/catalog.json" "$conf" "$first" "$second" "$broken" "$slow" "$missing" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

catalog = json.loads(Path(sys.argv[1]).read_text())
assert catalog["version"] == 160016
lines = sorted(json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in catalog["catalog"])
fingerprint = hashlib.sha256(("\n".join(lines) + "\n").encode("ascii")).hexdigest()
record = {"db_identity": catalog["identity"], "algorithm": "pg16-public-v1", "fingerprint": fingerprint,
          "migrations": [{"file": "001_fixture.sql", "sha256": "a" * 64}]}
directory = Path(sys.argv[2])
(directory / "schema-record.json").write_text(json.dumps(record))
for digest in sys.argv[3:]:
    proof = record | {"image_digest": digest, "approval": {"approved_by": "fixture",
                      "approved_at": "2000-01-01T00:00:00Z", "reference": "isolated-fixture", "verification": "catalog and test image checks"}}
    (directory / "approvals" / (digest.split(":")[1] + ".json")).write_text(json.dumps(proof))
PY
sha256sum "$conf/schema-record.json" "$conf"/approvals/*.json >"$evidence/proof-hashes"
approval() { printf '%s/approvals/%s.json\n' "$conf" "${1#sha256:}"; }
api_id() { c "$first" ps -q --all api; }
assert_state() {
    python3 - "$journal" "$@" <<'PY'
import json
from pathlib import Path
import sys

state = json.loads(Path(sys.argv[1]).read_text())
status, current, previous, result, recovery = sys.argv[2:]
assert (state["status"], state["current"] or "-", state["previous"] or "-",
        state["attempt"]["result"], state["attempt"]["recovery"]) == (status, current, previous, result, recovery), state
PY
}
expect() {
    local expected=$1 status=0
    shift
    "$@" >"$evidence/last.out" 2>&1 || status=$?
    cat "$evidence/last.out" >>"$evidence/attempts.out"
    if [[ $status != "$expected" ]]; then
        cat "$evidence/last.out" >&2
        printf 'Expected exit %s, got %s\n' "$expected" "$status" >&2
        return 1
    fi
}
preserved() {
    [[ $(c "$first" ps -q db) == "$db_id" ]]
    [[ $(psql_fixture -c 'SELECT content FROM persistence_probe WHERE id=1') == db-marker ]]
    [[ $(curl -fsS --max-time 5 http://127.0.0.1:8000/fixture-upload) == image-marker ]]
    d volume inspect toi-be_pgdata toi-be_uploads unrelated_fixture_volume >/dev/null
    sha256sum -c --quiet "$evidence/proof-hashes"
    [[ $(find "$conf/approvals" -type f | wc -l) == 5 ]]
}

reconcile() (
    flock -x -w 180 9
    local target=$1 previous=$2
    sha256sum -c --quiet "$evidence/proof-hashes"
    [[ $(c "$target" ps -q db) == "$db_id" ]]
    c "$target" up -d --no-deps --no-build --pull never --force-recreate --wait api >/dev/null
    [[ $(d inspect --format '{{.Image}} {{.State.Health.Status}}' "$(api_id)") == "$(d image inspect --format '{{.Id}}' "$registry/toi-be@$target") healthy" ]]
    curl -fsS --max-time 5 http://127.0.0.1:8000/db-test >/dev/null
    cp -p "$journal" "$evidence/manual-$(date +%s).json"
    python3 - "$journal" "$target" "$previous" <<'PY'
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile

path, target, previous = sys.argv[1:]
path = Path(path)
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
new = {"version": 1, "status": "ready", "current": target, "previous": None if previous == "-" else previous,
       "attempt": {"operation": "reconcile", "digest": target, "started_at": now, "finished_at": now,
                   "result": "success", "recovery": "none", "reason": "operator-verified-fixture"}}
with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w") as stream:
    json.dump(new, stream)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(stream.name, path)
directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(directory)
os.close(directory)
PY
) 9<"$root/deployment/deploy.lock"

expect 2 "$deploy" 'sha256:ab;id'
expect 2 "$deploy" "$first" 'extra'
expect 2 "$rollback" 'extra'
expect 5 "$rollback"
mv "$conf/.env.api" "$conf/.env.api.saved"
expect 3 "$deploy" "$first"
ln -s "$conf/.env.api.saved" "$conf/.env.api"
expect 3 "$deploy" "$first"
rm -- "$conf/.env.api"
mv "$conf/.env.api.saved" "$conf/.env.api"
chmod 0644 "$conf/.env.api"
expect 3 "$deploy" "$first"
chmod 0600 "$conf/.env.api"
(
    flock -n 9
    expect 4 "$deploy" "$first"
    expect 4 "$rollback"
    expect 0 "$root/scripts/deployment_common.sh" status
    grep -qx 'deployment=busy' "$evidence/last.out"
) 9<"$root/deployment/deploy.lock"
touch "$evidence/fail-auth"
expect 6 "$deploy" "$first"
rm -- "$evidence/fail-auth"
expect 6 "$deploy" "$missing"
c "$first" stop db >/dev/null
expect 7 "$deploy" "$first"
c "$first" start --wait db
db_id=$(c "$first" ps -q db)
printf '%s\n' 'PASS invalid inputs, unsafe configuration, lock, authentication/Pull and DB readiness'

proof=$(approval "$first")
cp -p "$proof" "$evidence/approved"
mv "$proof" "$evidence/missing-proof"
expect 7 "$deploy" "$first"
mv "$evidence/missing-proof" "$proof"
for field in db_identity algorithm fingerprint migrations approval approval_date image_digest; do
    python3 - "$proof" "$evidence/approved" "$field" "$second" <<'PY'
import json
from pathlib import Path
import sys

proof, source, field, digest = sys.argv[1:]
record = json.loads(Path(source).read_text())
if field == "db_identity":
    record[field]["database_oid"] = "0"
elif field == "migrations":
    record[field][0]["sha256"] = "b" * 64
elif field == "approval":
    del record["approval"]["reference"]
elif field == "approval_date":
    record["approval"]["approved_at"] = "2000-99-99T00:00:00Z"
elif field == "image_digest":
    record[field] = digest
else:
    record[field] = "invalid"
Path(proof).write_text(json.dumps(record))
PY
    expect 7 "$deploy" "$first"
    [[ -z $(api_id) ]]
    assert_state ready - - failed none
done
cp -p "$evidence/approved" "$proof"
printf '%s\n' 'PASS all incompatible evidence rejected before API mutation'

expect 9 "$deploy" "$broken"
assert_state blocked - - failed not_available
expect 5 "$deploy" "$first"
# Archive the blocked first attempt under the operation lock after confirming
# that there is no last-known-good release or remaining API container.
(
    flock -x -n 9
    c "$broken" rm --force --stop api >/dev/null
    mv "$journal" "$evidence/first-failure.json"
) 9<"$root/deployment/deploy.lock"

mkdir "$evidence/untrusted"
printf '#!/bin/sh\nexit 99\n' >"$evidence/untrusted/aws"
chmod +x "$evidence/untrusted/aws"
printf 'touch %q\n' "$evidence/injected" >"$evidence/untrusted/bash-env"
expect 0 env PATH="$evidence/untrusted:$PATH" BASH_ENV="$evidence/untrusted/bash-env" \
    DOCKER_CONFIG="$evidence/untrusted" DOCKER_CONTEXT=foreign DOCKER_HOST=tcp://127.0.0.1:1 \
    COMPOSE_PROJECT_NAME=foreign COMPOSE_FILE=/invalid API_IMAGE=foreign API_ENV_FILE=/invalid \
    COMPOSE_ENV_FILES=/invalid API_PORT=9999 POSTGRES_DB=foreign AUTO_CREATE_SCHEMA=true \
    AWS_PROFILE=foreign AWS_DEFAULT_PROFILE=foreign AWS_ACCESS_KEY_ID=foreign AWS_SECRET_ACCESS_KEY=foreign \
    AWS_SESSION_TOKEN=foreign AWS_ENDPOINT_URL_ECR=http://127.0.0.1:1 \
    AWS_EC2_METADATA_SERVICE_ENDPOINT=http://127.0.0.1:1 AWS_WEB_IDENTITY_TOKEN_FILE=/invalid \
    AWS_CONTAINER_CREDENTIALS_FULL_URI=http://127.0.0.1:1 HTTP_PROXY=http://127.0.0.1:1 "$deploy" "$first"
[[ ! -e $evidence/injected ]]
assert_state ready "$first" - success none
preserved
before=$(api_id)
calls=$(wc -l <"$evidence/aws-calls")
expect 0 "$deploy" "$first"
assert_state ready "$first" - unchanged none
[[ $(api_id) == "$before" && $(wc -l <"$evidence/aws-calls") == "$calls" ]]
psql_fixture -c 'ALTER TABLE persistence_probe ADD COLUMN schema_drift int'
expect 7 "$deploy" "$first"
[[ $(api_id) == "$before" ]]
psql_fixture -c 'ALTER TABLE persistence_probe DROP COLUMN schema_drift'
c "$first" stop api >/dev/null
expect 5 "$deploy" "$first"
[[ $(api_id) == "$before" ]]
assert_state blocked "$first" - failed none
expect 5 "$deploy" "$first"
reconcile "$first" -
c "$second" up -d --no-deps --no-build --pull never --force-recreate --wait api
wrong=$(api_id)
expect 5 "$deploy" "$first"
[[ $(api_id) == "$wrong" ]]
assert_state blocked "$first" - failed none
expect 5 "$deploy" "$first"
reconcile "$first" -
c "$first" exec -T api sh -c 'touch /srv/uploads/dogs/bad-health'
expect 7 "$deploy" "$first"
c "$first" exec -T api sh -c 'rm /srv/uploads/dogs/bad-health'
c "$first" start --wait api >/dev/null

expect 0 "$deploy" "$second"
assert_state ready "$second" "$first" success none
preserved
expect 0 "$rollback"
assert_state ready "$first" "$second" success none
preserved
d image inspect "$registry/toi-be@$first" "$registry/toi-be@$second" >/dev/null
expect 8 "$deploy" "$broken"
assert_state ready "$first" "$second" failed rolled_back
preserved
printf '%s\n' 'PASS initial failure, environment isolation, no-op checks, image exchange, rollback and persistence'

wait_for_attempt() {
    local target=$1
    for ((attempt = 0; attempt < 200; attempt++)); do
        if [[ -s $journal ]] && python3 -c 'import json, sys; d=json.load(open(sys.argv[1])); sys.exit(not (d["status"] == "in_progress" and d["attempt"]["digest"] == sys.argv[2]))' \
            "$journal" "$target"; then return 0; fi
        sleep 0.2
    done
    return 1
}

"$deploy" "$broken" >"$evidence/recovery-failure.out" 2>&1 &
operation_pid=$!
wait_for_attempt "$broken"
mv "$proof" "$evidence/held-proof"
result=0
wait "$operation_pid" || result=$?
[[ $result == 9 ]]
assert_state blocked "$first" "$second" failed failed
mv "$evidence/held-proof" "$proof"
expect 5 "$deploy" "$second"


reconcile "$first" "$second"
old_inode=$(stat -c %i "$journal")
"$deploy" "$slow" >"$evidence/interrupted.out" 2>&1 &
operation_pid=$!
wait_for_attempt "$slow"
[[ $(stat -c %i "$journal") != "$old_inode" ]]
kill -KILL "$operation_pid"
wait "$operation_pid" || [[ $? == 137 ]]
(
    flock -x -w 180 9
    assert_state in_progress "$first" "$second" running none
) 9<"$root/deployment/deploy.lock"
expect 5 "$deploy" "$second"
expect 5 "$rollback"
reconcile "$first" "$second"
assert_state ready "$first" "$second" success none
expect 0 "$deploy" "$first"
preserved
expect 0 "$root/scripts/deployment_common.sh" status
grep -Fq "status=ready current=$first previous=$second last_result=unchanged" "$evidence/last.out"
[[ $(find "$root/deployment" -name '.docker-auth.*' -type d | wc -l) == 0 ]]
for digest in "$first" "$second" "$broken" "$slow"; do
    d image inspect "$registry/toi-be@$digest" >>"$evidence/images.out"
    d history --no-trunc "$registry/toi-be@$digest" >>"$evidence/images.out"
done
if grep -RqF -e "$canary" -e "$token" "$root/logs" "$evidence"/{attempts,images,recovery-failure,interrupted}.out; then
    printf '%s\n' 'Synthetic credential appeared in a log or image metadata.' >&2
    exit 1
fi
printf '%s\n' 'PASS rollback failure, forced termination, atomic state, manual reconciliation and credential canaries'
