"""Release CI for main: advisory PR preflight, installation evidence and protected-main verification.

Trust model
- pull_request runs use the workflow file from the PR merge commit, so they never approve a
  release. ``preflight`` reads Git objects with the verifier of the protected base and runs no
  candidate code. ``installation`` verifies the approved dev snapshot for the first install and
  only produces evidence for the independent review and the administrator approval.
- ``release`` is the official verdict. It runs on workflow_dispatch from protected main and reads
  the verifier, the pinned reference and the image exception from main. The dispatcher and the
  author of the approval comment must be repository administrators. The approval record must
  match the exact Base, Head, merge tree and change list computed here, and cite an independent
  review of the same Head. Dispatch inputs only select the PR and the comment.
- Only a passed ``release`` verdict writes the release artifact: the scanned smoke image as a
  single-image OCI archive (config and layer blobs unchanged, never rebuilt) and the evidence
  read by the ECR publication workflow. It is staged before any candidate test runs.
- Candidate code runs only in containers: loopback-only or outbound-rejecting networks, a
  non-root user without capabilities, read-only verifier and baseline mounts, no Docker socket
  and no runner environment. Workflow commands are suspended while candidate output streams.
  The host-side verifier copies the reports and decides with the reviewed reference tools.
- pytest still runs candidate code in the same process as the result collector. The final Head
  independent review covers test paths, dependencies, initialization and hooks.
"""
import argparse
import ast
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zlib

REPOSITORY = "ToI-walkwithdog/walkwithdog-BE"
BASE = "744793c95ba1362f7d6bba7f3f75cdd4614f1749"
REFERENCE = "f8546096672a697e8607d6f12e4766767984d2de"
TRUST = ".github/release-ci-trust.json"
WORKFLOW = ".github/workflows/release-ci.yml"
BASELINE = "docs/dev-automation/test-baseline.json"
MANIFEST = "migrations/manifest.json"
PATHS = frozenset({
    WORKFLOW,
    TRUST,
    "scripts/release_ci.py",
    ".github/tests/test_release_ci.py",
})
INPUTS = frozenset({
    ".dockerignore", ".github/requirements-ci.txt", "Dockerfile", "docker-compose.yml",
    "requirements.txt", BASELINE, MANIFEST,
    "scripts/check_migrations.py", "scripts/dev_gate.py", "scripts/dev_test_report.py",
    "scripts/verify_container.sh",
})
# The candidate may extend these two; both are checked against the protected reference.
SEMANTIC_INPUTS = frozenset({BASELINE, MANIFEST})
TRUST_FIELDS = {"issue", "base", "reference", "dev_quality_run", "files", "historical_main", "image_exception"}
EXCEPTION_FIELDS = {"reason", "platform", "base_image", "base_manifest", "base_layers", "layer", "path",
                    "file_sha256", "keys", "package", "source"}
APPROVAL_FIELDS = {"pr", "base", "head", "merge_tree", "changes", "review"}
CORE_GROUPS = {
    "tests/test_gps_motion_postgres.py": ("GPS_MOTION", 7),
    "tests/test_walk_maps_postgres.py": ("WALK_MAPS", 16),
    "tests/test_walk_postgres.py": ("WALK", 11),
    "tests/test_walk_recording_postgres.py": ("WALK_RECORDS", 24),
    "tests/test_walk_requests_postgres.py": ("WALK_REQUESTS", 19),
    "tests/test_walk_settings_postgres.py": ("WALK_SETTINGS", 5),
}
REGRESSION_DATABASES = ("toi_walk_test_ci_signup", "toi_walk_test_ci_regions")
POSTGRES_IMAGE = "postgres:16.16"
CANDIDATE_USER = "65534:65534"
CANDIDATE_ENV = {
    "HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONUTF8": "1",
    "PYTHONPATH": "/work", "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": "sqlite://",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
}
OID = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
NUMBER = re.compile(r"[1-9][0-9]{0,9}")
KEY_LABEL = re.compile(r"(?:RSA |EC |DSA )?PRIVATE KEY")
SECRET_BYTES = re.compile(
    rb"toi-release-ci-secret-canary-[A-Za-z0-9]+|"
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/]{28,}|"
    rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
)
PEM_BLOCK = re.compile(rb"-----BEGIN ([A-Z ]*PRIVATE KEY)-----[\r\n]+(.{30,8000}?)-----END \1-----", re.S)
APPROVAL_BLOCK = re.compile(r"^```toi-release-approval[ \t]*\n(.*?)\n```[ \t]*$", re.M | re.S)
RUNTIME_KEYS = {
    "DATABASE_URL", "POSTGRES_PASSWORD", "PHONE_VERIFICATION_HMAC_KEY",
    "PUBLIC_API_KEY", "PUBLIC_IMPORT_KEY", "SOLAPI_API_KEY", "SOLAPI_API_SECRET",
    "KAKAO_REST_API_KEY", "GEMINI_API_KEY", "SHELTER_CODE_HMAC_KEY", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GITHUB_TOKEN",
}
REPORT_LIMIT = 512 * 1024 * 1024


class Blocked(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def git(root, *args):
    return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(root), *args])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    keys = [key for key, _ in pairs]
    require(len(keys) == len(set(keys)), "Duplicate JSON key")
    return dict(pairs)


def loads(data):
    return json.loads(data, object_pairs_hook=unique_object)


def blob(root, treeish, path):
    return git(root, "show", f"{treeish}:{path}")


def has_path(root, treeish, path):
    return subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{treeish}:{path}"],
                          capture_output=True, check=False).returncode == 0


def ensure_commit(root, commit):
    found = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
                           capture_output=True, check=False)
    if found.returncode:
        git(root, "fetch", "--no-tags", "origin", commit)


def rev_parse(root, ref):
    return git(root, "rev-parse", "--verify", f"{ref}^{{object}}").decode().strip()


# Protected reference ---------------------------------------------------------------------------

def check_history_record(record):
    require(isinstance(record, dict) and set(record) == {"commit", "definitions", "mappings"}
            and record["commit"] == BASE and type(record["definitions"]) is int
            and record["definitions"] > 0 and isinstance(record["mappings"], dict),
            "Invalid historical main record")
    for node, mapping in record["mappings"].items():
        require(isinstance(mapping, dict) and set(mapping) == {"reason", "commit", "tests"}
                and isinstance(mapping["reason"], str) and mapping["reason"].strip()
                and isinstance(mapping["commit"], str) and OID.fullmatch(mapping["commit"])
                and isinstance(mapping["tests"], list) and mapping["tests"]
                and all(isinstance(test, str) and test for test in mapping["tests"]),
                f"Invalid historical mapping: {node}")


def check_exception_record(record):
    require(isinstance(record, dict) and set(record) == EXCEPTION_FIELDS, "Invalid image exception record")
    path = PurePosixPath(record["path"])
    keys = record["keys"]
    require(record["platform"] == "linux/amd64" and isinstance(record["base_image"], str)
            and IMAGE_ID.fullmatch(record["base_manifest"])
            and isinstance(record["base_layers"], list) and record["base_layers"]
            and all(isinstance(layer, str) and IMAGE_ID.fullmatch(layer) for layer in record["base_layers"])
            and record["layer"] in record["base_layers"]
            and isinstance(record["file_sha256"], str) and DIGEST.fullmatch(record["file_sha256"])
            and str(path) == record["path"] and not path.is_absolute() and ".." not in path.parts,
            "Invalid image exception target")
    require(isinstance(keys, list) and keys
            and all(isinstance(key, dict) and set(key) == {"label", "sha256"}
                    and isinstance(key["label"], str) and KEY_LABEL.fullmatch(key["label"])
                    and isinstance(key["sha256"], str) and DIGEST.fullmatch(key["sha256"]) for key in keys)
            and len({key["sha256"] for key in keys}) == len(keys),
            "Invalid image exception fingerprints")
    for field in ("package", "source"):
        evidence = record[field]
        require(isinstance(evidence, dict) and isinstance(evidence.get("url"), str)
                and evidence["url"].startswith("https://")
                and isinstance(evidence.get("sha256"), str) and DIGEST.fullmatch(evidence["sha256"]),
                f"Invalid image exception {field} evidence")


