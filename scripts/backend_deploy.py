#!/usr/bin/env python3
"""Validate and run the SSM backend deployment from protected main (#142).

``validate`` runs without AWS access: dispatcher, inputs, repository settings,
the ECR publish run and its evidence. ``execute`` runs with the dedicated
deploy role: AWS identity, the ECR image by digest, ToI-DeploymentStatus and,
only for ``operation=deploy``, ToI-DeployBackend.

Raw SSM output, AWS error text and credentials are never printed. A
SendCommand call whose response is lost is never repeated; the submitted
command is looked up by its unique comment instead.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

REPOSITORY = "ToI-walkwithdog/walkwithdog-BE"
REPOSITORY_ID = 1409697696
WORKFLOW = ".github/workflows/backend-deploy.yml"
PUBLISH_WORKFLOW = ".github/workflows/ecr-image-publish.yml"
PUBLISH_WORKFLOW_NAME = "ECR Image Publish"
PUBLISH_JOB_NAME = "Publish image"
EVIDENCE_FILE = "image-publish-evidence.json"
# Same JSON limit as the existing evidence consumer in scripts/ecr_image_publish.py.
MAX_JSON = 8 * 1024 * 1024
MAX_SOURCE_FILE = 8 * 1024 * 1024
CONFIRMED_SLACK = timedelta(seconds=60)
# Reviewed and administrator-approved ECR publisher versions: {path: sha256 of the bytes}.
# Every entry must cover PUBLISHER_FILES. Values are SHA-256 of the raw git blob bytes
# (`git cat-file blob <rev>:<path> | sha256sum`), never of a CRLF working copy.
TRUSTED_PUBLISHERS = (
    # PR #149, independently reviewed Head 312ea97c634ebc81ed23042131fafe77b7021253 (main 6d7b2a0).
    {
        PUBLISH_WORKFLOW: "9f006f13d36775f2e470026fcc1432208cae56b2f482e6cc3a1a079e0f692519",
        "scripts/ecr_image_publish.py": "39741575ad3dc1454f9e72769fbbcbc747fac9afd0333cd03553f474affc1829",
    },
)
PUBLISHER_FILES = frozenset({PUBLISH_WORKFLOW, "scripts/ecr_image_publish.py"})
RELEASE_VERIFIER_FILES = frozenset({
    ".github/workflows/release-ci.yml", ".github/release-ci-trust.json",
    "scripts/release_ci.py", ".github/tests/test_release_ci.py",
})
AWS_ACCOUNT = "478478097368"
REGION = "ap-northeast-2"
ECR_REPOSITORY = "toi-be"
ROLE_NAME = "toi-github-ssm-deploy"
# Pinned SSM document versions. Changing them is a reviewed workflow change.
DEPLOY_DOCUMENT = ("ToI-DeployBackend", "1")
STATUS_DOCUMENT = ("ToI-DeploymentStatus", "1")
OPERATIONS = ("preflight", "deploy")
COMMENT_PREFIX = "toi-backend-deploy"

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
NUMBER = re.compile(r"[1-9][0-9]{0,18}")
ATTEMPT = re.compile(r"[1-9][0-9]{0,8}")
ACCOUNT = re.compile(r"[0-9]{12}")
INSTANCE = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
COMMAND_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
AWS_ERROR = re.compile(r"An error occurred \(([A-Za-z0-9.]{1,64})\)")

STATE_LINE = re.compile(
    r"status=(ready|in_progress|blocked) current=(-|sha256:[0-9a-f]{64}) "
    r"previous=(-|sha256:[0-9a-f]{64}) last_result=(none|running|success|unchanged|failed)")
SERVICE_LINE = re.compile(
    r"(api|db) (?:absent|[0-9a-f]{64} sha256:[0-9a-f]{64} "
    r"(created|restarting|running|removing|paused|exited|dead) (starting|healthy|unhealthy|none))")
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  /opt/toi/scripts/(deploy|rollback|deployment_common)\.sh")

SSM_STATUSES = ("Pending", "InProgress", "Delayed", "Success", "Cancelled", "TimedOut", "Failed", "Cancelling")
TERMINAL = frozenset({"Success", "Cancelled", "TimedOut", "Failed"})
# docs/deployment/RUNBOOK.md on dev: deploy.sh exit codes.
EXIT_MEANINGS = {
    0: "new digest verified or same digest kept",
    2: "input format or argument count error",
    3: "user, installed files, Compose configuration or local Docker error",
    4: "another deployment or rollback holds the lock",
    5: "state file, containers or previous image disagree with the record; reconciliation required",
    6: "ECR authentication, exact digest pull or image platform check failed",
    7: "DB readiness, compatibility evidence or same-digest health check failed",
    8: "deployment failed; the previous healthy service was restored",
    9: "first deployment failed, recovery failed, state write error or interrupted",
}

STATUS_LIMIT = 150      # ToI-DeploymentStatus runs at most 60 seconds on the host.
DEPLOY_LIMIT = 1080     # ToI-DeployBackend runs at most 900 seconds on the host.
DELIVERY_TIMEOUT = "120"
POLL_INTERVAL = 5
LOOKUP_DELAYS = (0, 5, 10)


class Blocked(Exception):
    pass


class AwsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, message):
    if not condition:
        raise Blocked(message)


def field(env, name, pattern, label):
    value = env.get(name, "")
    require(isinstance(value, str) and pattern.fullmatch(value), f"Invalid or missing {label}")
    return value


# ---------------------------------------------------------------- GitHub side

def _request(path, token, accept):
    return urllib.request.Request("https://api.github.com/" + path, headers={
        "Authorization": f"Bearer {token}", "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "toi-backend-deploy",
    })


def _read(opener, request, limit):
    try:
        with opener(request, timeout=60) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise Blocked(f"GitHub API request failed (HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise Blocked("GitHub API is unreachable or returned an invalid response") from None
    require(len(data) <= limit, "GitHub API response exceeds the size limit")
    return data


def api(path, token):
    data = _read(urllib.request.urlopen, _request(path, token, "application/vnd.github+json"), MAX_JSON)
    try:
        return json.loads(data)
    except ValueError:
        raise Blocked("GitHub API returned invalid JSON") from None


def api_raw(path, token):
    """File bytes at a commit through the contents API."""
    return _read(urllib.request.urlopen, _request(path, token, "application/vnd.github.raw+json"), MAX_SOURCE_FILE)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_artifact(artifact_id, token):
    """Download an artifact ZIP; the signed storage URL is fetched without the API token."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(_request(f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip", token,
                             "application/vnd.github+json"), timeout=30).close()
        raise Blocked("Artifact download did not redirect to storage")
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location") if error.code in (301, 302, 303, 307, 308) else None
        require(location, f"GitHub API artifact download failed (HTTP {error.code})")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise Blocked("GitHub API is unreachable or returned an invalid response") from None
    require(urllib.parse.urlsplit(location).scheme == "https", "Artifact storage URL is not HTTPS")
    request = urllib.request.Request(location, headers={"User-Agent": "toi-backend-deploy"})
    return _read(urllib.request.urlopen, request, MAX_JSON)


