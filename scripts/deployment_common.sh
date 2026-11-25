#!/bin/bash -p
set -euo pipefail
umask 077
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C TZ=UTC
unset CDPATH GLOBIGNORE BASH_ENV ENV LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT deadline
readonly ROOT=/opt/toi PROJECT=toi-be DEPLOY=/opt/toi/deploy STATE_DIR=/opt/toi/deployment
readonly STATE=$STATE_DIR/state.json LOCK=$STATE_DIR/deploy.lock LOG_DIR=/opt/toi/logs
readonly COMPOSE=$DEPLOY/docker-compose.yml COMPOSE_ENV=$DEPLOY/.env API_ENV=$DEPLOY/.env.api
readonly SOCKET=unix:///var/run/docker.sock REGION=ap-northeast-2 REPOSITORY=toi-be
readonly DB_VOLUME=toi-be_pgdata UPLOAD_VOLUME=toi-be_uploads
readonly RUN_STARTED=$SECONDS
readonly DIGEST_PATTERN='^sha256:[0-9a-f]{64}$'
log_bytes=0 log_file='' auth_dir='' registry='' image=''
current=- previous=- status=ready db_container='' api_container='' started=''
failure_reason='' record_on_error=''

log() {
    local line
    printf -v line '%(%FT%TZ)T %s' -1 "$1"
    printf '%s\n' "$line" >&2
    if [[ -n $log_file ]] && (( log_bytes + ${#line} + 1 <= 4096 )); then
        printf '%s\n' "$line" >>"$log_file"
        log_bytes=$((log_bytes + ${#line} + 1))
    fi
}

die() {
    log "$2"
    if [[ -n $record_on_error ]]; then
        document state-write "$STATE" "$record_on_error" "$current" "$previous" "$operation" \
            "$requested" "$started" failed none "preflight-$1" 2>/dev/null || {
            log 'state: cannot record preflight outcome'
            exit 9
        }
    fi
    exit "$1"
}

cleanup() {
    [[ -n $auth_dir ]] || return 0
    if ! /usr/bin/rm -f -- "$auth_dir/config.json" || ! /usr/bin/rmdir -- "$auth_dir"; then
        log 'ECR credential cleanup failed'
        return 1
    fi
    auth_dir=''
}

interrupted() {
    log 'Interrupted. An active attempt requires operator reconciliation.'
    exit 9
}

secure() {
    local path=$1 kind=$2 attrs owner group mode links
    [[ ! -L $path ]] || return 1
    case "$kind" in
        dir|private-dir) [[ -d $path ]] || return 1 ;;
        file|private-file) [[ -f $path ]] || return 1 ;;
        *) return 1 ;;
    esac
    attrs=$(/usr/bin/stat -c '%u %g %a %h' -- "$path") || return 1
    read -r owner group mode links <<<"$attrs"
    [[ $owner == 0 && $group == 0 && $mode =~ ^[0-7]{3,4}$ ]] || return 1
    (( (8#$mode & 8#7022) == 0 )) || return 1
    case "$kind" in
        private-dir) (( (8#$mode & 8#077) == 0 )) ;;
        private-file) [[ $links == 1 ]] && (( (8#$mode & 8#177) == 0 )) ;;
        file) [[ $links == 1 ]] ;;
    esac
}

clean() {
    /usr/bin/env -i PATH="$PATH" LC_ALL=C TZ=UTC HOME=/root "$@"
}

bounded() {
    local seconds=$1
    shift
    if [[ -n ${deadline:-} ]]; then
        local remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || return 124
        if (( remaining < seconds )); then seconds=$remaining; fi
    fi
    clean /usr/bin/timeout --kill-after=3 "$seconds" "$@"
}

docker_run() {
    local seconds=$1
    shift
    bounded "$seconds" /usr/bin/docker --config "${auth_dir:-$STATE_DIR}" --host "$SOCKET" "$@"
}

compose() {
    local seconds=$1
    shift
    bounded "$seconds" /usr/bin/env API_IMAGE="$image" API_ENV_FILE="$API_ENV" API_PORT=8000 \
        /usr/bin/docker --config "$STATE_DIR" --host "$SOCKET" compose \
        --project-name "$PROJECT" --project-directory "$DEPLOY" \
        --env-file "$COMPOSE_ENV" --file "$COMPOSE" "$@"
}

# Keep JSON handling in one Python entry point. fd 3 carries data that must
# not be copied to command arguments, log files or command output.
document() {
    bounded 15 /usr/bin/python3 -I - "$@" <<'PY'
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote, urlsplit

def pairs(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj

def read(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=pairs)

def input_document():
    with os.fdopen(3, encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=pairs)

def require(condition):
    if not condition:
        raise ValueError("validation failed")

def digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r"sha256:[a-f0-9]{64}", value))

def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def timestamp_valid(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return False
    datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return True

def check_state(state):
    require(set(state) == {"version", "status", "current", "previous", "attempt"})
    require(type(state["version"]) is int and state["version"] == 1)
    require(state["status"] in ("ready", "in_progress", "blocked"))
    for field in ("current", "previous"):
        require(state[field] is None or digest(state[field]))
    if state["previous"] is not None:
        require(state["current"] is not None and state["current"] != state["previous"])
    attempt = state["attempt"]
    require(set(attempt) == {"operation", "digest", "started_at", "finished_at", "result", "recovery", "reason"})
    require(attempt["operation"] in ("deploy", "rollback", "reconcile") and digest(attempt["digest"]))
    require(timestamp_valid(attempt["started_at"]))
    require(attempt["result"] in ("running", "success", "unchanged", "failed"))
    require(attempt["recovery"] in ("none", "rolled_back", "not_available", "failed"))
    require(isinstance(attempt["reason"], str) and re.fullmatch(r"[a-z0-9-]{1,64}", attempt["reason"]))
    if state["status"] == "in_progress":
        require(attempt["result"] == "running" and attempt["finished_at"] is None)
    else:
        require(attempt["result"] != "running" and timestamp_valid(attempt["finished_at"]))
    if state["status"] == "blocked":
        require(attempt["result"] == "failed")

def write_state(path, fields):
    status, current, previous, operation, attempted, started, result, recovery, reason = fields
    state = {"version": 1, "status": status, "current": None if current == "-" else current,
             "previous": None if previous == "-" else previous,
             "attempt": {"operation": operation, "digest": attempted, "started_at": started,
                         "finished_at": None if status == "in_progress" else timestamp(),
                         "result": result, "recovery": recovery, "reason": reason}}
    check_state(state)
    path = Path(path)
    fd, staging = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)

def compose_model(image):
    model = input_document()
    require(model["name"] == "toi-be" and set(model["services"]) == {"api", "db"})
    api = model["services"]["api"]
    db = model["services"]["db"]
    require(api["image"] == image and api["platform"] == "linux/amd64")
    require(db["image"] == "postgres:16.16" and not db.get("ports"))
    ports = api["ports"]
    require(len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1")
    require(str(ports[0]["published"]) == "8000" and ports[0]["target"] == 8000 and ports[0]["protocol"] == "tcp")
    for service, volume, target in ((api, "uploads", "/srv/uploads"), (db, "pgdata", "/var/lib/postgresql/data")):
        mounts = service["volumes"]
        require(len(mounts) == 1 and mounts[0]["type"] == "volume" and mounts[0]["source"] == volume)
        require(mounts[0]["target"] == target and not mounts[0].get("read_only"))
        require(model["volumes"][volume]["name"] == "toi-be_" + volume)
        require(not model["volumes"][volume].get("external") and not model["volumes"][volume].get("driver_opts"))
        require(not any(service.get(key) for key in ("privileged", "network_mode", "devices", "cap_add")))
        require(set(service["networks"]) == {"default"} and bool(service["healthcheck"]["test"]))
    require(model["networks"]["default"]["name"] == "toi-be_default")
    require(not model["networks"]["default"].get("external"))
    require(not api.get("command") and not api.get("entrypoint"))
    env = api["environment"]
    require(env["AUTO_CREATE_SCHEMA"] == "false" and env["UPLOAD_DIR"] == "/srv/uploads/dogs")
    require("POSTGRES_PASSWORD" not in env)
    dsn = urlsplit(env["DATABASE_URL"])
    require(dsn.scheme in ("postgresql", "postgresql+psycopg2") and dsn.hostname == "db")
    require(dsn.port in (None, 5432) and not dsn.query and not dsn.fragment and bool(dsn.username))
    require(unquote(dsn.path) == "/" + db["environment"]["POSTGRES_DB"])
    db_user = db["environment"]["POSTGRES_USER"]
    require(re.fullmatch(r"[A-Za-z0-9_]{1,63}", db_user) and unquote(dsn.username) != db_user)
    db_name = db["environment"]["POSTGRES_DB"]
    require(re.fullmatch(r"[A-Za-z0-9_]{1,63}", db_name))
    print(db_name, hashlib.sha256(env["DATABASE_URL"].encode()).hexdigest())

def running_api(expected_dsn):
    runtime = input_document()
    env = dict(entry.split("=", 1) for entry in runtime["Config"]["Env"])
    require(hashlib.sha256(env["DATABASE_URL"].encode()).hexdigest() == expected_dsn)
    require(env["AUTO_CREATE_SCHEMA"] == "false" and env["UPLOAD_DIR"] == "/srv/uploads/dogs")
    require(set(runtime["NetworkSettings"]["Networks"]) == {"toi-be_default"})
    require(runtime["NetworkSettings"]["Ports"] == {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]})

def schema_evidence(record_path, approval_path, image_digest, expected_database):
    observed = input_document()
    require(observed["version"] == 160016)
    identity = observed["identity"]
    catalog = observed["catalog"]
    require(isinstance(identity, dict) and isinstance(catalog, list) and bool(catalog))
    require(identity["database"] == expected_database)
    require(all(isinstance(row, list) for row in catalog))
    canonical = sorted(json.dumps(row, ensure_ascii=True, separators=(",", ":")) for row in catalog)
    fingerprint = hashlib.sha256(("\n".join(canonical) + "\n").encode("ascii")).hexdigest()
    record = read(record_path)
    proof = read(approval_path)
    require(set(record) == {"db_identity", "algorithm", "fingerprint", "migrations"})
    require(set(proof) == set(record) | {"image_digest", "approval"})
    require(record["algorithm"] == "pg16-public-v1" and record["db_identity"] == identity)
    require(record["fingerprint"] == fingerprint and proof["image_digest"] == image_digest)
    require(all(proof[field] == record[field] for field in record))
    require(isinstance(record["migrations"], list))
    names = set()
    for migration in record["migrations"]:
        require(set(migration) == {"file", "sha256"})
        require(bool(re.fullmatch(r"[0-9]{3}_[a-zA-Z0-9_]+\.sql", migration["file"])))
        require(migration["file"] not in names and bool(re.fullmatch(r"[a-f0-9]{64}", migration["sha256"])))
        names.add(migration["file"])
    approval = proof["approval"]
    require(set(approval) == {"approved_by", "approved_at", "reference", "verification"})
    require(timestamp_valid(approval["approved_at"]) and approval["approved_at"] <= timestamp())
    for field in ("approved_by", "reference", "verification"):
        require(isinstance(approval[field], str) and 0 < len(approval[field].strip()) <= 512)

def prune(path, current_log):
    import stat
    import time
    entries = []
    with os.scandir(path) as directory:
        for entry in directory:
            if not re.fullmatch(r"toi-deploy-\d{8}T\d{6}Z\.[a-zA-Z0-9]{8}", entry.name):
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_gid == 0 and info.st_nlink == 1:
                entries.append((info.st_mtime, entry.path))
    entries.sort(reverse=True)
    for position, (modified, name) in enumerate(entries):
        if name != current_log and (position >= 20 or modified < time.time() - 14 * 86400):
            os.unlink(name)

try:
    action, *args = sys.argv[1:]
    if action == "state-read":
        state = read(args[0])
        check_state(state)
        print(state["status"], state["current"] or "-", state["previous"] or "-", state["attempt"]["result"])
    elif action == "state-write":
        write_state(args[0], args[1:])
    elif action == "compose":
        compose_model(args[0])
    elif action == "running-api":
        running_api(args[0])
    elif action == "proof":
        schema_evidence(*args)
    elif action == "prune":
        prune(*args)
    else:
        raise ValueError("unknown operation")
except (ValueError, KeyError, AttributeError, TypeError, OSError):
    sys.exit(1)
PY
}

catalog_snapshot() {
    # Capture the expected database on the same container and a local socket.
    # shellcheck disable=SC2016
    compose 20 exec -T db /bin/sh -ec '
        [ "$POSTGRES_DB" = "$1" ] || exit 1
        case "$POSTGRES_USER" in ""|*[!A-Za-z0-9_]*) exit 1 ;; esac
        exec /usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/psql \
            -X --no-password --no-align --tuples-only --quiet --set ON_ERROR_STOP=1 \
            --host /var/run/postgresql --port 5432 --username "$POSTGRES_USER" --dbname "$1"
    ' catalog "$expected_database" <<'SQL'
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '12s';
SET LOCAL search_path = pg_catalog;
WITH objects(item) AS (
  SELECT jsonb_build_array('table', c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
     pg_get_partkeydef(c.oid), CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound,c.oid) END)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S')
  UNION ALL
  SELECT jsonb_build_array('column', c.relname, a.attnum, a.attname,
     format_type(a.atttypid,a.atttypmod), a.attnotnull,a.attidentity,a.attgenerated,
     pg_get_expr(d.adbin,d.adrelid), col.collname)
    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
    LEFT JOIN pg_collation col ON col.oid=a.attcollation
    WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')
      AND a.attnum>0 AND NOT a.attisdropped
  UNION ALL
  SELECT jsonb_build_array('constraint', c.relname, k.conname, k.convalidated, pg_get_constraintdef(k.oid))
    FROM pg_constraint k JOIN pg_namespace n ON n.oid=k.connamespace
    LEFT JOIN pg_class c ON c.oid=k.conrelid WHERE n.nspname='public'
  UNION ALL
  SELECT jsonb_build_array('index', c.relname, i.indisvalid, pg_get_indexdef(c.oid))
    FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
  UNION ALL
  SELECT jsonb_build_array('enum', t.typname, e.enumsortorder, e.enumlabel)
    FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid
    JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public'
  UNION ALL
  SELECT jsonb_build_array('sequence', c.relname, s.seqtypid::regtype::text, s.seqstart, s.seqincrement,
      s.seqmin, s.seqmax, s.seqcache, s.seqcycle, owned.relname, a.attname)
    FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid
      AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') AND d.refobjsubid>0
    LEFT JOIN pg_class owned ON owned.oid=d.refobjid
    LEFT JOIN pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
    WHERE n.nspname='public'
  UNION ALL
  SELECT jsonb_build_array('routine', p.proname, pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid))
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.prokind IN ('f','p','w')
  UNION ALL
  SELECT jsonb_build_array('view', c.relname, pg_get_viewdef(c.oid), c.reloptions)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('v','m')
  UNION ALL
  SELECT jsonb_build_array('trigger', c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid))
    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
  UNION ALL
  SELECT jsonb_build_array('policy', p.tablename, p.policyname, p.permissive, p.roles,p.cmd,p.qual,p.with_check)
    FROM pg_policies p WHERE p.schemaname='public'
  UNION ALL
  SELECT jsonb_build_array('extension', e.extname, n.nspname, e.extversion)
    FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
)
SELECT jsonb_build_object('version', current_setting('server_version_num')::int,
    'identity', jsonb_build_object('system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
                                'database', current_database(), 'database_oid',
                                (SELECT oid::text FROM pg_database WHERE datname=current_database())),
    'catalog', (SELECT jsonb_agg(item) FROM objects));
COMMIT;
SQL
}

approval_file() {
    printf '%s/approvals/%s.json' "$DEPLOY" "${1#sha256:}"
}

verify_proof() {
    local proof
    proof=$(approval_file "$1")
    secure "$proof" private-file &&
        document proof "$DEPLOY/schema-record.json" "$proof" "$1" "$expected_database" 3<<<"$catalog"
}

load_state() {
    if [[ -e $STATE || -L $STATE ]]; then
        local record
        secure "$STATE" private-file &&
            record=$(document state-read "$STATE" 2>/dev/null) || return 1
        read -r status current previous last_result <<<"$record"
    fi
}

write_state() {
    document state-write "$STATE" "$@" 2>/dev/null || die 9 'state: atomic write failed; reconcile before retrying'
}

service_id() {
    # Includes stopped containers so an unrecorded API cannot be silently adopted.
    docker_run 12 ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=$1" --filter label=com.docker.compose.oneoff=False 2>/dev/null
}

check_app_container() {
    local id=$1 expected=$2 health=$3 row actual running health_status runtime
    row=$(docker_run 12 inspect --format '{{.Image}} {{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null) || return 1
    read -r actual running health_status <<<"$row"
    [[ $actual == "$expected" && $running == true ]] || return 1
    [[ $(docker_run 12 inspect --format '{{range .Mounts}}{{if eq .Destination "/srv/uploads"}}{{.Type}} {{.Name}}{{end}}{{end}}' "$id" 2>/dev/null) == "volume $UPLOAD_VOLUME" ]] || return 1
    if [[ $health == health ]]; then
        [[ $health_status == healthy ]] &&
            runtime=$(docker_run 12 inspect --format '{{json .}}' "$id" 2>/dev/null) &&
            document running-api "$expected_dsn" 3<<<"$runtime" 2>/dev/null
    fi
}

http_health() {
    bounded "$1" /usr/bin/curl -q --noproxy '*' --connect-timeout 2 --max-time "$1" \
        --fail --silent --output /dev/null http://127.0.0.1:8000/db-test
}

image_id() {
    local ref="$registry/$REPOSITORY@$1" info id os architecture
    info=$(docker_run 12 image inspect --format '{{.Id}} {{.Os}} {{.Architecture}}' "$ref" 2>/dev/null) || return 1
    read -r id os architecture <<<"$info"
    [[ $id =~ $DIGEST_PATTERN && $os == linux && $architecture == amd64 ]] || return 1
    printf '%s\n' "$id"
}

ensure_volume() {
    [[ $(docker_run 12 volume inspect --format '{{index .Labels "com.docker.compose.project"}} {{index .Labels "com.docker.compose.volume"}}' "$1" 2>/dev/null) == "$PROJECT $2" ]]
}

assert_db() {
    [[ $(service_id db) == "$db_container" ]] &&
        [[ $(docker_run 12 inspect --format '{{.Config.Image}} {{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{len .HostConfig.PortBindings}} {{len .NetworkSettings.Networks}} {{if index .NetworkSettings.Networks "toi-be_default"}}true{{end}}' "$db_container" 2>/dev/null) == 'postgres:16.16 true healthy 0 1 true' ]] &&
        [[ $(docker_run 12 inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Type}} {{.Name}}{{end}}{{end}}' "$db_container" 2>/dev/null) == "volume $DB_VOLUME" ]]
}

pull_image() {
    auth_dir=$(/usr/bin/mktemp -d "$STATE_DIR/.docker-auth.XXXXXXXX") || die 6 'auth: unable to allocate private Docker configuration'
    # A registry entry selects the file store even if a native helper is installed.
    printf '{"auths":{"%s":{}}}\n' "$registry" >"$auth_dir/config.json" ||
        die 6 'auth: unable to initialize private Docker configuration'
    # The child shell receives only positional arguments and explicit settings.
    # shellcheck disable=SC2016
    if ! bounded 30 /usr/bin/env AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
        AWS_EC2_METADATA_DISABLED=false AWS_MAX_ATTEMPTS=2 AWS_PAGER= \
        /bin/bash -p -c '
        set -o pipefail
        aws ecr get-login-password --region "$1" |
            /usr/bin/docker --config "$2" --host "$3" login --username AWS --password-stdin "$4"
    ' login "$REGION" "$auth_dir" "$SOCKET" "$registry" >/dev/null 2>&1; then
        die 6 'auth: ECR authentication failed or timed out'
    fi
    docker_run 300 pull --platform linux/amd64 "$image" >/dev/null 2>&1 ||
        die 6 'pull: ECR digest could not be fetched'
    cleanup || die 6 'pull: could not clear temporary authentication'
}

apply_image() {
    local digest=$1 expected=$2 old now
    local deadline=$((SECONDS + 170))
    image="$registry/$REPOSITORY@$digest"
    failure_reason=api-stop
    old=$(service_id api) || return 1
    if [[ -n $old ]]; then
        [[ $old =~ ^[0-9a-f]{12,64}$ ]] || return 1
        compose 35 stop --timeout 30 api >/dev/null 2>&1 || return 1
    fi
    failure_reason=db-state
    assert_db && ensure_volume "$UPLOAD_VOLUME" uploads || return 1
    failure_reason=api-start
    local health_deadline=$((SECONDS + 120))
    if (( health_deadline < deadline )); then deadline=$health_deadline; fi
    compose 120 up --detach --no-deps --no-build --pull never --force-recreate \
        --wait --wait-timeout 110 api >/dev/null 2>&1 || return 1
    failure_reason=api-image-or-health
    now=$(service_id api) || return 1
    [[ $now =~ ^[0-9a-f]{12,64}$ ]] && check_app_container "$now" "$expected" health || return 1
    failure_reason=api-db-test
    http_health 5 || return 1
    failure_reason=db-state
    assert_db && ensure_volume "$UPLOAD_VOLUME" uploads || return 1
    failure_reason=schema-changed
    catalog=$(catalog_snapshot 2>/dev/null) && verify_proof "$digest" 2>/dev/null
}

main() {
    local operation=${1:-} requested=${2:-} model expected_current='' target
    local target_previous='' prior_for_rollback=- original_failure catalog expected_database expected_dsn
    local last_result=none
    [[ $EUID == 0 ]] || die 3 'root is required'
    case "$operation" in
        deploy) [[ $# == 2 && $requested =~ $DIGEST_PATTERN ]] || die 2 'invalid ImageDigest' ;;
        rollback|status) [[ $# == 1 ]] || die 2 'unexpected argument' ;;
        *) die 2 'unsupported operation' ;;
    esac
    for path in / /opt "$ROOT" "$ROOT/scripts"; do secure "$path" dir || die 3 'unsafe base path'; done
    for path in "$DEPLOY" "$STATE_DIR" "$LOG_DIR"; do secure "$path" private-dir || die 3 'unsafe private path'; done
    for path in "$ROOT/scripts/deploy.sh" "$ROOT/scripts/rollback.sh" "$ROOT/scripts/deployment_common.sh"; do
        secure "$path" file && [[ -x $path ]] || die 3 'unsafe script installation'
    done
    [[ -S /var/run/docker.sock && ! -L /var/run/docker.sock ]] ||
        die 3 'trusted local Docker socket missing'
    [[ ! -e $STATE_DIR/config.json && ! -L $STATE_DIR/config.json ]] ||
        die 3 'unexpected Docker client configuration'
    secure "$LOCK" private-file || die 3 'missing or unsafe deployment lock'
    if [[ $operation == status ]]; then
        local deadline=$((SECONDS + 50))
        exec 9<"$LOCK"
        /usr/bin/flock -s -n 9 || { printf '%s\n' 'deployment=busy'; return 0; }
        load_state || die 5 'state: invalid or unsafe'
        bounded 12 /usr/bin/sha256sum "$ROOT/scripts/deploy.sh" "$ROOT/scripts/rollback.sh" "$ROOT/scripts/deployment_common.sh"
        printf 'status=%s current=%s previous=%s last_result=%s\n' "$status" "$current" "$previous" "$last_result"
        local service id
        for service in api db; do
            id=$(service_id "$service") || die 3 'Docker status unavailable'
            if [[ -z $id ]]; then
                printf '%s absent\n' "$service"
            elif [[ $id =~ ^[0-9a-f]{12,64}$ ]]; then
                printf '%s ' "$service"
                docker_run 8 inspect --format '{{.Id}} {{.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" ||
                    die 3 'container status unavailable'
            else
                die 5 'unexpected service inventory'
            fi
        done
        return
    fi

    exec 9<"$LOCK"
    /usr/bin/flock -n 9 || die 4 'lock: another deployment operation owns the lock'
    local stamp
    TZ=UTC printf -v stamp '%(%Y%m%dT%H%M%SZ)T' -1
    log_file=$(/usr/bin/mktemp "$LOG_DIR/toi-deploy-$stamp.XXXXXXXX") || die 3 'log: cannot create private log'
    document prune "$LOG_DIR" "$log_file" || die 3 'log: retention check failed'
    trap 'cleanup || :' EXIT
    trap interrupted INT TERM HUP
    load_state && [[ $status == ready ]] || die 5 'state: invalid, interrupted or blocked; reconcile before retrying'
    if [[ $operation == rollback ]]; then
        [[ $previous != - ]] || die 5 'rollback: no previous verified image'
        requested=$previous
    fi
    started=$(bounded 5 /usr/bin/date -u +%FT%TZ) || die 3 'clock: unavailable'
    record_on_error=ready
    if ! secure "$DEPLOY/registry" private-file || ! read -r registry <"$DEPLOY/registry"; then
        die 3 'registry: missing trusted configuration'
    fi
    [[ $registry =~ ^[0-9]{12}\.dkr\.ecr\.ap-northeast-2\.amazonaws\.com$ ]] &&
        [[ $(<"$DEPLOY/registry") == "$registry" ]] || die 3 'registry: invalid fixed registry'
    for path in "$COMPOSE" "$COMPOSE_ENV" "$API_ENV" "$DEPLOY/schema-record.json"; do
        secure "$path" private-file || die 3 'configuration: missing or unsafe file'
    done
    secure "$DEPLOY/approvals" private-dir || die 3 'approval directory: unsafe'
    image="$registry/$REPOSITORY@$requested"
    if ! model=$(compose 15 config --format json 2>/dev/null) ||
        ! read -r expected_database expected_dsn < <(document compose "$image" 3<<<"$model" 2>/dev/null) ||
        [[ -z $expected_database || ! $expected_dsn =~ ^[0-9a-f]{64}$ ]]; then
        die 3 'configuration: invalid Compose model'
    fi
    unset model
    if ! db_container=$(service_id db) || [[ ! $db_container =~ ^[0-9a-f]{12,64}$ ]] ||
        ! assert_db || ! ensure_volume "$DB_VOLUME" pgdata || ! ensure_volume "$UPLOAD_VOLUME" uploads; then
        die 7 'db: prepared healthy database and project volumes required'
    fi
    api_container=$(service_id api) || die 5 'state: cannot inventory API'
    if [[ $current != - ]]; then
        if ! expected_current=$(image_id "$current") || [[ ! $api_container =~ ^[0-9a-f]{12,64}$ ]] ||
            ! check_app_container "$api_container" "$expected_current" identity; then
            record_on_error=blocked
            die 5 'state: current image and running API disagree'
        fi
    elif [[ -n $api_container ]]; then
        record_on_error=blocked
        die 5 'state: API exists without a current deployment record'
    fi
    if [[ $previous != - ]]; then
        if ! target_previous=$(image_id "$previous"); then
            record_on_error=blocked
            die 5 'state: previous image is missing locally'
        fi
    fi

    if ! catalog=$(catalog_snapshot 2>/dev/null) || ! verify_proof "$requested" 2>/dev/null; then
        die 7 'schema: missing, invalid or incompatible target evidence'
    fi
    if [[ $current != - && $operation == deploy ]]; then
        verify_proof "$current" 2>/dev/null || die 7 'schema: current image does not have compatible rollback evidence'
    fi
    if [[ $current == "$requested" ]]; then
        if ! check_app_container "$api_container" "$expected_current" health || ! http_health 5; then
            die 7 'unchanged: recorded image is not healthy or runtime settings differ'
        fi
        record_on_error=''
        write_state ready "$current" "$previous" "$operation" "$requested" "$started" unchanged none verified
        log "unchanged: $requested"
        return
    fi
    if [[ $operation == deploy ]]; then
        pull_image
        target=$(image_id "$requested") || die 6 'pull: image identity or platform verification failed'
    else
        target=$target_previous
        if verify_proof "$current" 2>/dev/null &&
            check_app_container "$api_container" "$expected_current" health && http_health 5; then
            prior_for_rollback=$current
        fi
    fi
    # Check again after a potentially long pull, before any API mutation.
    if ! assert_db || ! catalog=$(catalog_snapshot 2>/dev/null) || ! verify_proof "$requested" 2>/dev/null; then
        die 7 'schema: evidence or database changed before activation'
    fi
    if [[ $current != - && $operation == deploy ]]; then
        verify_proof "$current" 2>/dev/null || die 7 'schema: rollback evidence changed before activation'
    fi
    (( SECONDS - RUN_STARTED <= 400 )) || die 7 'preflight: insufficient time for activation and rollback'
    record_on_error=''
    write_state in_progress "$current" "$previous" "$operation" "$requested" "$started" running none activating
    log "$operation: activating $requested"
    if apply_image "$requested" "$target"; then
        local retained=$current
        if [[ $operation == rollback ]]; then retained=$prior_for_rollback; fi
        write_state ready "$requested" "$retained" "$operation" "$requested" "$started" success none verified
        log "$operation: verified $requested"
        return
    fi
    original_failure=$failure_reason
    log "$operation: failed at $original_failure"
    if [[ $operation == deploy && $current != - ]] && assert_db &&
        catalog=$(catalog_snapshot 2>/dev/null) && verify_proof "$current" 2>/dev/null &&
        apply_image "$current" "$expected_current"; then
        write_state ready "$current" "$previous" deploy "$requested" "$started" failed rolled_back "$original_failure"
        die 8 'deploy: failed; previous healthy service restored'
    fi
    local recovery=failed
    if [[ $operation == deploy && $current == - ]]; then recovery=not_available; fi
    # Best-effort stop of only the project API; block even if that stop fails.
    if ! compose 35 stop --timeout 30 api >/dev/null 2>&1; then log 'recovery: API stop did not complete'; fi
    write_state blocked "$current" "$previous" "$operation" "$requested" "$started" failed "$recovery" "$original_failure"
    die 9 "recovery: $recovery; operator reconciliation is required"
}

main "$@"
