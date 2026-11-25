"""Integrity and read-only GitHub checks for ECR publication. Not an authorization gate.

The trusted GitHub run, review and source-tree checks must precede any publish.
Publication and evidence generation remain simulation-only; no live publish command.
"""
import argparse
import os
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import subprocess
import tarfile

REPOSITORY = "ToI-walkwithdog/walkwithdog-BE"
REPOSITORY_ID = 1409697696
RELEASE_WORKFLOW = ".github/workflows/release-ci.yml"
PLATFORM = "linux/amd64"
SHA = re.compile(r"[0-9a-f]{40}")
HEX = re.compile(r"[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
GZIP = LAYER + "+gzip"
CHECKS = ("regression", "migration", "postgresql", "container_smoke", "image_security")
MAX_JSON = 8 * 1024 * 1024
BLOCK = 1024 * 1024
MAX_ARCHIVE = 4 * 1024**3  # Provisional compressed input limit.
MAX_UNCOMPRESSED_LAYERS = 16 * 1024**3  # Provisional cumulative layer expansion limit.
RUN_IDENTITY_FIELDS = ("id", "run_attempt", "event", "head_branch", "head_sha", "path",
                       "status", "conclusion", "run_started_at", "updated_at")


class Blocked(Exception):
    """The supplied input cannot be used."""


def require(condition, message):
    if not condition:
        raise Blocked(message)


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def reject_json_constant(_):
    raise Blocked("Non-standard JSON constant")


def parse_json(raw):
    require(len(raw) <= MAX_JSON, "JSON exceeds size limit")
    try:
        result = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_json_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Blocked("Invalid JSON") from exc
    require(isinstance(result, dict), "Expected JSON object")
    return result


def object_field(data, key):
    value = data.get(key)
    require(isinstance(value, dict), f"Missing or invalid object: {key}")
    return value


def pattern_field(data, key, pattern):
    value = data.get(key)
    require(isinstance(value, str) and pattern.fullmatch(value), f"Invalid {key}")
    return value


def positive_int(data, key):
    value = data.get(key)
    require(type(value) is int and value > 0, f"Invalid {key}")
    return value


def measure_stream(stream, limit):
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(BLOCK):
        size += len(block)
        require(size <= limit, "Input exceeds size limit")
        digest.update(block)
    return digest.hexdigest(), size


def hash_stream(stream, limit=MAX_ARCHIVE):
    return measure_stream(stream, limit)[0]


def load_evidence(path):
    with Path(path).open("rb") as stream:
        return parse_json(stream.read(MAX_JSON + 1))


def validate_evidence(evidence):
    """Check schema and claims only; never infer approval or provenance."""
    require(type(evidence.get("schema_version")) is int and evidence["schema_version"] == 1,
            "Unsupported evidence schema")
    require(evidence.get("repository") == REPOSITORY, "Wrong repository")
    release = object_field(evidence, "release")
    positive_int(release, "pr_number")
    for key in ("base_sha", "head_sha", "merge_tree_sha"):
        pattern_field(release, key, SHA)
    workflow = object_field(evidence, "workflow")
    positive_int(workflow, "run_id")
    positive_int(workflow, "run_attempt")
    pattern_field(workflow, "workflow_sha", SHA)
    checks = object_field(evidence, "verification")
    require(all(checks.get(key) == "passed" for key in CHECKS), "Missing passing verification claim")
    require(type(checks.get("postgresql_passed")) is int and checks["postgresql_passed"] == 82,
            "Missing 82 PostgreSQL passes claim")
    image = object_field(evidence, "image")
    require(image.get("platform") == PLATFORM, "Wrong claimed platform")
    require(image.get("archive") == "image.oci.tar", "Unexpected archive name")
    pattern_field(image, "archive_sha256", HEX)
    for key in ("config_digest", "manifest_digest"):
        pattern_field(image, key, DIGEST)
    return image


def artifact_name(run_id, run_attempt):
    positive_int({"run_id": run_id}, "run_id")
    positive_int({"run_attempt": run_attempt}, "run_attempt")
    return f"release-image-{run_id}-{run_attempt}"


def inventory(archive):
    """Accept one OCI image layout; never extract paths to disk."""
    members = {}
    seen = set()
    for item in archive:
        name = item.name.removeprefix("./").rstrip("/")
        require(name not in seen, "Duplicate archive member")
        seen.add(name)
        if item.isdir():
            require(name in (".", "", "blobs", "blobs/sha256"), "Unexpected archive directory")
            continue
        require(item.isfile() and not item.issparse(), "Unexpected archive member type")
        require(name in ("oci-layout", "index.json") or
                (name.startswith("blobs/sha256/") and HEX.fullmatch(name[13:])),
                "Unexpected archive member path")
        members[name] = item
    require("index.json" in members and "oci-layout" in members, "Missing OCI layout")
    return members


def member_stream(archive, members, name):
    require(name in members, "Missing OCI member")
    return archive.extractfile(members[name])


def read_json_member(archive, members, name):
    require(name in members and members[name].size <= MAX_JSON, "Missing or oversized OCI JSON")
    with member_stream(archive, members, name) as stream:
        return parse_json(stream.read(MAX_JSON + 1))


def verify_descriptor(archive, members, descriptor, media_types):
    require(isinstance(descriptor, dict), "Invalid OCI descriptor")
    require(descriptor.get("mediaType") in media_types, "Unsupported OCI media type")
    require(not (set(descriptor) & {"urls", "data"}), "External or embedded descriptor unsupported")
    digest = pattern_field(descriptor, "digest", DIGEST)
    size = descriptor.get("size")
    name = "blobs/sha256/" + digest[7:]
    require(type(size) is int and size >= 0 and name in members and members[name].size == size,
            "OCI descriptor size mismatch or missing blob")
    with member_stream(archive, members, name) as stream:
        require("sha256:" + hash_stream(stream) == digest, "OCI blob digest mismatch")
    return name


def verify_archive(path, evidence):
    """Check archive hash, single manifest, config, layers and uncompressed DiffIDs."""
    image = validate_evidence(evidence)
    try:
        with Path(path).open("rb") as source:
            require(hash_stream(source) == image["archive_sha256"], "Archive SHA-256 mismatch")
            source.seek(0)
            with tarfile.open(fileobj=source, mode="r:") as archive:
                members = inventory(archive)
                layout = read_json_member(archive, members, "oci-layout")
                require(layout.get("imageLayoutVersion") == "1.0.0", "Unsupported OCI layout")
                index = read_json_member(archive, members, "index.json")
                descriptors = index.get("manifests")
                require(type(index.get("schemaVersion")) is int and index["schemaVersion"] == 2 and
                        isinstance(descriptors, list) and len(descriptors) == 1,
                        "Expected exactly one OCI image manifest")
                descriptor = descriptors[0]
                manifest_name = verify_descriptor(archive, members, descriptor, (MANIFEST,))
                require(descriptor["digest"] == image["manifest_digest"], "Manifest digest mismatch")
                if "platform" in descriptor:
                    platform = object_field(descriptor, "platform")
                    require(platform.get("os") == "linux" and platform.get("architecture") == "amd64",
                            "Wrong index platform")
                manifest = read_json_member(archive, members, manifest_name)
                require(type(manifest.get("schemaVersion")) is int and manifest["schemaVersion"] == 2 and
                        manifest.get("mediaType", MANIFEST) == MANIFEST and
                        not (set(manifest) & {"artifactType", "subject"}), "Invalid image manifest")
                config_descriptor = object_field(manifest, "config")
                config_name = verify_descriptor(archive, members, config_descriptor, (CONFIG,))
                require(config_descriptor["digest"] == image["config_digest"], "Config digest mismatch")
                config = read_json_member(archive, members, config_name)
                require(config.get("os") == "linux" and config.get("architecture") == "amd64",
                        "Wrong config platform")
                rootfs = object_field(config, "rootfs")
                diffs = rootfs.get("diff_ids")
                layers = manifest.get("layers")
                require(rootfs.get("type") == "layers" and isinstance(layers, list) and layers and
                        isinstance(diffs, list) and len(layers) == len(diffs), "Missing or invalid image layers")
                referenced = {"oci-layout", "index.json", manifest_name, config_name}
                remaining = MAX_UNCOMPRESSED_LAYERS
                for layer, diff in zip(layers, diffs, strict=True):
                    name = verify_descriptor(archive, members, layer, (LAYER, GZIP))
                    referenced.add(name)
                    require(isinstance(diff, str) and DIGEST.fullmatch(diff), "Invalid layer DiffID")
                    with member_stream(archive, members, name) as stream:
                        if layer["mediaType"] == GZIP:
                            with gzip.GzipFile(fileobj=stream) as uncompressed:
                                actual, size = measure_stream(uncompressed, remaining)
                        else:
                            actual, size = measure_stream(stream, remaining)
                    remaining -= size
                    require("sha256:" + actual == diff, "Layer DiffID mismatch")
                require(set(members) == referenced, "Unexpected unreferenced OCI blob")
    except (OSError, tarfile.TarError, EOFError, ValueError) as exc:
        raise Blocked("Unreadable OCI archive") from exc
    return {"platform": PLATFORM, "manifest_digest": image["manifest_digest"],
            "config_digest": image["config_digest"], "archive_sha256": image["archive_sha256"],
            "layer_count": len(layers), "integrity_verified": True, "publication_authorized": False}


def timestamp(data, key):
    value = data.get(key)
    require(isinstance(value, str), f"Missing timestamp: {key}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Blocked(f"Invalid timestamp: {key}") from exc
    require(result.tzinfo is not None, f"Missing timezone: {key}")
    return result


def same_repo(data):
    return (isinstance(data, dict) and data.get("id") == REPOSITORY_ID
            and data.get("full_name") == REPOSITORY)


def verify_github_metadata(evidence, *, run, artifacts, pr, commit, source_sha,
                           trusted_workflow_shas, now=None):
    """Compare API-shaped records; never accept them as authorization by themselves.

    Caller must obtain these from authenticated GitHub endpoints, not from an
    artifact. The reviewed workflow SHA list must come from outside the candidate.
    Only ordinary two-parent merge commits are supported by this initial contract.
    No network calls or AWS credentials are used here.
    """
    validate_evidence(evidence)
    release, workflow = evidence["release"], evidence["workflow"]
    require(isinstance(source_sha, str) and SHA.fullmatch(source_sha), "Invalid source SHA")
    require(workflow["workflow_sha"] in trusted_workflow_shas, "Untrusted release workflow SHA")
    require(run.get("id") == workflow["run_id"] and
            run.get("run_attempt") == workflow["run_attempt"], "Run or attempt mismatch")
    require(run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main" and
            run.get("path") == RELEASE_WORKFLOW, "Not the official main release workflow")
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "Release workflow did not succeed")
    require(same_repo(run.get("repository")) and same_repo(run.get("head_repository")),
            "Wrong workflow repository")
    require(run.get("head_sha") == workflow["workflow_sha"] == release["base_sha"],
            "Run workflow/base SHA mismatch")
    start = timestamp(run, "run_started_at")
    finish = timestamp(run, "updated_at")
    now = now or datetime.now(timezone.utc)
    require(start <= finish <= now, "Invalid run time window")
    require(isinstance(artifacts, list) and all(isinstance(a, dict) for a in artifacts),
            "Invalid artifact inventory")
    name = artifact_name(workflow["run_id"], workflow["run_attempt"])
    matches = [a for a in artifacts if a.get("name") == name]
    require(len(matches) == 1, "Expected exactly one attempt-specific artifact")
    artifact = matches[0]
    positive_int(artifact, "id")
    positive_int(artifact, "size_in_bytes")
    digest = pattern_field(artifact, "digest", DIGEST)
    require(artifact.get("expired") is False and timestamp(artifact, "expires_at") > now,
            "Artifact expired")
    require(start <= timestamp(artifact, "created_at") <= finish, "Artifact outside run attempt")
    origin = object_field(artifact, "workflow_run")
    require(origin.get("id") == run["id"] and origin.get("repository_id") == REPOSITORY_ID and
            origin.get("head_repository_id") == REPOSITORY_ID and origin.get("head_branch") == "main" and
            origin.get("head_sha") == run["head_sha"], "Artifact origin mismatch")
    require(pr.get("number") == release["pr_number"] and pr.get("merged") is True and
            pr.get("merge_commit_sha") == source_sha, "PR not merged as the requested source")
    base, head = object_field(pr, "base"), object_field(pr, "head")
    require(base.get("ref") == "main" and same_repo(base.get("repo")) and
            same_repo(head.get("repo")) and head.get("sha") == release["head_sha"],
            "PR repository, base branch or head mismatch")
    require(timestamp(pr, "merged_at") >= finish, "PR merged before official verification finished")
    require(commit.get("sha") == source_sha and
            object_field(object_field(commit, "commit"), "tree").get("sha") == release["merge_tree_sha"],
            "Published source tree mismatch")
    parents = commit.get("parents")
    require(isinstance(parents, list) and all(isinstance(p, dict) for p in parents) and
            [p.get("sha") for p in parents] == [release["base_sha"], release["head_sha"]],
            "Exact base/head merge parents required")
    return {"source_sha": source_sha, "source_tree": release["merge_tree_sha"],
            "artifact_id": artifact["id"], "artifact_digest": digest,
            "metadata_verified": True, "publication_authorized": False}


def verify_artifact_zip_hash(path, expected):
    """Check the original downloaded ZIP against the GitHub API digest."""
    require(isinstance(expected, str) and DIGEST.fullmatch(expected), "Invalid artifact digest")
    try:
        with Path(path).open("rb") as stream:
            require("sha256:" + hash_stream(stream) == expected, "Artifact ZIP digest mismatch")
    except OSError as exc:
        raise Blocked("Unreadable artifact ZIP") from exc


"""Read-only GitHub API integration; publication remains explicitly disabled."""
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAPI:
    """Only authenticated GET requests to the fixed repository; never follow redirects."""
    def __init__(self, token):
        require(isinstance(token, str) and token and not any(c.isspace() for c in token),
                "GITHUB_TOKEN is missing or invalid")
        self._token = token
        self._opener = urllib.request.build_opener(NoRedirect())

    def get(self, endpoint):
        require(re.fullmatch(r"/[A-Za-z0-9_./?=&-]+", endpoint) and ".." not in endpoint,
                "Invalid GitHub endpoint")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPOSITORY}{endpoint}",
            headers={"Authorization": "Bearer " + self._token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "toi-ecr-publish"}, method="GET")
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read(MAX_JSON + 1)
            require(len(raw) <= MAX_JSON, "GitHub response exceeds size limit")
            return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_json_constant)
        except urllib.error.HTTPError as exc:
            raise Blocked(f"GitHub API request failed (HTTP {exc.code})") from None
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise Blocked("GitHub API request failed or returned invalid JSON") from None