def check_dispatch(env):
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_REPOSITORY") == REPOSITORY
            and env.get("GITHUB_WORKFLOW_REF") == f"{REPOSITORY}/{WORKFLOW}@refs/heads/main",
            "Deployment must be dispatched from protected main")
    actor = env.get("GITHUB_ACTOR", "")
    require(LOGIN.fullmatch(actor) and env.get("GITHUB_TRIGGERING_ACTOR") == actor,
            "The dispatcher must be the triggering actor")
    field(env, "GITHUB_SHA", SHA, "workflow commit")
    return actor


def check_admin(permission, actor):
    require(isinstance(permission, dict) and (permission.get("user") or {}).get("login") == actor
            and permission.get("permission") == "admin",
            "Only repository administrators may dispatch the deployment")


def check_inputs(env):
    operation = env.get("OPERATION")
    require(operation in OPERATIONS, "operation must be preflight or deploy")
    return {
        "operation": operation,
        "run_id": int(field(env, "PUBLISH_RUN_ID", NUMBER, "publish_run_id")),
        "run_attempt": int(field(env, "PUBLISH_RUN_ATTEMPT", ATTEMPT, "publish_run_attempt")),
        "digest": field(env, "IMAGE_DIGEST", DIGEST, "image_digest"),
    }


def check_settings(env):
    require(env.get("AWS_REGION") == REGION, f"AWS_REGION must be {REGION}")
    require(env.get("ECR_REPOSITORY") == ECR_REPOSITORY, f"ECR_REPOSITORY must be {ECR_REPOSITORY}")
    account = field(env, "AWS_ACCOUNT_ID", ACCOUNT, "AWS_ACCOUNT_ID")
    require(account == AWS_ACCOUNT, f"AWS_ACCOUNT_ID must be {AWS_ACCOUNT}")
    instance = field(env, "EC2_INSTANCE_ID", INSTANCE, "EC2_INSTANCE_ID")
    require(env.get("SSM_DEPLOY_ROLE_ARN") == f"arn:aws:iam::{account}:role/{ROLE_NAME}",
            f"SSM_DEPLOY_ROLE_ARN must name the dedicated {ROLE_NAME} role")
    return {"account": account, "instance": instance}


def check_publish_run(run, latest, run_id, run_attempt):
    for item in (run, latest):
        require(isinstance(item, dict), "Publish run metadata is missing")
        repository = item.get("repository") or {}
        require(item.get("id") == run_id
                and repository.get("full_name") == REPOSITORY and repository.get("id") == REPOSITORY_ID
                and (item.get("head_repository") or {}).get("full_name") == REPOSITORY,
                "Publish run belongs to another run or repository")
        require(item.get("path") == PUBLISH_WORKFLOW and item.get("name") == PUBLISH_WORKFLOW_NAME
                and item.get("event") == "workflow_dispatch" and item.get("head_branch") == "main",
                "Publish run is not an ECR Image Publish dispatch on main")
    actor = (run.get("actor") or {}).get("login")
    require(isinstance(actor, str) and LOGIN.fullmatch(actor)
            and (run.get("triggering_actor") or {}).get("login") == actor,
            "Publish run actor and triggering actor differ")
    require(run.get("run_attempt") == run_attempt and latest.get("run_attempt") == run_attempt,
            "Only the latest attempt of the publish run is accepted")
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "Publish run did not succeed")
    source_sha = run.get("head_sha")
    require(isinstance(source_sha, str) and SHA.fullmatch(source_sha) and latest.get("head_sha") == source_sha,
            "Publish run has no consistent source commit")
    return source_sha


