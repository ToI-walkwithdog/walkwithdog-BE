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
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

REPOSITORY = "ToI-walkwithdog/walkwithdog-BE"
WORKFLOW = ".github/workflows/backend-deploy.yml"
PUBLISH_WORKFLOW = ".github/workflows/ecr-image-publish.yml"
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

def api(path, token):
    request = urllib.request.Request("https://api.github.com/" + path, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "toi-backend-deploy",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


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
    instance = field(env, "EC2_INSTANCE_ID", INSTANCE, "EC2_INSTANCE_ID")
    require(env.get("SSM_DEPLOY_ROLE_ARN") == f"arn:aws:iam::{account}:role/{ROLE_NAME}",
            f"SSM_DEPLOY_ROLE_ARN must name the dedicated {ROLE_NAME} role")
    return {"account": account, "instance": instance}


def check_publish_run(run, latest, run_id, run_attempt):
    for item in (run, latest):
        require(isinstance(item, dict), "Publish run metadata is missing")
        require(item.get("id") == run_id
                and (item.get("repository") or {}).get("full_name") == REPOSITORY
                and (item.get("head_repository") or {}).get("full_name") == REPOSITORY,
                "Publish run belongs to another run or repository")
        require(item.get("path") == PUBLISH_WORKFLOW and item.get("event") == "workflow_dispatch"
                and item.get("head_branch") == "main",
                "Publish run is not an ECR Image Publish dispatch on main")
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


def evidence_contract_pending(**_context):
    """Placeholder until #136 settles the operational image-publish-evidence.json contract."""
    raise Blocked("The operational ECR publish evidence contract (#136) is not integrated yet; "
                  "deployment stays blocked")


def write_outputs(env, values):
    path = env.get("GITHUB_OUTPUT")
    require(path, "GITHUB_OUTPUT is not available")
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def validate(env, fetch=api, verify_evidence=evidence_contract_pending):
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
    check_source_on_main(fetch(f"repos/{REPOSITORY}/compare/{source_sha}...{env['GITHUB_SHA']}", token))
    evidence = verify_evidence(fetch=fetch, token=token, run=run, source_sha=source_sha,
                               digest=inputs["digest"], account=settings["account"])
    require(isinstance(evidence, dict) and evidence.get("image_digest") == inputs["digest"]
            and evidence.get("source_sha") == source_sha,
            "Publish evidence does not confirm the requested digest and source commit")
    write_outputs(env, {"image_digest": inputs["digest"], "source_sha": source_sha})
    print(f"Validated {inputs['operation']} of {inputs['digest']} from publish run {run_id} attempt {attempt}.")
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
        for line in lines[:3]:
            match = CHECKSUM_LINE.fullmatch(line)
            require(match, "Status checksum lines had an unexpected format")
            checksums[match.group(2)] = match.group(1)
        require(len(checksums) == 3, "Status checksum lines had an unexpected format")
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
        if previous != "-" or last_result != "none" or services["api"] is not None:
            return {**report, "kind": "fault", "reason": "no current digest but deployment traces exist"}
        if services["db"] != healthy:
            return {**report, "kind": "fault", "reason": "DB container is not running and healthy"}
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
        self.run_id = field(self.env, "GITHUB_RUN_ID", NUMBER, "workflow run ID")
        self.run_attempt = field(self.env, "GITHUB_RUN_ATTEMPT", ATTEMPT, "workflow run attempt")
        self.note(f"- operation: {operation}, digest {digest}, source {source_sha}")

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
        require(before["kind"] in ("not_deployed", "healthy"),
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