def api_object(api, endpoint):
    value = api.get(endpoint)
    require(isinstance(value, dict), "Expected GitHub API object")
    return value


def api_pages(api, endpoint, *, key=None):
    """Fetch all pages, rejecting truncated or inconsistent inventories."""
    rows = []
    seen = set()
    expected_count = None
    for page in range(1, 101):
        response = api.get(f"{endpoint}?per_page=100&page={page}")
        if key is not None:
            require(isinstance(response, dict), "Invalid GitHub inventory")
            count = response.get("total_count")
            require(type(count) is int and count >= 0, "Invalid GitHub inventory count")
            if expected_count is None:
                expected_count = count
            require(count == expected_count, "GitHub inventory changed during pagination")
            batch = response.get(key)
        else:
            batch = response
        require(isinstance(batch, list) and len(batch) <= 100, "Invalid GitHub inventory page")
        for row in batch:
            require(isinstance(row, dict), "Invalid GitHub inventory entry")
            identifier = positive_int(row, "id")
            require(identifier not in seen, "Duplicate GitHub inventory entry")
            seen.add(identifier)
            rows.append(row)
        if len(batch) < 100:
            require(expected_count is None or len(rows) == expected_count, "Incomplete GitHub inventory")
            return rows
    raise Blocked("GitHub inventory exceeds pagination limit")


