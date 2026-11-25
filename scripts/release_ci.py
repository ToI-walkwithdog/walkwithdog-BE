"""Manual Release CI installation checks and isolated, pinned bootstrap verification."""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import secrets
import tarfile
import tempfile

BASE = "744793c95ba1362f7d6bba7f3f75cdd4614f1749"
REFERENCE = "855855138f9ab3b7992325c579eb32b586c155cf"
REPOSITORY = "ToI-walkwithdog/walkwithdog-BE"
PATHS = frozenset({
    ".github/workflows/release-ci.yml",
    ".github/release-ci-trust.json",
    "scripts/release_ci.py",
    ".github/tests/test_release_ci.py",
})
INPUTS = frozenset({
    ".dockerignore", ".github/requirements-ci.txt", "Dockerfile", "docker-compose.yml",
    "requirements.txt", "docs/dev-automation/test-baseline.json", "migrations/manifest.json",
    "scripts/check_migrations.py", "scripts/dev_gate.py", "scripts/dev_test_report.py",
    "scripts/verify_container.sh",
})
DATABASES = ("toi_walk_test_ci_signup", "toi_walk_test_ci_regions")
CORE_GROUPS = {
    "tests/test_gps_motion_postgres.py": ("GPS_MOTION", 7),
    "tests/test_walk_maps_postgres.py": ("WALK_MAPS", 16),
    "tests/test_walk_postgres.py": ("WALK", 11),
    "tests/test_walk_recording_postgres.py": ("WALK_RECORDS", 24),
    "tests/test_walk_requests_postgres.py": ("WALK_REQUESTS", 19),
    "tests/test_walk_settings_postgres.py": ("WALK_SETTINGS", 5),
}
OID = re.compile(r"[a-f0-9]{40}")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
SECRET_BYTES = re.compile(
    rb"toi-release-ci-secret-canary-[A-Za-z0-9]+|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/]{28,}|"
    rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
)
RUNTIME_KEYS = {
    "DATABASE_URL", "POSTGRES_PASSWORD", "PHONE_VERIFICATION_HMAC_KEY",
    "PUBLIC_API_KEY", "PUBLIC_IMPORT_KEY", "SOLAPI_API_KEY", "SOLAPI_API_SECRET",
    "KAKAO_REST_API_KEY", "GEMINI_API_KEY", "SHELTER_CODE_HMAC_KEY", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GITHUB_TOKEN",
}


class Blocked(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def claim(root, event, head, merge):
    pr = event["pull_request"]
    base = pr["base"]["sha"]
    require(all(OID.fullmatch(ref) for ref in (base, head, merge)), "Expected full commit IDs")
    require(pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPOSITORY,
            "Wrong release base repository or branch")
    require(pr["head"]["sha"] == head and pr["head"]["repo"]["full_name"] == REPOSITORY,
            "Wrong PR head")
    require(git(root, "rev-list", "--parents", "-1", merge).decode().split() == [merge, base, head],
            "PR merge candidate does not match the exact base and head")
    changes = git(root, "diff", "--name-only", "--no-renames", "-z", base, merge, "--")
    paths = frozenset(changes.decode().rstrip("\0").split("\0")) if changes else frozenset()
    if pr["head"]["ref"] != "chore/release-ci" or base != BASE:
        # In-process application tests cannot attest their own results. Also,
        # pull_request can replace this workflow. This is an installation
        # check, not an approval channel for subsequent release PRs.
        raise Blocked("Release verdict disabled pending an independent execution and approval boundary")
    require(paths == PATHS, "Bootstrap must change exactly the four approved paths")
    return base


def pinned_inputs(root, merge):
    doc = json.loads(git(root, "show", f"{merge}:.github/release-ci-trust.json"))
    require(doc["issue"] == 125 and doc["base"] == BASE and doc["reference"] == REFERENCE,
            "Unexpected submitted installation reference")
    require(set(doc["files"]) == INPUTS, "Incomplete reviewed input inventory")
    for path, digest in doc["files"].items():
        require(sha(git(root, "show", f"{REFERENCE}:{path}")) == digest,
                "Reviewed input checksum mismatch")
    return doc


def archive(root, revision, output):
    output.mkdir()
    with tempfile.TemporaryFile() as stream:
        subprocess.run(["git", "-C", str(root), "archive", revision], stdout=stream, check=True)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as tar:
            tar.extractall(output, filter="data")


def protect_migrations(reference, candidate):
    original = {row["file"]: row for row in reference["migrations"]}
    proposed = {row["file"]: row for row in candidate["migrations"]}
    require(all(proposed.get(name) == row for name, row in original.items()),
            "Previously approved migration record changed or was removed")


def core_inventory(baseline):
    groups = {path: [] for path in CORE_GROUPS}
    for item in baseline["skips"]:
        node = item["nodeid"]
        path = node.split("::", 1)[0]
        require(path in groups and CORE_GROUPS[path][0] + "_TEST_DATABASE_URL" in item["message"],
                "Unexpected approved PostgreSQL skip")
        groups[path].append(node)
    for path, nodes in groups.items():
        require(len(set(nodes)) == len(nodes) == CORE_GROUPS[path][1], "Core inventory mismatch")
    return groups


def require_passes(expected, report):
    require(report["exit_code"] == 0 and set(report["collected"]) == set(expected)
            and len(report["collected"]) == len(set(expected)),
            "Core PostgreSQL tests did not complete exactly")
    outcomes = {event["nodeid"] for event in report["events"]
                if event["phase"] == "call" and event["subtest"] is None and event["outcome"] == "passed"}
    require(outcomes == set(expected) and all(e["outcome"] == "passed" for e in report["events"]),
            "Core PostgreSQL integration results contain skips, failures or missing passes")


def forbidden_path(name):
    parts = Path(name).parts
    if set(parts) & {".git", ".ssh", ".aws", ".netrc", ".npmrc", ".pypirc", ".pgpass", "secrets"}:
        return True
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True
    prefix = ("srv", "walkwithdog")
    if parts[:2] == prefix and len(parts) > 2:
        return (parts[2] not in {"app", "policies", "requirements.txt"}
                or any(part.startswith(".") for part in parts[2:])
                or Path(name).suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".pyc"})
    return False


