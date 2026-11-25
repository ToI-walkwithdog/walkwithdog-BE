#!/usr/bin/env bash
set -eEuo pipefail
trap 'printf "Container verification failed at line %s.\n" "$LINENO" >&2' ERR

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
docker compose version >/dev/null
docker_host=$(docker context inspect --format '{{.Endpoints.docker.Host}}' "$(docker context show)")
if [[ -n ${DOCKER_HOST:-} && -z ${DOCKER_CONTEXT:-} ]]; then
    docker_host=$DOCKER_HOST
fi
case "$docker_host" in
    unix://*|npipe://*) ;;
    *) printf 'Use a local Docker context for this test.\n' >&2; exit 1 ;;
esac
[[ $(docker info --format '{{.OSType}}') == linux ]]

scratch=$(mktemp -d "${TMPDIR:-/tmp}/toi-container.XXXXXXXX")
suffix=$(printf '%s' "${scratch##*.}" | tr '[:upper:]' '[:lower:]')
project="toi-container-$suffix"

export API_IMAGE="$project:local"
export API_PORT=0
export POSTGRES_USER=toi_container
export POSTGRES_PASSWORD=synthetic_container_password
export POSTGRES_DB=toi_container_test
export API_ENV_FILE="$scratch/api.env"
if command -v cygpath >/dev/null 2>&1; then
    API_ENV_FILE=$(cygpath -am "$API_ENV_FILE")
fi

compose() {
    docker compose --project-name "$project" --project-directory "$repo_root" \
        --env-file "$API_ENV_FILE" --file "$repo_root/docker-compose.yml" "$@"
}

stack_started=false
image_built=false
cleanup() {
    local cleanup_status=0
    if "$stack_started"; then
        compose down --volumes || cleanup_status=1
    fi
    if "$image_built"; then
        docker image rm "$API_IMAGE" >/dev/null || cleanup_status=1
    fi
    rm -f -- "$scratch/api.env" "$scratch/original.png" "$scratch/current.png" || cleanup_status=1
    rmdir -- "$scratch" || cleanup_status=1
    return "$cleanup_status"
}
trap 'status=$?; cleanup || status=1; exit "$status"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cat > "$scratch/api.env" <<EOF
DATABASE_URL=postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/$POSTGRES_DB
APP_ENV=test
SMS_PROVIDER=mock
PHONE_VERIFICATION_HMAC_KEY=synthetic_phone_verification_key_for_containers
LOCATION_GEOCODING_PROVIDER=mock
LOCATION_EXTERNAL_TRANSFER_ENABLED=false
EOF

compose config --quiet
compose build api
image_built=true
[[ $(docker image inspect "$API_IMAGE" --format '{{.Os}}/{{.Architecture}}') == linux/amd64 ]]
docker run --rm -i --network none --entrypoint python "$API_IMAGE" -B - <<'PY'
import os
from pathlib import Path
import sys

assert sys.version_info[:3] == (3, 12, 10)
runtime_keys = {
    "DATABASE_URL", "POSTGRES_PASSWORD", "PUBLIC_API_KEY", "PUBLIC_IMPORT_KEY",
    "GEMINI_API_KEY", "PHONE_VERIFICATION_HMAC_KEY", "SHELTER_CODE_HMAC_KEY",
    "SOLAPI_API_KEY", "SOLAPI_API_SECRET", "KAKAO_REST_API_KEY", "APP_ENV",
}
assert runtime_keys.isdisjoint(os.environ), "Runtime settings found in image environment"
for path in Path("/srv").rglob("*"):
    name = path.name.lower()
    assert not (name.startswith(".") or name == "secrets" or path.suffix.lower() in {
        ".key", ".pem", ".p12", ".pfx",
    }), "Unexpected local or credential file in image application paths"
PY
printf 'Compose config, image platform, Python version and image secret checks passed.\n'

stack_started=true
compose up -d --wait --wait-timeout 120 db
# Initialize only the disposable database for this project with the current ORM models.
compose run --rm --no-deps -T -e AUTO_CREATE_SCHEMA=true api python -B -c 'import app.main'
compose up -d --wait --wait-timeout 120 --no-build
[[ $(compose exec -T api id -u) == 10001 ]]
[[ $(compose exec -T api printenv AUTO_CREATE_SCHEMA) == false ]]

db_sql() {
    compose exec -T db psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
}

check_ports() {
    [[ $(docker inspect --format '{{len .HostConfig.PortBindings}}' "$(compose ps -q db)") == 0 ]]
    api_address=$(compose port api 8000)
    [[ $api_address =~ ^127\.0\.0\.1:[0-9]+$ ]]
}
api_get() {
    curl --noproxy '*' --fail --silent --show-error --max-time 10 "http://$api_address$1" "${@:2}"
}

check_ports
api_get / >/dev/null
api_get /db-test >/dev/null
[[ $(db_sql -c 'SHOW server_version_num') == 160016 ]]
db_sql <<'SQL'
CREATE TABLE container_smoke (id integer PRIMARY KEY);
INSERT INTO container_smoke VALUES (1);
SQL

upload_path=$(compose exec -T api python -B - <<'PY'
from base64 import b64decode
from io import BytesIO

from fastapi import UploadFile
from app.services.dog_service import save_dog_image

image = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGP4DwABAQEAsTj2FAAAAABJRU5ErkJggg==")
print(save_dog_image(UploadFile(file=BytesIO(image), filename="test.png")))
PY
)
[[ $upload_path =~ ^/media/dogs/[0-9a-f-]+\.png$ ]]
api_get "$upload_path" --output "$scratch/original.png"
printf 'Isolated schema, API, non-root user, port bindings and data writes passed.\n'

check_data() {
    check_ports
    api_get /db-test >/dev/null
    [[ $(db_sql -c 'SELECT id FROM container_smoke') == 1 ]]
    api_get "$upload_path" --output "$scratch/current.png"
    cmp "$scratch/original.png" "$scratch/current.png"
}

api_id=$(compose ps -q api)
db_id=$(compose ps -q db)
compose stop
[[ $(docker inspect --format '{{.State.Running}}' "$api_id") == false ]]
[[ $(docker inspect --format '{{.State.Running}}' "$db_id") == false ]]
compose start --wait --wait-timeout 120
[[ $(compose ps -q api) == "$api_id" ]]
[[ $(compose ps -q db) == "$db_id" ]]
check_data
printf 'Stop and start preserved the database row and uploaded image.\n'

compose up -d --wait --wait-timeout 120 --no-build --force-recreate
[[ $(compose ps -q api) != "$api_id" ]]
[[ $(compose ps -q db) != "$db_id" ]]
check_data
printf 'Container recreation preserved the database row and uploaded image.\n'