def load_trust(root, commit):
    """Read the trust record of a commit and check its pins against the reviewed reference."""
    doc = loads(blob(root, commit, TRUST))
    require(set(doc) == TRUST_FIELDS, "Unexpected trust record fields")
    require(doc["issue"] == 125 and doc["base"] == BASE and doc["reference"] == REFERENCE,
            "Unexpected installation reference")
    require(isinstance(doc["files"], dict) and set(doc["files"]) == INPUTS, "Incomplete reviewed input inventory")
    for path, digest in sorted(doc["files"].items()):
        require(sha(blob(root, REFERENCE, path)) == digest, f"Reviewed input checksum mismatch: {path}")
    check_history_record(doc["historical_main"])
    check_exception_record(doc["image_exception"])
    return doc


def input_findings(root, treeish, doc):
    """Pinned build and verification inputs must be byte-identical in the candidate."""
    findings = []
    for path in sorted(INPUTS - SEMANTIC_INPUTS):
        if not has_path(root, treeish, path) or sha(blob(root, treeish, path)) != doc["files"][path]:
            findings.append(f"Pinned input changed; maintenance approval required: {path}")
    return findings


# Scope ---------------------------------------------------------------------------------------

def merge_tree(root, base, head):
    result = subprocess.run(["git", "-C", str(root), "merge-tree", "--write-tree", "--no-messages", base, head],
                            capture_output=True, text=True, check=False)
    require(result.returncode == 0, "Release candidate does not merge cleanly")
    tree = result.stdout.split("\n", 1)[0].strip()
    require(OID.fullmatch(tree), "Unexpected merge-tree output")
    return tree


def changes(root, base, tree):
    """Exact candidate change list; renames are reported as deletion plus addition."""
    raw = git(root, "diff", "--no-renames", "--no-ext-diff", "--name-status", "-z", base, tree, "--").decode()
    fields = raw.split("\0")
    require(fields[-1] == "" and len(fields) % 2 == 1, "Unexpected change list")
    fields = fields[:-1]
    return sorted(zip(fields[1::2], fields[0::2]))


def change_digest(pairs):
    return sha(json.dumps([[path, status] for path, status in pairs], separators=(",", ":"),
                          ensure_ascii=False).encode())


def approval_template(number, base, head, tree, digest):
    record = {"pr": number, "base": base, "head": head, "merge_tree": tree, "changes": digest,
              "review": {"head": head, "record": "<independent review URL>", "blocking": 0}}
    return "```toi-release-approval\n" + json.dumps(record, indent=2) + "\n```"


def remote_refs(root, number):
    raw = git(root, "ls-remote", "origin", "refs/heads/main", f"refs/pull/{number}/head").decode()
    refs = dict(reversed(line.split("\t", 1)) for line in raw.splitlines() if line)
    return {"main": refs.get("refs/heads/main"), "head": refs.get(f"refs/pull/{number}/head")}


# Independent approval ---------------------------------------------------------------------------

def positive(value, label):
    require(isinstance(value, str) and NUMBER.fullmatch(value), f"Invalid {label}")
    return int(value)


def dispatcher(env, permission):
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_REPOSITORY") == REPOSITORY
            and env.get("GITHUB_WORKFLOW_REF") == f"{REPOSITORY}/{WORKFLOW}@refs/heads/main",
            "Official verification must be dispatched from protected main")
    actor = env.get("GITHUB_ACTOR", "")
    require(LOGIN.fullmatch(actor) and env.get("GITHUB_TRIGGERING_ACTOR") == actor,
            "The dispatcher must be the triggering actor")
    require((permission.get("user") or {}).get("login") == actor and permission.get("permission") == "admin",
            "Only repository administrators may dispatch the official verification")
    return actor


def approval_record(comment, number, permission):
    """Parse an unedited administrator comment; dispatch inputs are never approval evidence."""
    user = comment.get("user") or {}
    login = user.get("login", "")
    require(comment.get("issue_url") == f"https://api.github.com/repos/{REPOSITORY}/issues/{number}",
            "Approval comment belongs to another issue or PR")
    require(user.get("type") == "User" and isinstance(login, str) and LOGIN.fullmatch(login),
            "Approval must come from a person")
    require((permission.get("user") or {}).get("login") == login and permission.get("permission") == "admin",
            "Approval author is not a repository administrator")
    require(comment.get("created_at") and comment.get("created_at") == comment.get("updated_at"),
            "Edited approval comments are not accepted; post a new approval")
    blocks = APPROVAL_BLOCK.findall(str(comment.get("body", "")).replace("\r\n", "\n"))
    require(len(blocks) == 1, "Expected exactly one toi-release-approval block")
    record = loads(blocks[0])
    require(isinstance(record, dict) and set(record) == APPROVAL_FIELDS, "Unexpected approval fields")
    review = record["review"]
    require(isinstance(review, dict) and set(review) == {"head", "record", "blocking"}
            and review["head"] == record["head"] and type(review["blocking"]) is int and review["blocking"] == 0
            and isinstance(review["record"], str)
            and review["record"].startswith(f"https://github.com/{REPOSITORY}/"),
            "Approval must cite an independent review of the same Head without blocking findings")
    return record


def check_pull_request(pr, number, head):
    require(pr.get("number") == number and pr.get("state") == "open" and not pr.get("merged"),
            "Official verification requires the open PR")
    require(pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPOSITORY,
            "Release PRs must target main of this repository")
    require((pr["head"].get("repo") or {}).get("full_name") == REPOSITORY and pr["head"]["sha"] == head,
            "PR head must be the fetched commit of this repository")