def scan_bytes(stream, member_path):
    tail = b""
    while block := stream.read(1024 * 1024):
        require(not SECRET_BYTES.search(tail + block), f"Credential-like content in image ({member_path})")
        tail = (tail + block)[-1024:]


def oci_contains_config(image, expected, config_id):
    """Tie a containerd manifest/index ID to its config in a Docker save archive."""
    try:
        index = json.load(image.extractfile("index.json"))
    except KeyError:
        return False
    pending = [(entry["digest"], False) for entry in index["manifests"]]
    visited = set()
    while pending:
        digest, matched = pending.pop()
        require(IMAGE_ID.fullmatch(digest), "Invalid OCI descriptor digest")
        matched = matched or digest == expected
        if (digest, matched) in visited:
            continue
        visited.add((digest, matched))
        content = image.extractfile(f"blobs/sha256/{digest[7:]}").read()
        require("sha256:" + sha(content) == digest, "OCI descriptor fingerprint mismatch")
        descriptor = json.loads(content)
        if "manifests" in descriptor:
            pending.extend((child["digest"], matched) for child in descriptor["manifests"])
        elif matched and descriptor.get("config", {}).get("digest") == config_id:
            return True
    return False


def scan_image(archive_path, expected):
    require(IMAGE_ID.fullmatch(expected), "Expected full tested image ID")
    with tarfile.open(archive_path, mode="r:*") as image:
        manifest, = json.load(image.extractfile("manifest.json"))
        config = image.extractfile(manifest["Config"]).read()
        config_id = "sha256:" + sha(config)
        require(config_id == expected or oci_contains_config(image, expected, config_id),
                "Scanned image differs from smoke image")
        data = json.loads(config)
        require(data["os"] == "linux" and data["architecture"] == "amd64", "Invalid image platform")
        env = {item.split("=", 1)[0] for item in data["config"].get("Env", [])}
        require(not env & RUNTIME_KEYS and not SECRET_BYTES.search(config),
                "Runtime credential, secret or build history in image metadata")
        layers = manifest["Layers"]
        expected_layers = data["rootfs"]["diff_ids"]
        require(layers and len(layers) == len(expected_layers), "Missing image layers")
        for layer_path, digest in zip(layers, expected_layers, strict=True):
            with image.extractfile(layer_path) as contents:
                # Docker may store OCI layer blobs gzip-compressed. Compare
                # the uncompressed tar with the corresponding config diff ID.
                signature = contents.read(2)
                contents.seek(0)
                stream = gzip.GzipFile(fileobj=contents) if signature == b"\x1f\x8b" else contents
                with tempfile.TemporaryFile() as layer:
                    measured = hashlib.sha256()
                    while block := stream.read(1024 * 1024):
                        measured.update(block)
                        layer.write(block)
                    require("sha256:" + measured.hexdigest() == digest, "Layer fingerprint mismatch")
                    layer.seek(0)
                    with tarfile.open(fileobj=layer, mode="r:") as files:
                        for member in files:
                            require(not forbidden_path(member.name), "Unexpected file path in image layer")
                            if member.isfile():
                                scan_bytes(files.extractfile(member), member.name)
        return len(layers)