def check_source_on_main(compare):
    require(isinstance(compare, dict) and compare.get("status") in ("ahead", "identical"),
            "Published source commit is not contained in protected main")


# ------------------------------------------- ECR publish evidence (contract v1)

def parse_time(value, label):
    require(isinstance(value, str) and UTC_TIME.fullmatch(value), f"Invalid {label}")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Blocked(f"Invalid {label}") from None


def check_publish_job(jobs, run_id, run_attempt, head_sha):
    items = jobs.get("jobs") if isinstance(jobs, dict) else None
    require(isinstance(items, list), "Publish run jobs are missing")
    matches = [job for job in items if isinstance(job, dict) and job.get("name") == PUBLISH_JOB_NAME]
    require(len(matches) == 1, f"Expected exactly one '{PUBLISH_JOB_NAME}' job in the publish attempt")
    job = matches[0]
    require(job.get("run_id") == run_id and job.get("run_attempt") == run_attempt
            and job.get("head_sha") == head_sha and job.get("status") == "completed"
            and job.get("conclusion") == "success",
            f"The '{PUBLISH_JOB_NAME}' job of this attempt did not succeed")
    parse_time(job.get("started_at"), "publish job start time")
    parse_time(job.get("completed_at"), "publish job completion time")
    return job


def artifact_name(run_id, run_attempt):
    return f"image-publish-{run_id}-{run_attempt}"


def check_artifact(listing, run_id, run_attempt, head_sha):
    items = listing.get("artifacts") if isinstance(listing, dict) else None
    require(isinstance(items, list), "Publish run artifacts are missing")
    name = artifact_name(run_id, run_attempt)
    matches = [item for item in items if isinstance(item, dict) and item.get("name") == name]
    require(len(matches) == 1, f"Expected exactly one artifact named {name}")
    artifact = matches[0]
    origin = artifact.get("workflow_run") or {}
    require(artifact.get("expired") is False, "Publish evidence artifact has expired")
    require(origin.get("id") == run_id and origin.get("repository_id") == REPOSITORY_ID
            and origin.get("head_repository_id") == REPOSITORY_ID
            and origin.get("head_sha") == head_sha and origin.get("head_branch") == "main",
            "Publish evidence artifact comes from another run or repository")
    require(type(artifact.get("id")) is int and artifact["id"] > 0, "Invalid publish evidence artifact ID")
    require(isinstance(artifact.get("digest"), str) and DIGEST.fullmatch(artifact["digest"]),
            "Publish evidence artifact has no SHA-256 digest")
    size = artifact.get("size_in_bytes")
    require(type(size) is int and 0 < size <= MAX_JSON, "Publish evidence artifact size is out of range")
    return artifact


def read_evidence_zip(data, expected_digest):
    require(isinstance(data, bytes) and len(data) <= MAX_JSON, "Publish evidence ZIP is too large")
    require("sha256:" + hashlib.sha256(data).hexdigest() == expected_digest,
            "Publish evidence ZIP does not match the artifact digest")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            require(len(infos) == 1 and infos[0].filename == EVIDENCE_FILE and not infos[0].is_dir()
                    and not infos[0].flag_bits & 0x1,
                    f"Publish evidence ZIP must contain only {EVIDENCE_FILE} at its root")
            require(infos[0].file_size <= MAX_JSON, "Publish evidence JSON is too large")
            with archive.open(infos[0]) as handle:
                content = handle.read(MAX_JSON + 1)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, ValueError, EOFError, OSError):
        raise Blocked("Publish evidence ZIP is corrupt") from None
    require(len(content) == infos[0].file_size, "Publish evidence JSON size does not match the ZIP entry")
    return content


def strict_json(data):
    def pairs(items):
        keys = [key for key, _ in items]
        require(len(keys) == len(set(keys)), "Publish evidence has a duplicate JSON key")
        return dict(items)

    def constant(_name):
        raise Blocked("Publish evidence has a non-finite number")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Blocked("Publish evidence is not valid UTF-8 JSON") from None


def section(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), f"Evidence {label} has unexpected fields")
    return value


def text(value, pattern, label):
    require(isinstance(value, str) and pattern.fullmatch(value), f"Evidence {label} has an invalid value")
    return value


def positive(value, label):
    require(type(value) is int and value > 0, f"Evidence {label} must be a positive integer")
    return value


def exactly(value, expected, label):
    require(type(value) is type(expected) and value == expected, f"Evidence {label} must be {expected!r}")