def api(path, token):
    request = urllib.request.Request("https://api.github.com/" + path, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "toi-release-ci",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return loads(response.read())


def collect(out, env=os.environ, fetch=api):
    """Fetch PR, approval comment and permissions before any candidate step runs."""
    token = env.get("GH_TOKEN", "")
    require(token, "GH_TOKEN is required to collect approval evidence")
    number = positive(env.get("PR_NUMBER"), "PR number")
    comment = positive(env.get("APPROVAL_COMMENT_ID"), "approval comment ID")
    actor = env.get("GITHUB_TRIGGERING_ACTOR", "")
    require(LOGIN.fullmatch(actor), "Invalid dispatcher")
    out.mkdir(mode=0o700)
    approval = fetch(f"repos/{REPOSITORY}/issues/comments/{comment}", token)
    author = (approval.get("user") or {}).get("login", "")
    require(isinstance(author, str) and LOGIN.fullmatch(author), "Invalid approval author")
    evidence = {
        "pr": fetch(f"repos/{REPOSITORY}/pulls/{number}", token),
        "approval": approval,
        "dispatcher": fetch(f"repos/{REPOSITORY}/collaborators/{actor}/permission", token),
        "approver": fetch(f"repos/{REPOSITORY}/collaborators/{author}/permission", token),
    }
    for name, document in evidence.items():
        (out / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
    print(f"Collected evidence for PR #{number}: approval comment {comment} by {author}, dispatcher {actor}")


# Test inventory and migrations ----------------------------------------------------------------

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


def historical_definitions(root, commit):
    """Statically list the test functions of the old main without importing it."""
    names = git(root, "ls-tree", "-r", "-z", "--name-only", commit, "--", "tests").decode().split("\0")
    definitions = set()
    for name in names:
        if not name.endswith(".py") or not PurePosixPath(name).name.startswith("test_"):
            continue
        tree = ast.parse(blob(root, commit, name), filename=name)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                definitions.add(f"{name}::{node.name}")
            elif isinstance(node, ast.ClassDef):
                definitions.update(f"{name}::{node.name}::{item.name}" for item in node.body
                                   if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                                   and item.name.startswith("test_"))
    return definitions


def historical_inventory(root, record, baseline):
    definitions = historical_definitions(root, record["commit"])
    require(len(definitions) == record["definitions"],
            f"Historical main has {len(definitions)} test definitions, expected {record['definitions']}")
    supported = {node.split("[", 1)[0] for node in baseline["collected"]}
    missing = definitions - supported
    mappings = record["mappings"]
    require(missing == set(mappings), "Unmapped or stale historical main tests: "
            + ", ".join(sorted(missing ^ set(mappings))))
    for node, mapping in sorted(mappings.items()):
        require(set(mapping["tests"]) <= supported, f"Missing replacement coverage for {node}")
        print(f"Historical test mapping: {node} -> {', '.join(mapping['tests'])}")
    print(f"Compared {len(definitions)} historical main test definitions with the protected baseline")


def protect_migrations(reference, candidate):
    original = {row["file"]: row for row in reference["migrations"]}
    proposed = {row["file"]: row for row in candidate["migrations"]}
    require(all(proposed.get(name) == row for name, row in original.items()),
            "Previously approved migration record changed or was removed")


def migration_checks(root, reference_dir, source_dir, base, candidate):
    proposed = loads(blob(root, candidate, MANIFEST))
    approved = [loads(blob(root, REFERENCE, MANIFEST))]
    if has_path(root, base, MANIFEST):
        approved.append(loads(blob(root, base, MANIFEST)))
    for manifest in approved:
        protect_migrations(manifest, proposed)
        for row in manifest["migrations"]:
            path = f"migrations/{row['file']}"
            require(has_path(root, candidate, path)
                    and sha(blob(root, candidate, path).replace(b"\r\n", b"\n")) == row["sha256"],
                    f"Approved migration SQL changed or removed: {row['file']}")
    subprocess.run([sys.executable, "-I", "-B", str(reference_dir / "scripts/check_migrations.py"),
                    "--root", str(source_dir), "--check-reservations"], check=True)


# Image inspection ---------------------------------------------------------------------------------

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


def key_fingerprints(content):
    return sorted((match[1].decode(), sha(re.sub(rb"\s+", b"", match[2]))) for match in PEM_BLOCK.finditer(content))


def check_exception_file(content, exception):
    """Allow only the reviewed public self-test keys, each detection inside one of them."""
    require(sha(content) == exception["file_sha256"], "Excepted library differs from the reviewed file")
    expected = sorted((key["label"], key["sha256"]) for key in exception["keys"])
    require(key_fingerprints(content) == expected, "Excepted library keys differ from the reviewed set")
    starts = {match.start() for match in PEM_BLOCK.finditer(content)}
    detections = [match.start() for match in SECRET_BYTES.finditer(content)]
    require(len(detections) == len(expected) and set(detections) == starts,
            "Unexpected credential-like content in the excepted library")


def member_name(member):
    return str(PurePosixPath(member.name.lstrip("/"))).removeprefix("./")


def overrides(name, member, target):
    """True when a later layer replaces, removes or hides the excepted path."""
    path = PurePosixPath(name)
    target = PurePosixPath(target)
    ancestors = {target, *target.parents}
    if path.name == ".wh..wh..opq":
        return path.parent in ancestors
    if path.name.startswith(".wh."):
        return path.parent / path.name[4:] in ancestors
    return path == target or (path in ancestors and not member.isdir())


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


def scan_image(archive_path, expected, exception=None):
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
        excepted_layer = None
        if exception:
            base = exception["base_layers"]
            require(expected_layers[:len(base)] == base, "Image is not built on the reviewed base layers")
            excepted_layer = base.index(exception["layer"])
        consumed = 0
        for index, (layer_path, digest) in enumerate(zip(layers, expected_layers, strict=True)):
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
                            name = member_name(member)
                            require(not forbidden_path(name), "Unexpected file path in image layer")
                            if excepted_layer is not None and index > excepted_layer:
                                require(not overrides(name, member, exception["path"]),
                                        "A later layer replaces or removes the excepted library")
                            if not member.isfile():
                                continue
                            if index == excepted_layer and name == exception["path"]:
                                check_exception_file(files.extractfile(member).read(), exception)
                                consumed += 1
                                continue
                            scan_bytes(files.extractfile(member), name)
        if exception:
            require(consumed == 1, "Excepted library was not found exactly once in the reviewed layer")
        return len(layers)


# Release image artifact -------------------------------------------------------------------------
# Contract read by the ECR publication workflow (#136): one ZIP with exactly these two files.
# The archive reuses the scanned smoke image blobs byte for byte; nothing is rebuilt.

ARTIFACT_ARCHIVE = "image.oci.tar"
ARTIFACT_EVIDENCE = "release-evidence.json"
ARTIFACT_LIMIT = 4 * 1024 ** 3
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYER_GZIP = OCI_LAYER + "+gzip"
RUN_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
STAGED_IMAGE = "image.json"
POSTGRESQL_PASSES = 82  # Core PostgreSQL tests of the protected baseline, also required by the consumer.


class HashingReader:
    """Hash the bytes that are actually copied into the archive."""

    def __init__(self, stream):
        self.stream, self.digest = stream, hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def measure_blob(stream):
    """Digest of the stored blob and of its uncompressed tar, which is the config diff ID."""
    signature = stream.read(2)
    stream.seek(0)
    stored = HashingReader(stream)
    gzipped = signature == b"\x1f\x8b"
    reader = gzip.GzipFile(fileobj=stored) if gzipped else stored
    uncompressed = hashlib.sha256()
    while block := reader.read(1024 * 1024):
        uncompressed.update(block)
    if gzipped:
        while stored.read(1024 * 1024):
            pass
    return "sha256:" + stored.digest.hexdigest(), "sha256:" + uncompressed.hexdigest(), gzipped


def oci_member(name, size):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime, info.uid, info.gid = size, 0o644, 0, 0, 0
    info.uname = info.gname = ""
    return info


def export_oci(saved, expected, target):
    """Write the scanned `docker image save` output as a deterministic single-image OCI layout.

    Config and layer blobs are copied unchanged. The config digest must be the smoke image, and
    the uncompressed layers must match the config diff IDs in order. Only the OCI manifest and
    index are written here, so the manifest digest is the final value recorded in the evidence.
    """
    require(IMAGE_ID.fullmatch(expected), "Expected full tested image ID")
    with tarfile.open(saved, mode="r:") as image:
        entries = loads(image.extractfile("manifest.json").read())
        require(isinstance(entries, list) and len(entries) == 1, "Expected exactly one saved image")
        entry, = entries
        config = image.extractfile(entry["Config"]).read()
        config_id = "sha256:" + sha(config)
        require(config_id == expected or oci_contains_config(image, expected, config_id),
                "Saved config differs from the smoke image")
        data = loads(config)
        require(data["os"] == "linux" and data["architecture"] == "amd64", "Invalid image platform")
        require(data["rootfs"].get("type") == "layers", "Invalid image rootfs")
        diff_ids, paths = data["rootfs"]["diff_ids"], entry["Layers"]
        require(isinstance(paths, list) and paths and len(paths) == len(diff_ids), "Missing image layers")
        layers, blobs = [], {}
        for path, diff_id in zip(paths, diff_ids, strict=True):
            member = image.getmember(path)
            require(member.isfile(), "Saved layer is not a regular file")
            digest, measured, gzipped = measure_blob(image.extractfile(member))
            require(measured == diff_id, "Saved layer differs from the config diff ID")
            layers.append({"mediaType": OCI_LAYER_GZIP if gzipped else OCI_LAYER, "digest": digest,
                           "size": member.size})
            blobs.setdefault(digest, member)
        manifest = json.dumps({"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                               "config": {"mediaType": OCI_CONFIG, "digest": config_id, "size": len(config)},
                               "layers": layers}, sort_keys=True, separators=(",", ":")).encode()
        manifest_id = "sha256:" + sha(manifest)
        index = json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [
            {"mediaType": OCI_MANIFEST, "digest": manifest_id, "size": len(manifest),
             "platform": {"architecture": "amd64", "os": "linux"}}]},
            sort_keys=True, separators=(",", ":")).encode()
        small = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}', "index.json": index,
                 "blobs/sha256/" + config_id[7:]: config, "blobs/sha256/" + manifest_id[7:]: manifest}
        with open(target, "xb") as stream, tarfile.open(fileobj=stream, mode="w",
                                                        format=tarfile.USTAR_FORMAT) as archive:
            names = sorted(set(small) | {"blobs/sha256/" + digest[7:] for digest in blobs})
            for name in names:
                if name in small:
                    archive.addfile(oci_member(name, len(small[name])), io.BytesIO(small[name]))
                    continue
                member = blobs["sha256:" + name[13:]]
                copied = HashingReader(image.extractfile(member))
                archive.addfile(oci_member(name, member.size), copied)
                require("sha256:" + copied.digest.hexdigest() == "sha256:" + name[13:],
                        "Layer blob changed while copying")
    require(os.path.getsize(target) <= ARTIFACT_LIMIT, "Release image archive exceeds the artifact limit")
    return {"config_digest": config_id, "manifest_digest": manifest_id, "layers": len(layers)}


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def stage_image(smoke_scratch, stage):
    """Keep the exact scanned smoke image before any candidate test runs."""
    require((smoke_scratch / "image-verified").is_file(), "No passed smoke image inspection")
    tested = set((smoke_scratch / "tested-images").read_text(encoding="utf-8").split())
    require(len(tested) == 1, "Expected exactly one smoke image")
    image_id, = tested
    saved = smoke_scratch / "image.tar"
    require(saved.is_file() and not saved.is_symlink(), "Missing saved smoke image")
    try:
        facts = export_oci(saved, image_id, stage / ARTIFACT_ARCHIVE)
    except (tarfile.TarError, EOFError, zlib.error, KeyError) as exc:
        raise Blocked(f"Unreadable saved smoke image: {exc}") from exc
    facts.update({"smoke_image": image_id, "archive_sha256": file_sha256(stage / ARTIFACT_ARCHIVE)})
    (stage / STAGED_IMAGE).write_text(json.dumps(facts), encoding="utf-8")
    print(f"Staged smoke image {image_id}: config {facts['config_digest']}, "
          f"OCI manifest {facts['manifest_digest']}, {facts['layers']} layers")