def verify_current_main(api, source_sha):
    main = api_object(api, "/branches/main")
    require(main.get("name") == "main" and main.get("protected") is True and
            object_field(main, "commit").get("sha") == source_sha,
            "Source must be the current protected main HEAD")


def verify_current_run(api, run):
    latest = api_object(api, f"/actions/runs/{positive_int(run, 'id')}")
    require(all(latest.get(key) == run.get(key) for key in RUN_IDENTITY_FIELDS) and
            same_repo(latest.get("repository")) and same_repo(latest.get("head_repository")),
            "Run changed during verification")


def collect_github_records(api, run_id, attempt, pr_number, source_sha):
    for key, value in (("run_id", run_id), ("run_attempt", attempt), ("pr_number", pr_number)):
        positive_int({key: value}, key)
    require(isinstance(source_sha, str) and SHA.fullmatch(source_sha), "Invalid source SHA")
    run_path = f"/actions/runs/{run_id}"
    run = api_object(api, run_path)
    require(run.get("id") == run_id and run.get("run_attempt") == attempt,
            "Requested run is not the latest attempt")
    historical = api_object(api, f"{run_path}/attempts/{attempt}")
    require(all(run.get(key) == historical.get(key) for key in RUN_IDENTITY_FIELDS),
            "Run attempt changed or disagrees")
    require(same_repo(historical.get("repository")) and same_repo(historical.get("head_repository")),
            "Wrong attempt repository")
    artifacts = api_pages(api, f"{run_path}/artifacts", key="artifacts")
    pr = api_object(api, f"/pulls/{pr_number}")
    commit = api_object(api, f"/commits/{source_sha}")
    verify_current_main(api, source_sha)
    reviews = api_pages(api, f"/pulls/{pr_number}/reviews")
    # Recheck latest attempt after pagination, before accepting the inventory.
    verify_current_run(api, run)
    return run, artifacts, pr, commit, reviews