def check_evidence_document(doc):
    """Schema v1 shape and production markers. Values are compared with GitHub and ECR separately."""
    section(doc, ("schema_version", "record_type", "simulation", "publication_authorized", "deployable",
                  "repository", "source", "release", "release_workflow", "approval", "verifier",
                  "release_artifact", "verification", "image", "ecr", "publish_workflow", "outcome",
                  "confirmed_at"), "document")
    exactly(doc["schema_version"], 1, "schema_version")
    exactly(doc["record_type"], "production", "record_type")
    exactly(doc["simulation"], False, "simulation")
    exactly(doc["publication_authorized"], True, "publication_authorized")
    exactly(doc["deployable"], True, "deployable")
    exactly(doc["repository"], REPOSITORY, "repository")
    source = section(doc["source"], ("sha", "tree"), "source")
    text(source["sha"], SHA, "source.sha")
    text(source["tree"], SHA, "source.tree")
    release = section(doc["release"], ("pr_number", "base_sha", "head_sha", "merge_tree_sha", "change_digest"),
                      "release")
    positive(release["pr_number"], "release.pr_number")
    for key in ("base_sha", "head_sha", "merge_tree_sha"):
        text(release[key], SHA, f"release.{key}")
    text(release["change_digest"], HEX64, "release.change_digest")
    release_run = section(doc["release_workflow"], ("run_id", "run_attempt", "workflow_sha", "dispatcher",
                                                    "official_job_id"), "release_workflow")
    for key in ("run_id", "run_attempt", "official_job_id"):
        positive(release_run[key], f"release_workflow.{key}")
    text(release_run["workflow_sha"], SHA, "release_workflow.workflow_sha")
    text(release_run["dispatcher"], LOGIN, "release_workflow.dispatcher")
    approval = section(doc["approval"], ("comment_id", "approver", "record_sha256", "review"), "approval")
    positive(approval["comment_id"], "approval.comment_id")
    text(approval["approver"], LOGIN, "approval.approver")
    text(approval["record_sha256"], HEX64, "approval.record_sha256")
    review = section(approval["review"], ("head", "record", "blocking"), "approval.review")
    text(review["head"], SHA, "approval.review.head")
    require(isinstance(review["record"], str) and review["record"].startswith(f"https://github.com/{REPOSITORY}/"),
            "Evidence approval.review.record must cite this repository")
    exactly(review["blocking"], 0, "approval.review.blocking")
    files = section(section(doc["verifier"], ("files",), "verifier")["files"], RELEASE_VERIFIER_FILES,
                    "verifier.files")
    for path, digest in files.items():
        text(digest, HEX64, f"verifier.files[{path}]")
    release_artifact = section(doc["release_artifact"], ("id", "digest", "name"), "release_artifact")
    positive(release_artifact["id"], "release_artifact.id")
    text(release_artifact["digest"], DIGEST, "release_artifact.digest")
    verification = section(doc["verification"], ("regression", "migration", "postgresql", "container_smoke",
                                                  "image_security", "postgresql_passed"), "verification")
    for key in ("regression", "migration", "postgresql", "container_smoke", "image_security"):
        exactly(verification[key], "passed", f"verification.{key}")
    exactly(verification["postgresql_passed"], 82, "verification.postgresql_passed")
    image = section(doc["image"], ("platform", "archive_sha256", "config_digest", "manifest_digest"), "image")
    exactly(image["platform"], "linux/amd64", "image.platform")
    text(image["archive_sha256"], HEX64, "image.archive_sha256")
    text(image["config_digest"], DIGEST, "image.config_digest")
    text(image["manifest_digest"], DIGEST, "image.manifest_digest")
    ecr = section(doc["ecr"], ("account_id", "region", "repository_uri", "tag", "digest", "digest_uri",
                               "remote_verified"), "ecr")
    exactly(ecr["account_id"], AWS_ACCOUNT, "ecr.account_id")
    exactly(ecr["region"], REGION, "ecr.region")
    exactly(ecr["repository_uri"], repository_uri(), "ecr.repository_uri")
    text(ecr["tag"], SHA, "ecr.tag")
    text(ecr["digest"], DIGEST, "ecr.digest")
    exactly(ecr["remote_verified"], True, "ecr.remote_verified")
    publish = section(doc["publish_workflow"], ("run_id", "run_attempt", "workflow_sha", "dispatcher"),
                      "publish_workflow")
    positive(publish["run_id"], "publish_workflow.run_id")
    positive(publish["run_attempt"], "publish_workflow.run_attempt")
    text(publish["workflow_sha"], SHA, "publish_workflow.workflow_sha")
    text(publish["dispatcher"], LOGIN, "publish_workflow.dispatcher")
    require(doc["outcome"] in ("published", "already_present", "reconciled"), "Evidence outcome is not accepted")
    parse_time(doc["confirmed_at"], "confirmed_at")


def repository_uri():
    return f"{AWS_ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPOSITORY}"