def verification_claims(results):
    """Evidence claims come from the verdict; any missing or failed check blocks the artifact."""
    passed = {name for name, ok, _ in results if ok}
    names = {"regression": ["full regression against the protected baseline"],
             "postgresql": [f"PostgreSQL group {path}" for path in CORE_GROUPS],
             "container_smoke": ["container smoke and image layers"],
             "image_security": ["container smoke and image layers", "verified smoke image staged"]}
    for key, required in names.items():
        require(all(name in passed for name in required), f"Release evidence lacks a passed {key} check")
    # Migration checks run before any result is recorded and raise on failure.
    claims = {key: "passed" for key in ("regression", "migration", "postgresql", "container_smoke",
                                         "image_security")}
    claims["postgresql_passed"] = sum(count for _, count in CORE_GROUPS.values())
    require(claims["postgresql_passed"] == POSTGRESQL_PASSES,
            f"PostgreSQL passes differ from the protected baseline ({POSTGRESQL_PASSES})")
    return claims


def write_artifact(stage, out, release, run, results):
    """Move the staged archive and write the evidence only after the official verdict passed."""
    facts = loads((stage / STAGED_IMAGE).read_bytes())
    archive = stage / ARTIFACT_ARCHIVE
    require(file_sha256(archive) == facts["archive_sha256"], "Staged release image changed")
    evidence = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "release": release,
        "workflow": run,
        "verification": verification_claims(results),
        "image": {"platform": "linux/amd64", "archive": ARTIFACT_ARCHIVE,
                  "archive_sha256": facts["archive_sha256"], "config_digest": facts["config_digest"],
                  "manifest_digest": facts["manifest_digest"]},
    }
    out.mkdir(mode=0o700)
    try:
        os.replace(archive, out / ARTIFACT_ARCHIVE)
        with open(out / ARTIFACT_EVIDENCE, "x", encoding="utf-8") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True)
        require(sorted(path.name for path in out.iterdir()) == sorted([ARTIFACT_ARCHIVE, ARTIFACT_EVIDENCE])
                and file_sha256(out / ARTIFACT_ARCHIVE) == facts["archive_sha256"], "Unexpected release artifact")
    except BaseException:
        # Never leave a partial folder that a later step could upload.
        shutil.rmtree(out, ignore_errors=True)
        raise
    print(f"Release artifact: {ARTIFACT_ARCHIVE} sha256 {facts['archive_sha256']}, "
          f"config {facts['config_digest']}, manifest {facts['manifest_digest']}")
    return evidence


# Docker helpers -------------------------------------------------------------------------------

def docker(*args, capture=False):
    command = [os.environ.get("TOI_REAL_DOCKER", "docker"), *args]
    if capture:
        return subprocess.check_output(command, text=True).strip()
    return subprocess.run(command, check=True)


def quiet(*args):
    return subprocess.run([os.environ.get("TOI_REAL_DOCKER", "docker"), *args],
                          capture_output=True, text=True, check=False)


def inspect_image(container):
    return docker("inspect", "--format", "{{.Image}}", container, capture=True)


def runtime_container_check(info):
    """Compose containers must not receive host privileges, host namespaces or host paths."""
    host = info["HostConfig"]
    require(not host.get("Privileged") and not host.get("CapAdd") and not host.get("Devices")
            and host.get("NetworkMode") != "host" and host.get("PidMode") != "host"
            and host.get("IpcMode") != "host" and host.get("UsernsMode") != "host",
            "Unexpected container privileges")
    for mount in info.get("Mounts") or []:
        require(mount.get("Type") == "volume" and "docker.sock" not in json.dumps(mount),
                "Unexpected host mount in a candidate container")


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

for target, port in ((sys.argv[1], 9), ("198.18.0.1", 9), ("1.1.1.1", 443)):
    with socket.socket() as connection:
        connection.settimeout(2)
        try:
            connection.connect((target, port))
        except OSError:
            continue
        raise AssertionError("Unexpected network access")