def observe_independent_reviews(pr, reviews, head_sha, run):
    """Provisional observation only; current API reviews cannot prove the full #125 approval contract."""
    author = positive_int(object_field(pr, "user"), "id")
    finish = timestamp(pr, "merged_at")
    run_start = timestamp(run, "run_started_at")
    decisive = {}
    for review in reviews:
        user = object_field(review, "user")
        identifier = positive_int(user, "id")
        state = review.get("state")
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        when = timestamp(review, "submitted_at")
        # Later reviews (including dismissals) supersede earlier decisive records.
        rank = (when, positive_int(review, "id"))
        if identifier not in decisive or rank > decisive[identifier][0]:
            decisive[identifier] = (rank, review)
    approved = sorted(review["id"] for identifier, (rank, review) in decisive.items()
                      if identifier != author and review.get("state") == "APPROVED"
                      and review.get("commit_id") == head_sha and rank[0] <= min(finish, run_start)
                      and review.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"})
    require(approved, "No independent exact-head approval observed before release run")
    return {"independent_review_ids_observed": approved, "approval_contract_verified": False}




def verify_github_zip(api, zip_path, *, run_id, attempt, pr_number, source_sha, trusted_workflow_shas, now=None):
    """Bind the exact API-listed ZIP to its contents; never authorize publication."""
    run, artifacts, pr, commit, reviews = collect_github_records(api, run_id, attempt, pr_number, source_sha)
    matches = [a for a in artifacts if a.get("name") == artifact_name(run_id, attempt)]
    require(len(matches) == 1, "Expected exactly one attempt-specific artifact")
    artifact = matches[0]
    require(artifact.get("expired") is False, "Artifact expired")
    try:
        require(Path(zip_path).stat().st_size <= MAX_ARCHIVE, "Artifact ZIP exceeds size limit")
        # Work only on a private local copy, so the verified bytes are the bytes parsed.
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "artifact.zip"
            with Path(zip_path).open("rb") as src, local.open("xb") as dst:
                while block := src.read(BLOCK):
                    dst.write(block)
                    require(dst.tell() <= MAX_ARCHIVE, "Artifact ZIP exceeds size limit")
            verify_artifact_zip_hash(local, artifact.get("digest"))
            with zipfile.ZipFile(local) as bundle:
                items = bundle.infolist()
                require(len(items) == 2 and {item.filename for item in items} ==
                        {"image.oci.tar", "release-evidence.json"}, "Unexpected artifact ZIP members")
                for item in items:
                    mode = (item.external_attr >> 16) & 0o170000
                    require(not item.is_dir() and mode in (0, 0o100000) and not item.flag_bits & 1,
                            "Unsafe artifact ZIP member")
                require(bundle.getinfo("release-evidence.json").file_size <= MAX_JSON,
                        "Evidence exceeds size limit")
                require(bundle.getinfo("image.oci.tar").file_size <= MAX_ARCHIVE, "OCI archive exceeds size limit")
                with bundle.open("release-evidence.json") as evidence_stream:
                    evidence = parse_json(evidence_stream.read(MAX_JSON + 1))
                validate_evidence(evidence)
                require(evidence["workflow"]["run_id"] == run_id and evidence["workflow"]["run_attempt"] == attempt
                        and evidence["release"]["pr_number"] == pr_number, "Requested release identifiers mismatch")
                result = verify_github_metadata(evidence, run=run, artifacts=artifacts, pr=pr, commit=commit,
                                                source_sha=source_sha, trusted_workflow_shas=trusted_workflow_shas, now=now)
                result.update(observe_independent_reviews(pr, reviews, evidence["release"]["head_sha"], run))
                path = Path(folder) / "image.oci.tar"
                with bundle.open("image.oci.tar") as source, path.open("xb") as destination:
                    while block := source.read(BLOCK):
                        destination.write(block)
                        require(destination.tell() <= MAX_ARCHIVE, "OCI archive exceeds size limit")
                result.update(verify_archive(path, evidence))
                # Hashing large archives can take time. Do not return stale
                # observations if the protected branch or release attempt moved.
                verify_current_run(api, run)
                verify_current_main(api, source_sha)
                return result
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise Blocked("Unreadable artifact ZIP") from exc


# Phase 4: transport and idempotency contract. No live runner or CLI entry
# point is installed until the official release approval boundary is agreed.
AWS_ACCOUNT = "478478097368"
AWS_REGION = "ap-northeast-2"
ECR_NAME = "toi-be"
ECR_ROLE = "toi-github-ecr-publish"
ECR_HOST = f"{AWS_ACCOUNT}.dkr.ecr.{AWS_REGION}.amazonaws.com"
ECR_URI = f"{ECR_HOST}/{ECR_NAME}"
ECR_ARN = f"arn:aws:ecr:{AWS_REGION}:{AWS_ACCOUNT}:repository/{ECR_NAME}"


class ECRTransport:
    """Command protocol exercised with an injected fake runner only in phase 4.

    runner(argv, *, input, timeout) returns a CompletedProcess-like object
    with byte stdout/stderr. No default subprocess implementation exists.
    """

    def __init__(self, runner):
        require(callable(runner), "Explicit test transport required")
        self.runner = runner

    def command(self, argv, *, input_bytes=None, timeout=30):
        try:
            return self.runner(argv, input=input_bytes, timeout=timeout)
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
            raise Blocked("Transport failed or timed out") from None

    def aws(self, service, operation, *args):
        return self.command(["aws", service, operation, *args, "--region", AWS_REGION,
                             "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt"])

    def response(self, result, operation):
        require(result.returncode == 0, f"{operation} failed")
        return parse_json(result.stdout)

    def check_target(self):
        identity = self.response(self.aws("sts", "get-caller-identity"), "AWS identity lookup")
        require(identity.get("Account") == AWS_ACCOUNT, "Wrong AWS account")
        arn = identity.get("Arn")
        require(isinstance(arn, str) and re.fullmatch(
            rf"arn:aws:sts::{AWS_ACCOUNT}:assumed-role/{ECR_ROLE}/[A-Za-z0-9+=,.@_-]{{2,64}}", arn),
            "Wrong AWS assumed role")
        result = self.response(self.aws("ecr", "describe-repositories", "--registry-id", AWS_ACCOUNT,
                                        "--repository-names", ECR_NAME), "ECR repository lookup")
        rows = result.get("repositories")
        require(not result.get("nextToken") and isinstance(rows, list) and len(rows) == 1 and
                isinstance(rows[0], dict), "Unexpected ECR repository inventory")
        repo = rows[0]
        require(repo.get("registryId") == AWS_ACCOUNT and repo.get("repositoryName") == ECR_NAME and
                repo.get("repositoryArn") == ECR_ARN and repo.get("repositoryUri") == ECR_URI,
                "Wrong ECR repository")
        require(repo.get("imageTagMutability") == "IMMUTABLE" and
                not repo.get("imageTagMutabilityExclusionFilters"), "ECR repository is not immutable")

    def lookup(self, source_sha):
        require(isinstance(source_sha, str) and SHA.fullmatch(source_sha), "Invalid source SHA")
        result = self.aws("ecr", "describe-images", "--registry-id", AWS_ACCOUNT,
                          "--repository-name", ECR_NAME, "--image-ids", f"imageTag={source_sha}")
        if result.returncode != 0:
            # Only the explicit service error for a missing image is absence.
            # AccessDenied, repository absence and timeouts never permit a push.
            if re.search(rb"\(ImageNotFoundException\) when calling the DescribeImages operation(?:\s|:)",
                         result.stderr):
                return None
            raise Blocked("ECR image lookup failed")
        response = self.response(result, "ECR image lookup")
        rows = response.get("imageDetails")
        require(not response.get("nextToken") and isinstance(rows, list) and len(rows) == 1 and
                isinstance(rows[0], dict), "Unexpected ECR image inventory")
        row = rows[0]
        tags = row.get("imageTags")
        require(row.get("registryId") == AWS_ACCOUNT and row.get("repositoryName") == ECR_NAME and
                isinstance(tags, list) and source_sha in tags, "ECR image identity mismatch")
        return pattern_field(row, "imageDigest", DIGEST)

    def copy_once(self, archive, source_sha):
        require(isinstance(source_sha, str) and SHA.fullmatch(source_sha), "Invalid source SHA")
        # Credentials go over stdin and a private, short-lived authfile only.
        # Do not print subprocess stdout/stderr or pass credentials as arguments.
        with tempfile.TemporaryDirectory() as folder:
            auth = str(Path(folder) / "auth.json")
            password = self.command(["aws", "ecr", "get-login-password", "--region", AWS_REGION,
                                     "--no-cli-pager", "--no-cli-auto-prompt"])
            require(password.returncode == 0 and isinstance(password.stdout, bytes) and
                    password.stdout.strip(), "ECR login token retrieval failed")
            login = self.command(["skopeo", "login", "--authfile", auth, "--username", "AWS",
                                  "--password-stdin", "--tls-verify=true", ECR_HOST],
                                 input_bytes=password.stdout)
            require(login.returncode == 0, "ECR login failed")
            copied = self.command(["skopeo", "copy", "--preserve-digests", "--dest-tls-verify=true",
                                   "--authfile", auth, f"oci-archive:{archive}",
                                   f"docker://{ECR_URI}:{source_sha}"], timeout=600)
            require(copied.returncode == 0, "Image copy failed")


def simulate_publication(archive, evidence, source_sha, *, runner, pause=lambda _: None):
    """Test-only orchestration; synthetic inputs are NOT publication approval.

    No call to this routine is wired into main(). A future live entrypoint must
    require separately verified approval, not simply reuse this simulation.
    """
    require(isinstance(source_sha, str) and SHA.fullmatch(source_sha), "Invalid source SHA")
    transport = ECRTransport(runner)
    with tempfile.TemporaryDirectory() as folder:
        # Pin bytes for the verification and copy steps, without trusting a
        # caller-controlled path after validation or extracting arbitrary files.
        local = Path(folder) / "image.oci.tar"
        with Path(archive).open("rb") as source, local.open("xb") as dest:
            while block := source.read(BLOCK):
                dest.write(block)
                require(dest.tell() <= MAX_ARCHIVE, "OCI archive exceeds size limit")
        verified = verify_archive(local, evidence)
        expected = verified["manifest_digest"]
        transport.check_target()
        current = transport.lookup(source_sha)
        if current is not None:
            require(current == expected, "Immutable SHA tag digest conflict")
            outcome = "already_present"
        else:
            copied = True
            try:
                transport.copy_once(local, source_sha)
            except Blocked:
                copied = False
            # Never retry a write blindly. Reconcile using up to three reads;
            # a matching remote digest is authoritative even on response loss.
            for attempt in range(3):
                try:
                    current = transport.lookup(source_sha)
                except Blocked:
                    current = None
                if current is not None:
                    require(current == expected, "Immutable SHA tag digest conflict")
                    break
                if attempt < 2:
                    pause(2 ** attempt)
            require(current == expected, "ECR publication unconfirmed; stop and reconcile before retry")
            outcome = "published" if copied else "reconciled"
        return {**verified, "source_sha": source_sha, "ecr_repository_uri": ECR_URI,
                "ecr_digest": current, "outcome": outcome, "simulation": True,
                "publication_authorized": False}


# Phase 5: an explicitly synthetic record, never a deployable evidence file.
SIMULATION_EVIDENCE = "image-publish-evidence.simulation.json"
PUBLICATION_OUTCOMES = {"published", "already_present", "reconciled"}


def simulation_evidence(evidence, result, *, publish_run_id, publish_run_attempt,
                        publish_workflow_sha, confirmed_at):
    """Build an allowlisted record from a confirmed mock outcome.

    Release fields are claims until the official #125 approval contract is
    verified. This function cannot create production evidence.
    """
    image = validate_evidence(evidence)
    require(result.get("simulation") is True and result.get("publication_authorized") is False,
            "Only simulation results are supported")
    require(result.get("integrity_verified") is True and result.get("outcome") in PUBLICATION_OUTCOMES,
            "Publication result unconfirmed")
    for key in ("archive_sha256", "config_digest", "manifest_digest", "platform"):
        require(result.get(key) == image[key], f"Publication {key} mismatch")
    require(result.get("ecr_digest") == image["manifest_digest"] and
            result.get("ecr_repository_uri") == ECR_URI, "ECR confirmation mismatch")
    source_sha = pattern_field(result, "source_sha", SHA)
    context = {"run_id": publish_run_id, "run_attempt": publish_run_attempt,
               "workflow_sha": publish_workflow_sha}
    positive_int(context, "run_id")
    positive_int(context, "run_attempt")
    pattern_field(context, "workflow_sha", SHA)
    require(isinstance(confirmed_at, datetime) and confirmed_at.utcoffset() is not None,
            "Confirmation time must include timezone")
    release, workflow = evidence["release"], evidence["workflow"]
    return {
        "schema_version": 1,
        "record_type": "simulation",
        "simulation": True,
        "publication_authorized": False,
        "deployable": False,
        "repository": REPOSITORY,
        "source": {"sha": source_sha, "tree": release["merge_tree_sha"],
                   "tree_verified": False},
        "release": {"pr_number": release["pr_number"], "base_sha": release["base_sha"],
                    "head_sha": release["head_sha"], "merge_tree_sha": release["merge_tree_sha"],
                    "approval_contract_verified": False},
        "release_workflow": {key: workflow[key] for key in ("run_id", "run_attempt", "workflow_sha")},
        "image": {key: image[key] for key in ("platform", "archive_sha256", "config_digest", "manifest_digest")},
        "ecr": {"account_id": AWS_ACCOUNT, "region": AWS_REGION, "repository_uri": ECR_URI,
                "tag": source_sha, "digest": result["ecr_digest"],
                "digest_uri": f"{ECR_URI}@{result['ecr_digest']}", "remote_verified": False},
        "publish_workflow": context,
        "outcome": result["outcome"],
        "confirmed_at": confirmed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "confirmation_scope": "mock_transport_only",
    }


def simulate_with_evidence(archive, evidence, source_sha, output_directory, *, runner,
                           publish_run_id, publish_run_attempt, publish_workflow_sha,
                           pause=lambda _: None, clock=lambda: datetime.now(timezone.utc)):
    """Exercise publication and evidence persistence with an injected mock.

    The directory must exist; never overwrite a previous record. A failed
    publication produces no new file. Not exposed through the live CLI.
    """
    output = Path(output_directory) / SIMULATION_EVIDENCE
    require(output.parent.is_dir(), "Evidence output directory missing")
    require(not output.exists() and not output.is_symlink(), "Evidence already exists")
    context = {"run_id": publish_run_id, "run_attempt": publish_run_attempt,
               "workflow_sha": publish_workflow_sha}
    positive_int(context, "run_id")
    positive_int(context, "run_attempt")
    pattern_field(context, "workflow_sha", SHA)
    result = simulate_publication(archive, evidence, source_sha, runner=runner, pause=pause)
    record = simulation_evidence(evidence, result, publish_run_id=publish_run_id,
                                 publish_run_attempt=publish_run_attempt,
                                 publish_workflow_sha=publish_workflow_sha, confirmed_at=clock())
    payload = (json.dumps(record, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    # Write privately, then install atomically without replacing an existing file.
    # Same directory ensures the temporary file and destination share a filesystem.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".publish-evidence-",
                                         delete=False) as stream:
            temp = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, output)
    except OSError:
        raise Blocked("Could not persist simulation evidence") from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return record



def download_release_zip(api, path, *, token, run_id, attempt, trusted_workflow_shas,
                         runner=subprocess.run, now=None):
    """Download the exact attempt-specific ZIP before the full read-only validation.

    curl follows GitHub's signed HTTPS redirect without forwarding Authorization
    to a different host. Never pass the token in argv or log curl output.
    """
    positive_int({"run_id": run_id}, "run_id")
    positive_int({"run_attempt": attempt}, "run_attempt")
    require(isinstance(token, str) and token and not any(c.isspace() for c in token),
            "Invalid GitHub token")
    run = api_object(api, f"/actions/runs/{run_id}")
    require(run.get("id") == run_id and run.get("run_attempt") == attempt and
            run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main" and
            run.get("path") == RELEASE_WORKFLOW and run.get("status") == "completed" and
            run.get("conclusion") == "success" and same_repo(run.get("repository")) and
            same_repo(run.get("head_repository")) and run.get("head_sha") in trusted_workflow_shas,
            "Requested run is not a trusted successful release attempt")
    rows = api_pages(api, f"/actions/runs/{run_id}/artifacts", key="artifacts")
    matches = [a for a in rows if a.get("name") == artifact_name(run_id, attempt)]
    require(len(matches) == 1, "Expected exactly one attempt-specific artifact")
    item = matches[0]
    artifact_id = positive_int(item, "id")
    size = positive_int(item, "size_in_bytes")
    expected = pattern_field(item, "digest", DIGEST)
    now = now or datetime.now(timezone.utc)
    origin = object_field(item, "workflow_run")
    require(item.get("expired") is False and timestamp(item, "expires_at") > now and
            timestamp(run, "run_started_at") <= timestamp(item, "created_at") <= timestamp(run, "updated_at") and
            size <= MAX_ARCHIVE and origin.get("id") == run_id and
            origin.get("repository_id") == REPOSITORY_ID and
            origin.get("head_repository_id") == REPOSITORY_ID and
            origin.get("head_branch") == "main" and origin.get("head_sha") == run.get("head_sha"),
            "Artifact expired, oversized or origin mismatch")
    path = Path(path)
    require(not path.exists() and not path.is_symlink(), "Download target already exists")
    # A concurrent creator must never lose its file if exclusive creation fails.
    try:
        with path.open("xb"):
            pass
    except OSError:
        raise Blocked("Could not reserve download target") from None
    try:
        result = runner(["curl", "--disable", "--fail", "--silent", "--location",
                         "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "3",
                         "--connect-timeout", "15", "--max-time", "600", "--max-filesize", str(MAX_ARCHIVE),
                         "--header", "@-", "--output", str(path),
                         f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip"],
                        input=("Authorization: Bearer " + token + "\nAccept: application/vnd.github+json\n"
                               "X-GitHub-Api-Version: 2022-11-28\n").encode(),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=610)
        require(result.returncode == 0 and 0 < path.stat().st_size <= MAX_ARCHIVE,
                "Artifact download failed or exceeded size limit")
        verify_artifact_zip_hash(path, expected)
    except (OSError, subprocess.TimeoutExpired, Blocked):
        path.unlink(missing_ok=True)
        raise Blocked("Artifact download or digest verification failed") from None
    return artifact_id

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--artifact-zip", type=Path)
    parser.add_argument("--download-artifact", action="store_true",
                        help="Download the official attempt ZIP and perform read-only verification")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--source-sha")
    parser.add_argument("--trusted-workflow-sha", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.artifact_zip is not None or args.download_artifact:
            require(args.evidence is None and args.archive is None, "Choose one verification mode")
            require(args.trusted_workflow_sha and all(SHA.fullmatch(s) for s in args.trusted_workflow_sha),
                    "Independently reviewed workflow SHA required")
            require(not (args.download_artifact and args.artifact_zip is not None), "Choose one artifact source")
            for key, value in (("run_id", args.run_id), ("run_attempt", args.run_attempt), ("pr_number", args.pr_number)):
                positive_int({key: value}, key)
            require(isinstance(args.source_sha, str) and SHA.fullmatch(args.source_sha), "Invalid source SHA")
            token = os.environ.get("GITHUB_TOKEN")
            api = GitHubAPI(token)
            options = dict(run_id=args.run_id, attempt=args.run_attempt, pr_number=args.pr_number,
                           source_sha=args.source_sha, trusted_workflow_shas=set(args.trusted_workflow_sha))
            if args.download_artifact:
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "artifact.zip"
                    download_release_zip(api, path, token=token, run_id=args.run_id, attempt=args.run_attempt,
                                         trusted_workflow_shas=options["trusted_workflow_shas"])
                    result = verify_github_zip(api, path, **options)
            else:
                result = verify_github_zip(api, args.artifact_zip, **options)
        else:
            require(args.evidence is not None and args.archive is not None, "Evidence and archive required")
            require(not any((args.run_id, args.run_attempt, args.pr_number, args.source_sha, args.trusted_workflow_sha)),
                    "GitHub options require --artifact-zip")
            result = verify_archive(args.archive, load_evidence(args.evidence))
    except (Blocked, OSError) as exc:
        print(f"Verification blocked: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