def check_evidence_relations(doc, *, run_id, run_attempt, run, job, digest):
    source, release, ecr, publish = doc["source"], doc["release"], doc["ecr"], doc["publish_workflow"]
    release_run = doc["release_workflow"]
    relations = (
        (source["sha"] == ecr["tag"], "source.sha must equal ecr.tag"),
        (source["tree"] == release["merge_tree_sha"], "source.tree must equal release.merge_tree_sha"),
        (publish["workflow_sha"] == source["sha"], "publish_workflow.workflow_sha must equal source.sha"),
        (publish["workflow_sha"] == run.get("head_sha"), "publish_workflow.workflow_sha must equal the run head"),
        (publish["run_id"] == run_id, "publish_workflow.run_id must equal the requested run"),
        (publish["run_attempt"] == run_attempt, "publish_workflow.run_attempt must equal the requested attempt"),
        (publish["dispatcher"] == (run.get("actor") or {}).get("login"),
         "publish_workflow.dispatcher must equal the run actor"),
        (release_run["workflow_sha"] == release["base_sha"], "release_workflow.workflow_sha must equal release.base_sha"),
        (doc["approval"]["review"]["head"] == release["head_sha"], "approval.review.head must equal release.head_sha"),
        (doc["image"]["manifest_digest"] == ecr["digest"], "image.manifest_digest must equal ecr.digest"),
        (ecr["digest_uri"] == f"{ecr['repository_uri']}@{ecr['digest']}", "ecr.digest_uri must use ecr.digest"),
        (ecr["digest"] == digest, "ecr.digest must equal the requested image digest"),
        (doc["release_artifact"]["name"] == f"release-image-{release_run['run_id']}-{release_run['run_attempt']}",
         "release_artifact.name must match release_workflow"),
    )
    for holds, message in relations:
        require(holds, "Evidence mismatch: " + message)
    confirmed = parse_time(doc["confirmed_at"], "confirmed_at")
    started = parse_time(job["started_at"], "publish job start time")
    completed = parse_time(job["completed_at"], "publish job completion time")
    require(started - CONFIRMED_SLACK <= confirmed <= completed + CONFIRMED_SLACK,
            "Evidence confirmed_at is outside the publish job execution window")


def check_release_merge(doc, commit, pr):
    source, release = doc["source"], doc["release"]
    parents = [(parent or {}).get("sha") for parent in (commit.get("parents") or [])] \
        if isinstance(commit, dict) else []
    require(isinstance(commit, dict) and commit.get("sha") == source["sha"]
            and (commit.get("tree") or {}).get("sha") == source["tree"],
            "Source commit tree does not match the evidence")
    require(parents == [release["base_sha"], release["head_sha"]],
            "Source commit parents must be the approved release base and head")
    require(isinstance(pr, dict) and pr.get("number") == release["pr_number"] and pr.get("merged") is True
            and pr.get("merge_commit_sha") == source["sha"]
            and (pr.get("base") or {}).get("ref") == "main"
            and ((pr.get("base") or {}).get("repo") or {}).get("full_name") == REPOSITORY
            and (pr.get("head") or {}).get("sha") == release["head_sha"],
            "Release PR is not merged into main as the evidence source commit")


def check_publisher(read_file, head_sha, trusted):
    """Compare the publisher files at the publish commit with reviewed byte hashes."""
    require(trusted, "No reviewed ECR publisher hashes are registered; deployment stays blocked")
    for entry in trusted:
        require(isinstance(entry, dict) and PUBLISHER_FILES <= set(entry)
                and all(isinstance(value, str) and HEX64.fullmatch(value) for value in entry.values()),
                "Invalid reviewed ECR publisher entry")
    actual = {}

    def matches(entry):
        # Each reviewed version is checked on its own files; a file it lacks only fails that version.
        for path, value in sorted(entry.items()):
            if path not in actual:
                try:
                    actual[path] = hashlib.sha256(read_file(path, head_sha)).hexdigest()
                except Blocked:
                    actual[path] = None
            if actual[path] != value:
                return False
        return True
    require(any(matches(entry) for entry in trusted),
            "ECR publisher files at the publish commit do not match a reviewed version")


def verify_evidence(*, fetch, token, run, job, run_id, run_attempt, digest, account,
                    read_file=None, download=None, trusted=None):
    """Verify the ECR publish evidence artifact of the requested run against GitHub records."""
    trusted = TRUSTED_PUBLISHERS if trusted is None else trusted
    download = download or (lambda artifact_id: download_artifact(artifact_id, token))
    read_file = read_file or (lambda path, ref: api_raw(
        f"repos/{REPOSITORY}/contents/{urllib.parse.quote(path)}?ref={ref}", token))
    require(account == AWS_ACCOUNT, "Unexpected AWS account")
    head_sha = run["head_sha"]
    check_publisher(read_file, head_sha, trusted)
    listing = fetch(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts"
                    f"?name={artifact_name(run_id, run_attempt)}&per_page=100", token)
    artifact = check_artifact(listing, run_id, run_attempt, head_sha)
    doc = strict_json(read_evidence_zip(download(artifact["id"]), artifact["digest"]))
    check_evidence_document(doc)
    check_evidence_relations(doc, run_id=run_id, run_attempt=run_attempt, run=run, job=job, digest=digest)
    check_release_merge(doc, fetch(f"repos/{REPOSITORY}/git/commits/{head_sha}", token),
                        fetch(f"repos/{REPOSITORY}/pulls/{doc['release']['pr_number']}", token))
    return {"image_digest": doc["ecr"]["digest"], "source_sha": doc["source"]["sha"],
            "digest_uri": doc["ecr"]["digest_uri"]}