def docker(*args, capture=False):
    command = [os.environ.get("TOI_REAL_DOCKER", "docker"), *args]
    return subprocess.check_output(command, text=True).strip() if capture else subprocess.run(command, check=True)


def inspect_image(container):
    return docker("inspect", "--format", "{{.Image}}", container, capture=True)


def filter_rules(interface):
    return (
        ("INPUT", "-i", interface),
        ("FORWARD", "-i", interface, "!", "-o", interface),
    )


def firewall(tool, operation, rule):
    chain, *conditions = rule
    command = [tool, "-w", operation, chain]
    if operation == "-I":
        command.append("1")
    command.extend([*conditions, "-m", "conntrack", "!", "--ctstate",
                    "ESTABLISHED,RELATED", "-j", "REJECT"])
    if os.geteuid() != 0:
        command.insert(0, "sudo")
        command.insert(1, "-n")
    subprocess.run(command, check=True)


@contextmanager
def isolated_bridge(interface):
    installed = []
    try:
        for tool in ("iptables", "ip6tables"):
            for rule in filter_rules(interface):
                firewall(tool, "-I", rule)
                installed.append((tool, rule))
        yield
    finally:
        failed = False
        for tool, rule in reversed(installed):
            try:
                firewall(tool, "-D", rule)
            except subprocess.CalledProcessError:
                failed = True
        require(not failed, "Could not remove all Release CI bridge rules")


def bridge_probe(image, network):
    interface = os.environ["TOI_SMOKE_BRIDGE"]
    info, = json.loads(docker("network", "inspect", network, capture=True))
    require(info["Options"]["com.docker.network.bridge.name"] == interface and not info["EnableIPv6"],
            "Unexpected Compose runtime bridge")
    for tool in ("iptables", "ip6tables"):
        for rule in filter_rules(interface):
            firewall(tool, "-C", rule)
    proof = """
import socket
import sys

for target in (sys.argv[1], "198.18.0.1"):
    with socket.socket() as connection:
        connection.settimeout(2)
        try:
            connection.connect((target, 9))
        except OSError:
            continue
        raise AssertionError("Unexpected network access")
"""
    gateway = info["IPAM"]["Config"][0]["Gateway"]
    docker("run", "--rm", "--pull", "never", "--network", network,
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--entrypoint", "python", image, "-B", "-c", proof, gateway)


def smoke_docker(args):
    scratch = Path(os.environ["TOI_SMOKE_SCRATCH"])
    proof = scratch / "tested-images"
    if args[:1] == ["compose"] and "--file" in args:
        compose_file = args.index("--file")
        end = compose_file + 2
        compose = [*args[:end], "--file", str(scratch / "compose-overlay.yml")]
        operation = args[end:]
        if operation[0] not in {"up", "start"}:
            docker(*compose, *operation)
            return
        if operation[0] == "up":
            operation = ["up", "--pull", "never", *operation[1:]]
        docker(*compose, *operation)
        container = docker(*compose, "ps", "-q", "api", capture=True)
        if container:
            image = inspect_image(container)
            networks = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Networks}}",
                                         container, capture=True))
            require(len(networks) == 1, "Unexpected API network inventory")
            network, = networks
            bridge_probe(image, network)
            with proof.open("a", encoding="utf-8") as stream:
                stream.write(image + "\n")
        return
    if args[:2] == ["image", "rm"] and proof.exists():
        image = docker("image", "inspect", "--format", "{{.Id}}", args[-1], capture=True)
        live = set(proof.read_text().splitlines())
        try:
            require(live == {image}, "Inconsistent smoke image IDs")
            saved = scratch / "image.tar"
            docker("image", "save", "--output", str(saved), image)
            layers = scan_image(saved, image)
            print(f"Inspected all {layers} layers of the smoke image")
            (scratch / "image-verified").touch()
        finally:
            docker(*args)
    else:
        docker(*args)