"""
    gateway = info["IPAM"]["Config"][0]["Gateway"]
    docker("run", "--rm", "--pull", "never", "--network", network,
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--entrypoint", "python", image, "-I", "-B", "-c", proof, gateway)


def compose_services(operation):
    """Service names of a compose up or start command, skipping options and their values."""
    services, skip = [], False
    for arg in operation[1:]:
        if skip:
            skip = False
        elif arg in {"--wait-timeout", "--timeout", "-t"}:
            skip = True
        elif not arg.startswith("-"):
            services.append(arg)
    return services


def split_start_wait(operation):
    """Compose 2.x on hosted runners has no `start --wait`. Start the stopped containers, then
    wait for health with `up --wait --no-recreate`, which keeps the same containers and timeout."""
    start, timeout, wait, rest = ["start"], None, False, iter(operation[1:])
    for arg in rest:
        if arg == "--wait":
            wait = True
        elif arg == "--wait-timeout":
            timeout = next(rest, "")
            require(timeout.isdigit(), "Invalid Compose wait timeout")
        else:
            start.append(arg)
    if not wait:
        require(timeout is None, "Compose wait timeout without --wait")
        return start, None
    limit = ["--wait-timeout", timeout] if timeout else []
    return start, ["up", "--detach", "--wait", *limit, "--no-build", "--no-recreate", "--pull", "never", *start[1:]]


def smoke_docker(args):
    """Docker wrapper used by the reviewed verify_container.sh during the smoke test."""
    scratch = Path(os.environ["TOI_SMOKE_SCRATCH"])
    exception = loads(Path(os.environ["TOI_SMOKE_EXCEPTION"]).read_bytes())
    proof = scratch / "tested-images"
    verified = scratch / "network-verified"
    if args[:1] == ["compose"] and "--file" in args:
        compose_file = args.index("--file")
        end = compose_file + 2
        compose = [*args[:end], "--file", str(scratch / "compose-overlay.yml")]
        operation = args[end:]
        # Candidate code runs in api containers; only the database may start before the check.
        if operation[0] in {"run", "exec"} or (operation[0] in {"up", "start"}
                                               and compose_services(operation) != ["db"]):
            require(verified.is_file(), "Candidate containers must not start before the network isolation check")
        if operation[0] not in {"up", "start"}:
            docker(*compose, *operation)
            return
        if operation[0] == "up":
            docker(*compose, "up", "--pull", "never", *operation[1:])
        else:
            start, wait = split_start_wait(operation)
            docker(*compose, *start)
            if wait:
                docker(*compose, *wait)
        containers = docker(*compose, "ps", "-q", capture=True).split()
        for container in containers:
            runtime_container_check(json.loads(docker("inspect", container, capture=True))[0])
        if containers and not verified.is_file():
            networks = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Networks}}",
                                         containers[0], capture=True))
            require(len(networks) == 1, "Unexpected Compose network inventory")
            network, = networks
            # The reviewed base image probes the bridge before any candidate container starts.
            bridge_probe("python@" + exception["base_manifest"], network)
            verified.touch()
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
            layers = scan_image(saved, image, exception)
            print(f"Inspected all {layers} layers of the smoke image; only the reviewed GnuTLS "
                  f"self-test keys were excepted")
            (scratch / "image-verified").touch()
        finally:
            docker(*args)
    else:
        docker(*args)


def smoke(reference, source, scratch, exception):
    """Run the reviewed verify_container.sh on the candidate tree behind a closed bridge."""
    scratch.mkdir(mode=0o700)
    interface = "toi" + secrets.token_hex(5)
    (scratch / "compose-overlay.yml").write_text(
        "networks:\n  default:\n    enable_ipv6: false\n    driver_opts:\n"
        f"      com.docker.network.bridge.name: {interface}\n"
        "services:\n  api:\n    dns: [127.0.0.1]\n  db:\n    dns: [127.0.0.1]\n",
        encoding="utf-8",
    )
    (scratch / "exception.json").write_text(json.dumps(exception), encoding="utf-8")
    helper = scratch / "docker"
    helper.write_text(
        "#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "smoke-docker"])
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
        "TOI_SMOKE_EXCEPTION": str(scratch / "exception.json"),
        "COMPOSE_ANSI": "never", "BUILDKIT_PROGRESS": "plain",
    }
    # On a noexec temporary directory the shell would silently fall back to the real client and
    # skip the image inspection. Fail first with the cause instead.
    require(shutil.which("docker", path=env["PATH"]) == str(helper),
            "The smoke Docker wrapper is not executable; use an exec-capable TMPDIR")
    # The reviewed reference script runs with the candidate script path as argv[0], so the
    # existing repository lookup builds the candidate tree with its pinned Compose inputs.
    normalized = scratch / "verify_container.sh"
    normalized.write_bytes((reference / "scripts/verify_container.sh").read_bytes())
    with isolated_bridge(interface):
        subprocess.run(["bash", "-c", 'source "$1"', str(source / "scripts/verify_container.sh"), str(normalized)],
                       cwd=source, env=env, check=True)
    require((scratch / "image-verified").is_file(), "No passed smoke image inspection")


def prepare_images(exception):
    """Check tooling, then pull and check reviewed base images before any candidate code runs."""
    # The smoke wrapper serves `start --wait` of the pinned verify_container.sh with
    # `up --wait --no-recreate`, which Compose 2.x supports; fail before any candidate run.
    usage = docker("compose", "up", "--help", capture=True)
    require(all(re.search(rf"(?m)^\s+{option}(?=\s)", usage) for option in ("--wait", "--wait-timeout", "--no-recreate")),
            "Docker Compose on this runner lacks `up --wait --no-recreate`, which the smoke wrapper uses")
    pinned = "python@" + exception["base_manifest"]
    for image in (pinned, exception["base_image"], POSTGRES_IMAGE):
        docker("pull", "--quiet", "--platform", exception["platform"], image)
    for image in (pinned, exception["base_image"]):
        layers = loads(docker("image", "inspect", "--format", "{{json .RootFS.Layers}}", image, capture=True))
        require(layers == exception["base_layers"], f"{image} does not match the reviewed base layers")


def build_test_image(reference, folder, base_image):
    """Prepare reviewed public dependencies with network access before candidate execution."""
    folder.mkdir()
    # Migration tests need pg_dump and pg_restore 16.16. Both roots are Debian 12 (bookworm), so the
    # exact Python 3.12.10 installation is copied onto the PostgreSQL 16.16 client image.
    (folder / "Dockerfile").write_text(
        f"FROM {base_image} AS interpreter\n"
        f"FROM {POSTGRES_IMAGE}-bookworm\n"
        "COPY --from=interpreter /usr/local/ /usr/local/\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*\n"
        "COPY requirements.txt requirements-ci.txt /tmp/dependencies/\n"
        "RUN python -m pip install --disable-pip-version-check --no-cache-dir "
        "-r /tmp/dependencies/requirements.txt -r /tmp/dependencies/requirements-ci.txt\n",
        encoding="utf-8",
    )
    shutil.copyfile(reference / "requirements.txt", folder / "requirements.txt")
    shutil.copyfile(reference / ".github/requirements-ci.txt", folder / "requirements-ci.txt")
    tag = "toi-release-tests:" + secrets.token_hex(6)
    docker("build", "--pull=false", "--platform", "linux/amd64", "--tag", tag, str(folder))
    return tag


# Isolated PostgreSQL and candidate runs -------------------------------------------------------

def start_postgres(folder):
    """Disposable PostgreSQL without network; only the non-superuser test role may use TCP."""
    folder.mkdir()
    (folder / "pg_hba.conf").write_text("local all postgres trust\nhost all toi_ci 127.0.0.1/32 trust\n",
                                        encoding="utf-8")
    folder.chmod(0o755)
    (folder / "pg_hba.conf").chmod(0o644)
    name = "toi-release-pg-" + secrets.token_hex(6)
    capabilities = [flag for cap in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")
                    for flag in ("--cap-add", cap)]
    docker("run", "--detach", "--quiet", "--pull", "never", "--name", name, "--network", "none",
           "--pids-limit", "512", "--security-opt", "no-new-privileges", "--cap-drop", "ALL", *capabilities,
           "--env", f"POSTGRES_PASSWORD={secrets.token_hex(16)}",
           "--mount", f"type=bind,source={folder},target=/etc/toi-release,readonly",
           POSTGRES_IMAGE, "-c", "hba_file=/etc/toi-release/pg_hba.conf")
    try:
        for _ in range(120):
            if quiet("exec", name, "pg_isready", "-q", "-h", "127.0.0.1", "-p", "5432").returncode == 0:
                break
            time.sleep(1)
        else:
            raise Blocked("Isolated PostgreSQL did not start")
        require(psql(name, "postgres", "SHOW server_version_num") == "160016", "Expected PostgreSQL 16.16")
        psql(name, "postgres", "CREATE ROLE toi_ci LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
    except BaseException:
        quiet("rm", "--force", "--volumes", name)
        raise
    return name


def psql(container, database, *statements):
    # The candidate role owns test databases and may create objects in public; trusted
    # superuser queries must resolve operators and functions from pg_catalog only.
    command = ["exec", "--user", "postgres", container, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
               "-d", database, "-c", "SET search_path = pg_catalog"]
    for statement in statements:
        command.extend(["-c", statement])
    return docker(*command, capture=True)


def create_database(container, database):
    docker("exec", "--user", "postgres", container, "createdb", "--owner", "toi_ci", database)


def database_clean(container, database):
    """True when no test schema or table remains; the database is dropped either way."""
    try:
        counts = psql(container, database,
                      "SELECT count(*) FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
                      "AND nspname NOT IN ('public', 'information_schema')",
                      "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        return counts.split() == ["0", "0"]
    finally:
        docker("exec", "--user", "postgres", container, "dropdb", "--force", database)


def drop_database(container, database):
    """Core groups build their own schema in a disposable database; prove it is gone."""
    docker("exec", "--user", "postgres", container, "dropdb", "--force", database)
    remaining = psql(container, "postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{database}'")
    require(remaining == "0", f"Disposable database {database} was not removed")


def dsn(database):
    return f"postgresql+psycopg2://toi_ci@127.0.0.1:5432/{database}"


def extract(root, treeish, output):
    output.mkdir(parents=True)
    with tempfile.TemporaryFile() as stream:
        subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(root), "archive", treeish],
                       stdout=stream, check=True)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as tar:
            tar.extractall(output, filter="data")
    return output


def phase_dir(root, treeish, folder):
    """Fresh writable copy for one candidate run, so no run can alter another run's input."""
    work = extract(root, treeish, folder / "work") if treeish else folder / "work"
    work.mkdir(parents=True, exist_ok=True)
    for path in [work, *work.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(0o777 if path.is_dir() else path.stat().st_mode | 0o666)
    out = folder / "out"
    out.mkdir()
    out.chmod(0o777)
    return work, out


def candidate_command(image, postgres, work, reference, out, env, command):
    args = ["run", "--rm", "--pull", "never", "--network", f"container:{postgres}",
            "--user", CANDIDATE_USER, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "1024", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=2g",
            "--mount", f"type=bind,source={work},target=/work",
            "--mount", f"type=bind,source={reference},target=/reference,readonly",
            "--mount", f"type=bind,source={out},target=/out",
            "--workdir", "/work"]
    for key, value in sorted(env.items()):
        args.extend(["--env", f"{key}={value}"])
    return [*args, "--entrypoint", command[0], image, *command[1:]]


def run_candidate(image, postgres, work, reference, out, env, command):
    # The exit code is candidate-controlled; the host only trusts its own report checks.
    return subprocess.run([os.environ.get("TOI_REAL_DOCKER", "docker"),
                           *candidate_command(image, postgres, work, reference, out, env, command)],
                          check=False).returncode


def trusted_copy(path, destination):
    """Copy a candidate-written report without following links planted by the candidate."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= REPORT_LIMIT, f"Invalid report file: {path.name}")
        data = stream.read(REPORT_LIMIT + 1)
    require(len(data) <= REPORT_LIMIT, f"Report too large: {path.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


PROBE = r"""
import json
import os
from pathlib import Path
import socket


def attempt(action):
    try:
        action()
    except Exception:
        return "blocked"
    return "allowed"


def connect(host, port):
    socket.create_connection((host, port), timeout=2).close()


def database(user):
    import psycopg2
    psycopg2.connect(host="127.0.0.1", port=5432, user=user, dbname="postgres", connect_timeout=3).close()


status = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in open("/proc/self/status")}
result = {
    "tcp_external": attempt(lambda: connect("198.18.0.1", 9)),
    "tcp_public": attempt(lambda: connect("1.1.1.1", 53)),
    "dns": attempt(lambda: socket.getaddrinfo("example.com", 443)),
    "write_reference": attempt(lambda: Path("/reference/probe").write_text("x")),
    "write_root": attempt(lambda: Path("/probe").write_text("x")),
    "superuser": attempt(lambda: database("postgres")),
    "test_role": attempt(lambda: database("toi_ci")),
    "write_out": attempt(lambda: Path("/out/write-check").write_text("x")),
    "interfaces": sorted(os.listdir("/sys/class/net")),
    "docker_socket": any(Path(p).exists() for p in ("/var/run/docker.sock", "/run/docker.sock")),
    "uid": os.getuid(),
    "cap_eff": status["CapEff"],
    "no_new_privs": status["NoNewPrivs"],
    "env": sorted(os.environ),
}
Path("/out/probe.json").write_text(json.dumps(result))
"""


def evaluate_probe(result):
    for key in ("tcp_external", "tcp_public", "dns", "write_reference", "write_root", "superuser"):
        require(result[key] == "blocked", f"Isolation probe: {key} was allowed")
    require(result["test_role"] == "allowed" and result["write_out"] == "allowed",
            "Isolation probe could not reach its own test database or report directory")
    require(result["interfaces"] == ["lo"], "Candidate network has interfaces besides loopback")
    require(result["uid"] == 65534 and int(result["cap_eff"], 16) == 0 and result["no_new_privs"] == "1",
            "Candidate process has privileges")
    require(result["docker_socket"] is False, "Docker socket is visible to the candidate")
    leaked = [key for key in result["env"] if key not in CANDIDATE_ENV and (
              key in RUNTIME_KEYS or key.startswith(("GITHUB_", "ACTIONS_", "RUNNER_"))
              or re.search("TOKEN|SECRET|PASSWORD|CREDENTIAL", key))]
    require(not leaked, f"Runner or credential variables reached the candidate: {leaked}")


def probe(scratch, image, postgres, reference):
    """Check the exact candidate container settings with the reviewed probe, not candidate code."""
    work, out = phase_dir(None, None, scratch / "probe")
    run_candidate(image, postgres, work, reference, out, CANDIDATE_ENV, ["python", "-I", "-B", "-c", PROBE])
    evaluate_probe(loads(trusted_copy(out / "probe.json", scratch / "trusted" / "probe.json").read_bytes()))
    print("Candidate container: loopback only, uid 65534, no capabilities, read-only reference, no Docker socket")


def full_suite(root, candidate, reference, scratch, image, postgres):
    work, out = phase_dir(root, candidate, scratch / "regression")
    for database in REGRESSION_DATABASES:
        create_database(postgres, database)
    env = {**CANDIDATE_ENV, "SIGNUP_TEST_DATABASE_URL": dsn(REGRESSION_DATABASES[0]),
           "REGION_TEST_DATABASE_URL": dsn(REGRESSION_DATABASES[1])}
    try:
        run_candidate(image, postgres, work, reference, out, env,
                      ["python", "-B", "/reference/scripts/dev_test_report.py", "--out", "/out/report.json"])
    finally:
        dirty = [database for database in REGRESSION_DATABASES if not database_clean(postgres, database)]
    trusted = scratch / "trusted"
    report = trusted_copy(out / "report.json", trusted / "regression.json")
    proposed = trusted / "candidate-baseline.json"
    proposed.write_bytes(blob(root, candidate, BASELINE))
    gate = subprocess.run([sys.executable, "-I", "-B", str(reference / "scripts/dev_gate.py"), "tests", str(report),
                           "--base", str(reference / BASELINE), "--candidate", str(proposed)], check=False)
    require(not dirty, f"Test schema or data remained in {', '.join(dirty)}")
    require(gate.returncode == 0, "Regression does not match the protected baseline")


CORE_TEST = """
import json
import sys