def write_outputs(env, values):
    path = env.get("GITHUB_OUTPUT")
    require(path, "GITHUB_OUTPUT is not available")
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def validate(env, fetch=api, verify_evidence=verify_evidence):
    actor = check_dispatch(env)
    inputs = check_inputs(env)
    settings = check_settings(env)
    token = env.get("GH_TOKEN", "")
    require(token, "GH_TOKEN is not available")
    check_admin(fetch(f"repos/{REPOSITORY}/collaborators/{actor}/permission", token), actor)
    run_id, attempt = inputs["run_id"], inputs["run_attempt"]
    run = fetch(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}", token)
    latest = fetch(f"repos/{REPOSITORY}/actions/runs/{run_id}", token)
    source_sha = check_publish_run(run, latest, run_id, attempt)
    publisher = run["actor"]["login"]
    check_admin(fetch(f"repos/{REPOSITORY}/collaborators/{publisher}/permission", token), publisher)
    job = check_publish_job(fetch(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                                  "?per_page=100", token), run_id, attempt, source_sha)
    check_source_on_main(fetch(f"repos/{REPOSITORY}/compare/{source_sha}...{env['GITHUB_SHA']}", token))
    evidence = verify_evidence(fetch=fetch, token=token, run=run, job=job, run_id=run_id, run_attempt=attempt,
                               digest=inputs["digest"], account=settings["account"])
    digest_uri = f"{repository_uri()}@{inputs['digest']}"
    require(isinstance(evidence, dict) and evidence.get("image_digest") == inputs["digest"]
            and evidence.get("source_sha") == source_sha and evidence.get("digest_uri") == digest_uri,
            "Publish evidence does not confirm the requested digest and source commit")
    validated_attempt = field(env, "GITHUB_RUN_ATTEMPT", ATTEMPT, "workflow run attempt")
    write_outputs(env, {"image_digest": inputs["digest"], "source_sha": source_sha, "digest_uri": digest_uri,
                        "publish_run_id": str(run_id), "publish_run_attempt": str(attempt),
                        "validated_attempt": validated_attempt})
    lines = [f"- operation: {inputs['operation']}, dispatcher {actor} (admin)",
             f"- publish run {run_id} attempt {attempt}, source {source_sha}",
             f"- digest {inputs['digest']} confirmed by publish evidence"]
    for line in lines:
        print(line)
    write_summary(env, "Backend deploy validation", lines)
    return 0


# ------------------------------------------------------------------- AWS side