def smoke(source, scratch):
    scratch.mkdir()
    interface = "toi" + secrets.token_hex(5)
    (scratch / "compose-overlay.yml").write_text(
        "networks:\n  default:\n    enable_ipv6: false\n    driver_opts:\n"
        f"      com.docker.network.bridge.name: {interface}\n"
        "services:\n  api:\n    dns: [127.0.0.1]\n  db:\n    dns: [127.0.0.1]\n",
        encoding="utf-8",
    )
    helper = scratch / "docker"
    helper.write_text(
        "#!/bin/sh\nexec " + shlex.join([sys.executable, str(Path(__file__).resolve()), "smoke-docker"])
        + ' "$@"\n',
        encoding="utf-8",
    )
    helper.chmod(0o700)
    real = shutil.which("docker")
    require(real, "Docker is required")
    env = {
        "PATH": str(scratch) + os.pathsep + os.environ["PATH"],
        "HOME": str(scratch), "LC_ALL": "C.UTF-8", "TOI_REAL_DOCKER": real,
        "TOI_SMOKE_SCRATCH": str(scratch), "TOI_SMOKE_BRIDGE": interface,
        "COMPOSE_ANSI": "never", "BUILDKIT_PROGRESS": "plain",
    }
    script = source / "scripts/verify_container.sh"
    # Keep the reviewed Git file unchanged. Bash sources an LF copy with
    # the original path as argv[0] so the existing repository lookup works.
    normalized = scratch / "verify_container.sh"
    normalized.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    with isolated_bridge(interface):
        subprocess.run(["bash", "-c", 'source "$1"', str(script), str(normalized)],
                       cwd=source, env=env, check=True)
    require((scratch / "image-verified").is_file(), "No passed smoke image inspection")


REGRESSION = r"""
set -euo pipefail
install -d -o postgres -g postgres /tmp/ci-pg /tmp/ci-sock
runuser -u postgres -- /usr/lib/postgresql/16/bin/initdb -D /tmp/ci-pg --auth=trust --no-locale -E UTF8 >/dev/null
runuser -u postgres -- /usr/lib/postgresql/16/bin/pg_ctl -D /tmp/ci-pg \
    -o "-h 127.0.0.1 -p 55432 -k /tmp/ci-sock" -l /tmp/ci-pg/server.log -w start
trap 'runuser -u postgres -- /usr/lib/postgresql/16/bin/pg_ctl -D /tmp/ci-pg -m fast -w stop' EXIT
export PGHOST=127.0.0.1 PGPORT=55432 PGUSER=postgres DATABASE_URL=sqlite://
export SIGNUP_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/toi_walk_test_ci_signup
export REGION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/toi_walk_test_ci_regions
export AUTO_CREATE_SCHEMA=false APP_ENV=test SMS_PROVIDER=mock
export LOCATION_GEOCODING_PROVIDER=mock LOCATION_EXTERNAL_TRANSFER_ENABLED=false
export PHONE_VERIFICATION_HMAC_KEY=toi_synthetic_regression_key_2026
createdb toi_walk_test_ci_signup
createdb toi_walk_test_ci_regions
cd /base
PYTHONPATH=/base python -B /source/scripts/dev_test_report.py --collect-only --out /tmp/main-collection.json
inventory_status=0
python -B /checks/release_ci.py inventory /tmp/main-collection.json /source/docs/dev-automation/test-baseline.json \
    || inventory_status=$?
cd /source
PYTHONPATH=/source python -B /source/scripts/dev_test_report.py --out /tmp/regression.json
python -B /source/scripts/dev_gate.py tests /tmp/regression.json --base /source/docs/dev-automation/test-baseline.json
for db in toi_walk_test_ci_signup toi_walk_test_ci_regions; do
    actual=$(psql -X -At -v ON_ERROR_STOP=1 "$db" -c "
        SELECT count(*) FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%' AND nspname NOT IN ('public', 'information_schema');
        SELECT count(*) FROM pg_tables WHERE schemaname='public';")
    [[ "$actual" == $'0\n0' ]]
done
python -B /checks/release_ci.py trusted-core /source
exit "$inventory_status"
"""