sys.path.insert(0, "/reference/scripts")
from dev_test_report import TestReport
import pytest

report = TestReport()
status = pytest.main(["-q", "--strict-markers", sys.argv[1]], plugins=[report])
with open("/out/core.json", "w", encoding="utf-8") as stream:
    json.dump({"exit_code": int(status), "collected": report.collected, "events": report.events}, stream)
"""


def require_passes(expected, report):
    require(report["exit_code"] == 0 and set(report["collected"]) == set(expected)
            and len(report["collected"]) == len(set(expected)),
            "Core PostgreSQL tests did not complete exactly")
    outcomes = {event["nodeid"] for event in report["events"]
                if event["phase"] == "call" and event["subtest"] is None and event["outcome"] == "passed"}
    require(outcomes == set(expected) and all(e["outcome"] == "passed" for e in report["events"]),
            "Core PostgreSQL integration results contain skips, failures or missing passes")


def core_group(root, candidate, reference, scratch, image, postgres, path, nodes):
    prefix, _ = CORE_GROUPS[path]
    database = f"toi_walk_test_ci_{prefix.lower()}"
    work, out = phase_dir(root, candidate, scratch / f"core-{prefix.lower()}")
    create_database(postgres, database)
    try:
        run_candidate(image, postgres, work, reference, out,
                      {**CANDIDATE_ENV, f"{prefix}_TEST_DATABASE_URL": dsn(database)},
                      ["python", "-B", "-c", CORE_TEST, path])
    finally:
        drop_database(postgres, database)
    report = loads(trusted_copy(out / "core.json", scratch / "trusted" / f"core-{prefix.lower()}.json").read_bytes())
    require_passes(nodes, report)
    print(f"Validated {len(nodes)} core PostgreSQL tests in {path}; the isolated database was dropped")


# Verification -----------------------------------------------------------------------------------

@contextmanager
def command_barrier():
    """Suspend workflow commands while candidate output reaches the job log."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        yield
        return
    token = secrets.token_hex(24)
    print(f"::stop-commands::{token}", flush=True)
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        print(f"::{token}::", flush=True)