class Aws:
    """Runs the AWS CLI with JSON output and reduces failures to an error code."""

    def __init__(self, runner=subprocess.run, base_env=None):
        self.runner = runner
        self.base_env = os.environ if base_env is None else base_env

    def call(self, args, *, retries=True, timeout=60):
        env = dict(self.base_env)
        env.update({"AWS_PAGER": "", "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION})
        if not retries:
            # A retried SendCommand could run the deployment twice.
            env.update({"AWS_MAX_ATTEMPTS": "1", "AWS_RETRY_MODE": "standard"})
        try:
            done = self.runner(["aws", *args, "--region", REGION, "--output", "json", "--no-cli-pager"],
                               capture_output=True, text=True, timeout=timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            raise AwsError("ClientTimeout") from None
        except OSError:
            raise AwsError("CliUnavailable") from None
        if done.returncode != 0:
            match = AWS_ERROR.search(done.stderr or "")
            raise AwsError(match.group(1) if match else "CliFailed")
        try:
            return json.loads(done.stdout)
        except ValueError:
            raise AwsError("InvalidJson") from None


def safe_status(invocation):
    status = invocation.get("Status") if isinstance(invocation, dict) else None
    code = invocation.get("ResponseCode") if isinstance(invocation, dict) else None
    return (status if status in SSM_STATUSES else "Unknown",
            code if type(code) is int else None)


def check_identity(identity, account):
    arn = identity.get("Arn", "") if isinstance(identity, dict) else ""
    require(isinstance(identity, dict) and identity.get("Account") == account
            and re.fullmatch(rf"arn:aws:sts::{account}:assumed-role/{ROLE_NAME}/[\w+=,.@-]{{2,64}}", arn),
            f"AWS identity is not the dedicated {ROLE_NAME} role")


def check_ecr_image(result, account, digest, source_sha):
    details = result.get("imageDetails") if isinstance(result, dict) else None
    require(isinstance(details, list) and len(details) == 1, "ECR did not return exactly one image for the digest")
    image = details[0]
    require(isinstance(image, dict) and image.get("registryId") == account
            and image.get("repositoryName") == ECR_REPOSITORY and image.get("imageDigest") == digest,
            "ECR image does not match the requested digest")
    tags = image.get("imageTags") or []
    require(isinstance(tags, list) and source_sha in tags, "ECR image is not tagged with the published source SHA")


def classify(output):
    """Parse ToI-DeploymentStatus output and separate not-deployed from fault states."""
    require(isinstance(output, str), "Status output is missing")
    lines = output.splitlines()
    if lines == ["deployment=busy"]:
        return {"kind": "busy", "reason": "another deployment operation holds the lock"}
    checksums = {}
    if len(lines) == 6:
        for line, script in zip(lines[:3], ("deploy", "rollback", "deployment_common")):
            match = CHECKSUM_LINE.fullmatch(line)
            require(match and match.group(2) == script, "Status checksum lines had an unexpected format")
            checksums[script] = match.group(1)
        lines = lines[3:]
    require(len(lines) == 3, "Status output had an unexpected format")
    state = STATE_LINE.fullmatch(lines[0])
    require(state, "Status line had an unexpected format")
    services = {}
    for name, line in zip(("api", "db"), lines[1:]):
        match = SERVICE_LINE.fullmatch(line)
        require(match and match.group(1) == name, f"{name} status line had an unexpected format")
        services[name] = None if match.group(2) is None else (match.group(2), match.group(3))
    status, current, previous, last_result = state.groups()
    report = {"status": status, "current": current, "previous": previous, "last_result": last_result,
              "api": services["api"], "db": services["db"], "checksums": checksums}
    healthy = ("running", "healthy")
    if status != "ready":
        return {**report, "kind": "fault", "reason": f"deployment state is {status}"}
    if current == "-":
        # deploy.sh records ready/-/-/failed when the first deployment stops in its
        # preflight before touching the API; it accepts a retry from that state.
        if previous != "-" or last_result not in ("none", "failed") or services["api"] is not None:
            return {**report, "kind": "fault", "reason": "no current digest but deployment traces exist"}
        if services["db"] != healthy:
            return {**report, "kind": "fault", "reason": "DB container is not running and healthy"}
        if last_result == "failed":
            return {**report, "kind": "first_retry",
                    "reason": "first deployment failed in its preflight; a manual retry is allowed"}
        return {**report, "kind": "not_deployed", "reason": "no deployment has run yet"}
    if services["api"] != healthy or services["db"] != healthy:
        return {**report, "kind": "fault", "reason": "API or DB container is not running and healthy"}
    return {**report, "kind": "healthy", "reason": "current deployment is running and healthy"}


class Runner:
    def __init__(self, env, aws, sleep=time.sleep, clock=time.monotonic, now=None):
        self.env, self.aws, self.sleep, self.clock = env, aws, sleep, clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.summary = []

    def note(self, line):
        print(line)
        self.summary.append(line)

    def comment(self, phase):
        return f"{COMMENT_PREFIX} {phase} run {self.run_id} attempt {self.run_attempt}"

    def send(self, document, phase, parameters):
        name, version = document
        comment = self.comment(phase)
        started = self.now()
        args = ["ssm", "send-command", "--instance-ids", self.instance, "--document-name", name,
                "--document-version", version, "--comment", comment, "--timeout-seconds", DELIVERY_TIMEOUT]
        if parameters:
            args += ["--parameters", json.dumps(parameters)]
        try:
            result = self.aws.call(args, retries=False)
            command_id = (result.get("Command") or {}).get("CommandId") if isinstance(result, dict) else None
            if isinstance(command_id, str) and COMMAND_ID.fullmatch(command_id):
                return command_id
            print(f"SendCommand for {phase} returned no command ID; looking it up without resending.")
        except AwsError as error:
            print(f"SendCommand for {phase} was not confirmed ({error.code}); looking it up without resending.")
        return self.find_sent(name, version, comment, parameters or {}, started)

    def find_sent(self, name, version, comment, parameters, started):
        after = (started - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters = json.dumps([{"key": "DocumentName", "value": name}, {"key": "InvokedAfter", "value": after}])
        for delay in LOOKUP_DELAYS:
            self.sleep(delay)
            try:
                listed = self.aws.call(["ssm", "list-commands", "--instance-id", self.instance, "--filters", filters])
            except AwsError as error:
                raise Blocked(f"Submitted command could not be looked up ({error.code}); not resending. "
                              "Check ToI-DeploymentStatus before any rerun") from None
            commands = listed.get("Commands") if isinstance(listed, dict) else None
            require(isinstance(commands, list), "Command lookup returned an unexpected result")
            matches = [item for item in commands if isinstance(item, dict) and item.get("Comment") == comment]
            if not matches:
                continue
            require(len(matches) == 1, "More than one command matches this run; it cannot be determined. "
                                       "Check ToI-DeploymentStatus before any rerun")
            match = matches[0]
            require(match.get("DocumentName") == name and match.get("DocumentVersion") == version
                    and match.get("InstanceIds") == [self.instance]
                    and (match.get("Parameters") or {}) == parameters
                    and isinstance(match.get("CommandId"), str) and COMMAND_ID.fullmatch(match["CommandId"]),
                    "The command found for this run does not match the request; not tracking it")
            print("Found the submitted command; tracking it without resending.")
            return match["CommandId"]
        raise Blocked("No submitted command was found for this run; not resending. "
                      "Check ToI-DeploymentStatus before any rerun")

    def wait(self, command_id, limit):
        deadline = self.clock() + limit
        while True:
            try:
                invocation = self.aws.call(["ssm", "get-command-invocation", "--command-id", command_id,
                                            "--instance-id", self.instance])
            except AwsError as error:
                if error.code != "InvocationDoesNotExist":
                    raise Blocked(f"SSM result could not be retrieved ({error.code}); the command was not "
                                  "cancelled. Check ToI-DeploymentStatus") from None
                invocation = None
            if isinstance(invocation, dict) and invocation.get("Status") in TERMINAL:
                return invocation
            if self.clock() >= deadline:
                raise Blocked("SSM command did not finish in time; it was not cancelled. "
                              "Check ToI-DeploymentStatus")
            self.sleep(POLL_INTERVAL)

    def read_status(self, phase):
        command_id = self.send(STATUS_DOCUMENT, phase, None)
        invocation = self.wait(command_id, STATUS_LIMIT)
        status, code = safe_status(invocation)
        if status != "Success" or code != 0:
            meaning = EXIT_MEANINGS.get(code, "unknown exit code") if code is not None else "no exit code"
            raise Blocked(f"ToI-DeploymentStatus failed (status={status}, response_code={code}, {meaning})")
        report = classify(invocation.get("StandardOutputContent"))
        self.note(f"- {phase}: {report['kind']} ({report['reason']}), command {command_id}")
        if "status" in report:
            api = " ".join(report["api"]) if report["api"] else "absent"
            db = " ".join(report["db"]) if report["db"] else "absent"
            self.note(f"  - status={report['status']} current={report['current']} previous={report['previous']} "
                      f"last_result={report['last_result']} api={api} db={db}")
        for script, checksum in sorted(report.get("checksums", {}).items()):
            self.note(f"  - installed {script}.sh sha256 {checksum}")
        return report

    def run(self):
        settings = check_settings(self.env)
        self.account, self.instance = settings["account"], settings["instance"]
        operation = self.env.get("OPERATION")
        require(operation in OPERATIONS, "operation must be preflight or deploy")
        digest = field(self.env, "IMAGE_DIGEST", DIGEST, "validated image digest")
        require(self.env.get("INPUT_IMAGE_DIGEST") == digest, "Validated digest differs from the dispatch input")
        source_sha = field(self.env, "SOURCE_SHA", SHA, "validated source SHA")
        # The deploy target is always the digest URI; tags are only checked, never deployed.
        require(self.env.get("DIGEST_URI") == f"{repository_uri()}@{digest}",
                "Validated digest URI does not match the approved repository and digest")
        self.run_id = field(self.env, "GITHUB_RUN_ID", NUMBER, "workflow run ID")
        self.run_attempt = field(self.env, "GITHUB_RUN_ATTEMPT", ATTEMPT, "workflow run attempt")
        # A partial re-run reuses the earlier validate outputs without checking the
        # new dispatcher, so only the attempt that ran validate may continue.
        require(self.env.get("VALIDATED_ATTEMPT") == self.run_attempt,
                "This attempt did not run validate; re-run all jobs instead of a single job")
        actor = self.env.get("GITHUB_ACTOR", "")
        require(LOGIN.fullmatch(actor) and self.env.get("GITHUB_TRIGGERING_ACTOR") == actor,
                "The dispatcher must be the triggering actor")
        publish_run = field(self.env, "PUBLISH_RUN_ID", NUMBER, "validated publish run ID")
        publish_attempt = field(self.env, "PUBLISH_RUN_ATTEMPT", ATTEMPT, "validated publish run attempt")
        self.note(f"- operation: {operation}, dispatcher {actor}")
        self.note(f"- publish run {publish_run} attempt {publish_attempt}, source {source_sha}, digest {digest}")

        try:
            check_identity(self.aws.call(["sts", "get-caller-identity"]), self.account)
            check_ecr_image(self.aws.call(["ecr", "describe-images", "--registry-id", self.account,
                                           "--repository-name", ECR_REPOSITORY,
                                           "--image-ids", f"imageDigest={digest}"]),
                            self.account, digest, source_sha)
        except AwsError as error:
            raise Blocked(f"AWS identity or ECR image check failed ({error.code})") from None
        self.note("- AWS identity and ECR image verified")

        before = self.read_status("pre-status")
        require(before["kind"] != "busy",
                "Another deployment operation is running on the host; retry after it finishes")
        require(before["kind"] in ("not_deployed", "first_retry", "healthy"),
                "Deployment host is not in a deployable state; follow docs/deployment/ROLLBACK.md")
        if operation == "preflight":
            self.note("- preflight passed; ToI-DeployBackend was not called")
            return 0

        command_id = self.send(DEPLOY_DOCUMENT, "deploy", {"ImageDigest": [digest]})
        self.note(f"- ToI-DeployBackend command {command_id}")
        status, code = safe_status(self.wait(command_id, DEPLOY_LIMIT))
        meaning = EXIT_MEANINGS.get(code, "unknown exit code") if code is not None else "no exit code"
        self.note(f"- deploy result: status={status}, exit_code={code} ({meaning})")
        succeeded = status == "Success" and code == 0
        after = self.read_status("post-status")
        require(succeeded, f"Deployment failed (exit code {code}: {meaning}); see docs/deployment/ROLLBACK.md")
        require(after["kind"] == "healthy" and after["current"] == digest
                and after["last_result"] in ("success", "unchanged"),
                "Post-deployment status does not show the requested digest running healthy")
        self.note("- deployment verified: current digest is running healthy")
        return 0


def write_summary(env, title, lines, error=None):
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"## {title}\n\n")
        for line in lines:
            handle.write(line + "\n")
        if error:
            handle.write(f"\n**Blocked:** {error}\n")


def execute(env, aws=None, **timing):
    runner = Runner(env, aws or Aws(), **timing)
    try:
        result = runner.run()
    except Blocked as error:
        print(f"::error::{error}")
        write_summary(env, "Backend deploy", runner.summary, str(error))
        return 1
    write_summary(env, "Backend deploy", runner.summary)
    return result


def run_validate(env, **kwargs):
    try:
        return validate(env, **kwargs)
    except Blocked as error:
        print(f"::error::{error}")
        write_summary(env, "Backend deploy validation", [], str(error))
        return 1


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    if argv == ["validate"]:
        return run_validate(env)
    if argv == ["execute"]:
        return execute(env)
    print("usage: backend_deploy.py validate|execute", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