def trusted_core(source):
    """Run the six legacy PostgreSQL groups on the pinned bootstrap source."""
    baseline = json.loads((source / "docs/dev-automation/test-baseline.json").read_text())
    expected = core_inventory(baseline)
    env = {**os.environ, "PYTHONPATH": str(source), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    for path, nodes in expected.items():
        prefix, _ = CORE_GROUPS[path]
        database = f"toi_walk_test_ci_{prefix.lower()}"
        subprocess.run(["createdb", database], env=env, check=True)
        try:
            group_env = {**env, prefix + "_TEST_DATABASE_URL":
                         f"postgresql://postgres@127.0.0.1:55432/{database}"}
            # This collector is valid for the approved bootstrap tree only.
            # It deliberately has no entry in the untrusted PR path.
            script = """
import json
import sys
import pytest
from scripts.dev_test_report import TestReport

report = TestReport()
status = pytest.main(["-q", "--strict-markers", sys.argv[1]], plugins=[report])
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    json.dump({"exit_code": int(status), "collected": report.collected, "events": report.events}, stream)
"""
            with tempfile.TemporaryDirectory() as temp:
                report = Path(temp) / "core.json"
                subprocess.run([sys.executable, "-B", "-c", script, path, str(report)],
                               cwd=source, env=group_env, check=True)
                require_passes(nodes, json.loads(report.read_text()))
        finally:
            subprocess.run(["dropdb", database], env=env, check=True)
        print(f"Validated {len(nodes)} core PostgreSQL tests and discarded their isolated database")


def inventories(main, trusted):
    require(main["exit_code"] == 0 and not main["events"], "Main baseline collection failed")
    original, reference = set(main["collected"]), set(trusted["collected"])
    require(original and len(original) == len(main["collected"]), "Main collection is incomplete")
    delta = sorted(original - reference)
    print(f"Main collection: {len(original)}. Main-only test IDs: {len(delta)}.")
    for node in delta:
        print(f"Main-only: {node}")
    require(not delta, "Main-only IDs require explicit maintenance review; no release approval produced")


def regression(source, base, scratch):
    recipe = scratch / "Dockerfile"
    # Both image roots are Bookworm. Copy the exact Python installation into
    # the PostgreSQL image before installing reviewed package requirements.
    recipe.write_text(
        "FROM python:3.12.10-slim-bookworm AS interpreter\n"
        "FROM postgres:16.16-bookworm\nCOPY --from=interpreter /usr/local/ /usr/local/\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*\n"
        "COPY requirements.txt requirements-ci.txt /tmp/dependencies/\n"
        "RUN python -m pip install --disable-pip-version-check --no-cache-dir "
        "-r /tmp/dependencies/requirements.txt -r /tmp/dependencies/requirements-ci.txt\n",
        encoding="utf-8",
    )
    shutil.copyfile(source / "requirements.txt", scratch / "requirements.txt")
    shutil.copyfile(source / ".github/requirements-ci.txt", scratch / "requirements-ci.txt")
    image = "toi-release-regression:" + scratch.parent.name.lower()
    docker("build", "--platform", "linux/amd64", "--tag", image, str(scratch))
    try:
        docker("run", "--rm", "--pull", "never", "--network", "none",
               "--security-opt", "no-new-privileges",
               "--mount", f"type=bind,source={source},target=/source,readonly",
               "--mount", f"type=bind,source={base},target=/base,readonly",
               "--mount", f"type=bind,source={Path(__file__).resolve().parent},target=/checks,readonly",
               "--entrypoint", "/bin/bash", image, "-c", REGRESSION)
    finally:
        docker("image", "rm", image)


def bootstrap(root, event, merge):
    head = event["pull_request"]["head"]["sha"]
    base = claim(root, event, head, merge)
    pinned_inputs(root, merge)
    with tempfile.TemporaryDirectory(prefix="toi-release-") as temporary:
        scratch = Path(temporary)
        source, original = scratch / "source", scratch / "base"
        archive(root, REFERENCE, source)
        archive(root, base, original)
        baseline = json.loads((source / "docs/dev-automation/test-baseline.json").read_text())
        core_inventory(baseline)
        manifest = "migrations/manifest.json"
        protect_migrations(json.loads(git(root, "show", f"{REFERENCE}:{manifest}")),
                           json.loads((source / manifest).read_text()))
        subprocess.run([sys.executable, "-I", str(source / "scripts/check_migrations.py"),
                        "--root", str(source), "--check-reservations"], check=True)
        # Only reviewed sources are executed in bootstrap. Base images and
        # package installation are prepared by the trusted runner.
        docker("pull", "postgres:16.16")
        smoke(source, scratch / "smoke")
        regression_dir = scratch / "regression"
        regression_dir.mkdir()
        regression(source, original, regression_dir)
    print("Trusted bootstrap completed. This is installation evidence only.")


def main(args):
    if args and args[0] == "smoke-docker":
        smoke_docker(args[1:])
        return
    if args and args[0] == "trusted-core":
        trusted_core(Path(args[1]))
        return
    if args and args[0] == "inventory":
        inventories(*(json.loads(Path(path).read_text()) for path in args[1:]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--event", required=True, type=Path)
    parser.add_argument("--merge", required=True)
    parser.add_argument("--verify-bootstrap", action="store_true")
    options = parser.parse_args(args)
    event = json.loads(options.event.read_text())
    claim(options.repo, event, event["pull_request"]["head"]["sha"], options.merge)
    pinned_inputs(options.repo, options.merge)
    if options.verify_bootstrap:
        bootstrap(options.repo, event, options.merge)
    else:
        print("Approved installation scope and pinned reference verified")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (Blocked, ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Release CI blocked: {exc}")