def run_check(name, action):
    try:
        action()
    except (Blocked, subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAILED {name}: {exc}", flush=True)
        return name, False, str(exc)
    return name, True, ""


def candidate_runs(root, candidate, reference, scratch, image, groups):
    results = []
    postgres = start_postgres(scratch / "postgres")
    try:
        results.append(run_check("candidate isolation probe", lambda: probe(scratch, image, postgres, reference)))
        if not results[-1][1]:
            return results
        results.append(run_check("full regression against the protected baseline",
                                 lambda: full_suite(root, candidate, reference, scratch, image, postgres)))
        for path, nodes in groups.items():
            results.append(run_check(f"PostgreSQL group {path}",
                                     lambda p=path, n=nodes: core_group(root, candidate, reference, scratch,
                                                                        image, postgres, p, n)))
    finally:
        quiet("rm", "--force", "--volumes", postgres)
    return results


def owner(path):
    return os.lstat(path).st_uid


def reclaim(scratch):
    """Return files written by uid 65534 under the verified scratch directory to the runner user.

    Limited to this run's own temporary directory; chown neither traverses nor dereferences links.
    """
    require(not scratch.is_symlink() and scratch.is_dir(), "Scratch directory must be a real directory")
    resolved = scratch.resolve(strict=True)
    require(resolved.parent == Path(tempfile.gettempdir()).resolve() and resolved.name.startswith("toi-release-")
            and owner(resolved) == os.geteuid(), "Refusing to change ownership outside the run scratch")
    if os.geteuid() == 0:
        return
    subprocess.run(["sudo", "-n", "chown", "-R", "-P", "--no-dereference", "--preserve-root",
                    f"{os.geteuid()}:{os.getegid()}", "--", str(resolved)], check=True)


def verify(root, candidate, doc, base, stage=None):
    """Static checks, then the isolated smoke, regression and PostgreSQL runs of one tree.

    With ``stage`` (official release only), the scanned smoke image is kept there as an OCI
    archive right after the smoke check and before any candidate test runs.
    """
    results = []
    # A cleanup failure must not hide the verdict; it is reported as a failed check instead.
    with tempfile.TemporaryDirectory(prefix="toi-release-", ignore_cleanup_errors=True) as temporary:
        scratch = Path(temporary)
        try:
            # Static checks print text from candidate files, so workflow commands stop here.
            with command_barrier():
                reference = extract(root, REFERENCE, scratch / "reference")
                source = extract(root, candidate, scratch / "source")
                for path in sorted(INPUTS - SEMANTIC_INPUTS):
                    require(sha((source / path).read_bytes()) == doc["files"][path],
                            f"Extracted candidate input differs from the pinned file: {path}")
                baseline = loads(blob(root, REFERENCE, BASELINE))
                groups = core_inventory(baseline)
                historical_inventory(root, doc["historical_main"], baseline)
                migration_checks(root, reference, source, base, candidate)
                exception = doc["image_exception"]
                prepare_images(exception)
                image = build_test_image(reference, scratch / "test-image", exception["base_image"])
                try:
                    results.append(run_check("container smoke and image layers",
                                             lambda: smoke(reference, source, scratch / "smoke", exception)))
                    if stage is not None and results[-1][1]:
                        results.append(run_check("verified smoke image staged",
                                                 lambda: stage_image(scratch / "smoke", stage)))
                    results.extend(candidate_runs(root, candidate, reference, scratch, image, groups))
                finally:
                    quiet("image", "rm", "--force", image)
        finally:
            results.append(run_check("candidate files reclaimed for cleanup", lambda: reclaim(scratch)))
    return results


def record(title, facts, results):
    lines = [f"## {title}", "", "| Item | Value |", "| --- | --- |"]
    lines.extend(f"| {key} | `{value}` |" for key, value in facts)
    lines.extend(["", "| Check | Result |", "| --- | --- |"])
    lines.extend(f"| {name} | {'passed' if ok else 'FAILED'} |" for name, ok, _ in results)
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(text)


def finish(results, success):
    failures = [name for name, ok, _ in results if not ok]
    require(not failures, "Verification failed: " + ", ".join(failures))
    print(success)


# Modes ------------------------------------------------------------------------------------------

def static_findings(root, tree, base, doc):
    findings = input_findings(root, tree, doc)
    with tempfile.TemporaryDirectory(prefix="toi-preflight-") as temporary, command_barrier():
        scratch = Path(temporary)
        try:
            baseline = loads(blob(root, REFERENCE, BASELINE))
            core_inventory(baseline)
            historical_inventory(root, doc["historical_main"], baseline)
            migration_checks(root, extract(root, REFERENCE, scratch / "reference"),
                             extract(root, tree, scratch / "source"), base, tree)
        except (Blocked, subprocess.CalledProcessError) as exc:
            findings.append(str(exc))
    return findings


def preflight(root, event):
    """Advisory scope check of a release PR. Reads Git objects only; runs no candidate code."""
    pr = event["pull_request"]
    number, base, head = pr["number"], pr["base"]["sha"], pr["head"]["sha"]
    require(type(number) is int and OID.fullmatch(base) and OID.fullmatch(head), "Expected PR number and full SHAs")
    require(pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPOSITORY,
            "Release PRs must target main of this repository")
    require(rev_parse(root, "HEAD") == base, "Preflight must run from the protected base checkout")
    git(root, "fetch", "--no-tags", "origin", f"+refs/pull/{number}/head:refs/toi/preflight-head")
    require(rev_parse(root, "refs/toi/preflight-head") == head, "PR head moved; a new run will follow")
    ensure_commit(root, REFERENCE)
    tree = merge_tree(root, base, head)
    pairs = changes(root, base, tree)
    digest = change_digest(pairs)
    findings = []
    if (pr["head"].get("repo") or {}).get("full_name") != REPOSITORY:
        findings.append("Release candidates must come from this repository")
    protected = sorted(path for path, _ in pairs if path in PATHS)
    if protected:
        findings.append("Verifier changes need a separate maintenance PR: " + ", ".join(protected))
    findings.extend(static_findings(root, tree, base, load_trust(root, base)))
    print(f"PR #{number}: base {base}, head {head}, merge tree {tree}, {len(pairs)} changed paths, "
          f"change digest {digest}")
    print("Review the changes, then post this record with the independent review URL:")
    print(approval_template(number, base, head, tree, digest))
    require(not findings, "Advisory preflight found: " + "; ".join(findings))
    print("Advisory preflight only. The official verdict requires the protected-main workflow_dispatch run.")


def installation(root, event):
    """First install: verify the approved dev snapshot and print evidence for manual approval."""
    pr = event["pull_request"]
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    require(base == BASE and OID.fullmatch(head), "Installation must use the approved main base")
    require(pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPOSITORY
            and (pr["head"].get("repo") or {}).get("full_name") == REPOSITORY
            and pr["head"]["ref"] == "chore/release-ci", "Unexpected installation PR")
    require(not has_path(root, base, "scripts/release_ci.py") and not has_path(root, base, TRUST),
            "Release CI is already installed; use the protected-main workflow")
    merge = rev_parse(root, "HEAD")
    require(git(root, "rev-list", "--parents", "-1", merge).decode().split() == [merge, base, head],
            "Installation must run on the exact merge candidate")
    tree = rev_parse(root, f"{merge}^{{tree}}")
    require(tree == merge_tree(root, base, head), "Merge candidate tree differs from the computed merge")
    pairs = changes(root, base, tree)
    require(pairs == sorted((path, "A") for path in PATHS), "Installation must add exactly the four approved paths")
    ensure_commit(root, REFERENCE)
    doc = load_trust(root, head)
    facts = [("mode", "installation evidence only"), ("base", base), ("head", head), ("merge tree", tree),
             ("change digest", change_digest(pairs)), ("reference", REFERENCE)]
    facts.extend((f"sha256 {path}", sha(blob(root, head, path))) for path in sorted(PATHS))
    results = verify(root, REFERENCE, doc, base)
    record("Release CI installation evidence", facts, results)
    finish(results, "Installation evidence only. The final Head requires independent review and "
                    "administrator approval.")


def release(root, evidence_dir, env=os.environ, artifact=None):
    """Official verdict, dispatched by an administrator on protected main.

    With ``artifact``, the verified image and its evidence are written there only after the
    verdict passed. The workflow uploads that folder in the next step.
    """
    require(artifact is None or not os.path.lexists(artifact), "Release artifact folder already exists")
    evidence = {name: loads((evidence_dir / f"{name}.json").read_bytes())
                for name in ("pr", "approval", "dispatcher", "approver")}
    actor = dispatcher(env, evidence["dispatcher"])
    number = positive(env.get("PR_NUMBER"), "PR number")
    comment = positive(env.get("APPROVAL_COMMENT_ID"), "approval comment ID")
    require(evidence["approval"].get("id") == comment, "Collected approval does not match the dispatch input")
    base = env.get("GITHUB_SHA", "")
    require(OID.fullmatch(base) and rev_parse(root, "HEAD") == base, "Checkout differs from the dispatched main")
    git(root, "fetch", "--no-tags", "origin", f"+refs/pull/{number}/head:refs/toi/release-head")
    head = rev_parse(root, "refs/toi/release-head")
    check_pull_request(evidence["pr"], number, head)
    require(remote_refs(root, number) == {"main": base, "head": head}, "Protected main or PR head changed; rerun")
    tree = merge_tree(root, base, head)
    pairs = changes(root, base, tree)
    digest = change_digest(pairs)
    approval = approval_record(evidence["approval"], number, evidence["approver"])
    current = {"pr": number, "base": base, "head": head, "merge_tree": tree, "changes": digest}
    mismatched = sorted(key for key, value in current.items() if approval[key] != value)
    require(not mismatched, "Approval does not match the current candidate: " + ", ".join(mismatched))
    protected = sorted(path for path, _ in pairs if path in PATHS)
    require(not protected, "Verifier changes need a separate maintenance PR: " + ", ".join(protected))
    ensure_commit(root, REFERENCE)
    doc = load_trust(root, base)
    findings = input_findings(root, tree, doc)
    require(not findings, "; ".join(findings))
    facts = [("mode", "official release verification"), ("PR", number), ("base (protected main)", base),
             ("head", head), ("merge tree", tree), ("change digest", digest), ("changed paths", len(pairs)),
             ("dispatcher", actor), ("approval comment", comment),
             ("approver", evidence["approval"]["user"]["login"]), ("independent review", approval["review"]["record"]),
             ("reference", REFERENCE)]
    run = None
    if artifact is not None:
        run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
        require(RUN_NUMBER.fullmatch(run_id) and RUN_NUMBER.fullmatch(attempt), "Invalid workflow run identity")
        run = {"run_id": int(run_id), "run_attempt": int(attempt), "workflow_sha": base}
    stage = None if artifact is None else artifact.parent / f".{artifact.name}.staging"
    if stage is not None:
        stage.mkdir(mode=0o700)
    try:
        results = verify(root, tree, doc, base, stage=stage)
        results.append(run_check("Base and Head unchanged during verification", lambda: require(
            remote_refs(root, number) == {"main": base, "head": head}, "Protected main or PR head changed; rerun")))
        record("Release CI official verification", facts, results)
        finish(results,
               f"Protected-main release verification passed: PR #{number} base {base} head {head} tree {tree}")
        if artifact is not None:
            write_artifact(stage, artifact, {"pr_number": number, "base_sha": base, "head_sha": head,
                                             "merge_tree_sha": tree}, run, results)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def scope(root, number, base, head):
    """Local helper for administrators: print the record that an approval must contain."""
    require(OID.fullmatch(base) and OID.fullmatch(head), "Expected full SHAs")
    tree = merge_tree(root, base, head)
    pairs = changes(root, base, tree)
    for path, status in pairs:
        print(f"{status}\t{path}")
    print(approval_template(number, base, head, tree, change_digest(pairs)))


def main(args):
    if args and args[0] == "smoke-docker":
        smoke_docker(args[1:])
        return
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "installation"):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True, type=Path)
        command.add_argument("--event", required=True, type=Path)
    command = commands.add_parser("collect")
    command.add_argument("--out", required=True, type=Path)
    command = commands.add_parser("release")
    command.add_argument("--repo", required=True, type=Path)
    command.add_argument("--evidence", required=True, type=Path)
    command.add_argument("--artifact-out", type=Path,
                         help="New folder for the verified image and evidence, written only on success")
    command = commands.add_parser("scope")
    command.add_argument("--repo", required=True, type=Path)
    command.add_argument("--pr", required=True, type=int)
    command.add_argument("--base", required=True)
    command.add_argument("--head", required=True)
    options = parser.parse_args(args)
    if options.command == "preflight":
        preflight(options.repo, loads(options.event.read_bytes()))
    elif options.command == "installation":
        installation(options.repo, loads(options.event.read_bytes()))
    elif options.command == "collect":
        collect(options.out)
    elif options.command == "release":
        release(options.repo, options.evidence, artifact=options.artifact_out)
    else:
        scope(options.repo, options.pr, options.base, options.head)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (Blocked, ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Release CI blocked: {exc}")
