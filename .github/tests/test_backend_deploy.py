"""Unit tests for the SSM backend deploy verifier (#142). Synthetic data only.

No test reaches GitHub, AWS or a server: the GitHub API is a dict lookup and
the AWS CLI is replaced by a scripted runner that records every call.
"""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("backend_deploy", ROOT / "scripts" / "backend_deploy.py")
bd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bd)

_ENVIRONMENT = patch.dict(os.environ)


def setUpModule():
    # Synthetic runs must never write a job summary or outputs of a real run.
    _ENVIRONMENT.start()
    for key in ("GITHUB_STEP_SUMMARY", "GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_ENV"):
        os.environ.pop(key, None)


def tearDownModule():
    _ENVIRONMENT.stop()


REPO = bd.REPOSITORY
ACCOUNT = "111111111111"
INSTANCE = "i-0123456789abcdef0"
DIGEST = "sha256:" + "d" * 64
OTHER_DIGEST = "sha256:" + "e" * 64
SOURCE = "a" * 40
MAIN = "b" * 40
RUN_ID = 4242
COMMAND = "11111111-2222-3333-4444-555555555555"
SECRET = "SYNTHETIC-SECRET-VALUE-0001"
CHECKSUMS = [f"{c * 64}  /opt/toi/scripts/{name}.sh" for c, name in
             (("1", "deploy"), ("2", "rollback"), ("3", "deployment_common"))]
API_OK = "api " + "4" * 64 + " sha256:" + "5" * 64 + " running healthy"
DB_OK = "db " + "6" * 64 + " sha256:" + "7" * 64 + " running healthy"


def status_text(state, api=API_OK, db=DB_OK, checksums=True):
    lines = (CHECKSUMS if checksums else []) + [state, api, db]
    return "\n".join(lines) + "\n"


NOT_DEPLOYED = status_text("status=ready current=- previous=- last_result=none", api="api absent")
FIRST_RETRY = status_text("status=ready current=- previous=- last_result=failed", api="api absent")
HEALTHY_OLD = status_text(f"status=ready current={OTHER_DIGEST} previous=- last_result=success")
HEALTHY_NEW = status_text(f"status=ready current={DIGEST} previous={OTHER_DIGEST} last_result=success")


def dispatch_env(**overrides):
    env = {
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": REPO,
        "GITHUB_WORKFLOW_REF": f"{REPO}/{bd.WORKFLOW}@refs/heads/main", "GITHUB_ACTOR": "admin-user",
        "GITHUB_TRIGGERING_ACTOR": "admin-user", "GITHUB_SHA": MAIN, "GH_TOKEN": SECRET, "GITHUB_RUN_ATTEMPT": "1",
        "OPERATION": "preflight", "PUBLISH_RUN_ID": str(RUN_ID), "PUBLISH_RUN_ATTEMPT": "1", "IMAGE_DIGEST": DIGEST,
        "AWS_REGION": "ap-northeast-2", "AWS_ACCOUNT_ID": ACCOUNT, "ECR_REPOSITORY": "toi-be",
        "EC2_INSTANCE_ID": INSTANCE, "SSM_DEPLOY_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/{bd.ROLE_NAME}",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def execute_env(**overrides):
    env = {
        "OPERATION": "preflight", "IMAGE_DIGEST": DIGEST, "INPUT_IMAGE_DIGEST": DIGEST, "SOURCE_SHA": SOURCE,
        "AWS_REGION": "ap-northeast-2", "AWS_ACCOUNT_ID": ACCOUNT, "ECR_REPOSITORY": "toi-be",
        "EC2_INSTANCE_ID": INSTANCE, "SSM_DEPLOY_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/{bd.ROLE_NAME}",
        "GITHUB_RUN_ID": "9001", "GITHUB_RUN_ATTEMPT": "1", "VALIDATED_ATTEMPT": "1",
        "GITHUB_ACTOR": "admin-user", "GITHUB_TRIGGERING_ACTOR": "admin-user",
        "PUBLISH_RUN_ID": str(RUN_ID), "PUBLISH_RUN_ATTEMPT": "1",
        "AWS_SECRET_ACCESS_KEY": SECRET,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def publish_run(**overrides):
    run = {"id": RUN_ID, "run_attempt": 1, "path": bd.PUBLISH_WORKFLOW, "event": "workflow_dispatch",
           "head_branch": "main", "status": "completed", "conclusion": "success", "head_sha": SOURCE,
           "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}}
    run.update(overrides)
    return run


def github(run=None, latest=None, permission="admin", compare="ahead"):
    responses = {
        f"repos/{REPO}/collaborators/admin-user/permission": {"user": {"login": "admin-user"},
                                                              "permission": permission},
        f"repos/{REPO}/actions/runs/{RUN_ID}/attempts/1": run or publish_run(),
        f"repos/{REPO}/actions/runs/{RUN_ID}": latest or run or publish_run(),
        f"repos/{REPO}/compare/{SOURCE}...{MAIN}": {"status": compare},
    }

    def fetch(path, token):
        assert token == SECRET
        return responses[path]
    return fetch


def confirmed_evidence(**context):
    return {"image_digest": context["digest"], "source_sha": context["source_sha"]}


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def ok(value):
    return Result(0, json.dumps(value))


def aws_error(code):
    return Result(254, "", f"\nAn error occurred ({code}) when calling the operation: {SECRET} detail\n")


def invocation(status="Success", code=0, output=None):
    return ok({"Status": status, "ResponseCode": code, "StandardOutputContent": output if output is not None
               else f"{SECRET}", "StandardErrorContent": SECRET})


class FakeAws:
    """Scripted AWS CLI. Each key maps to a list of results consumed in order."""

    def __init__(self, **script):
        self.script = {key: list(value) for key, value in script.items()}
        self.calls = []

    def key(self, argv):
        op = argv[2]
        if op in ("send-command", "list-commands"):
            doc = argv[argv.index("--document-name") + 1] if "--document-name" in argv else "list"
            return f"{op}:{doc}" if op == "send-command" else op
        if op == "get-command-invocation":
            return f"{op}:{argv[argv.index('--command-id') + 1]}"
        return op

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        key = self.key(argv)
        queue = self.script.get(key)
        if not queue:
            raise AssertionError(f"unexpected AWS call {key}")
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def count(self, key):
        return sum(1 for argv, _ in self.calls if self.key(argv) == key)


STATUS_CMD = "aaaaaaaa-0000-0000-0000-000000000001"
DEPLOY_CMD = "bbbbbbbb-0000-0000-0000-000000000002"
POST_CMD = "cccccccc-0000-0000-0000-000000000003"


def identity(role=bd.ROLE_NAME):
    return ok({"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{role}/toi-backend-deploy-9001"})


def ecr_image(digest=DIGEST, tags=(SOURCE,)):
    return ok({"imageDetails": [{"registryId": ACCOUNT, "repositoryName": "toi-be", "imageDigest": digest,
                                 "imageTags": list(tags)}]})


def sent(command_id):
    return ok({"Command": {"CommandId": command_id}})


def base_script(pre=NOT_DEPLOYED):
    return {
        "get-caller-identity": [identity()],
        "describe-images": [ecr_image()],
        "send-command:ToI-DeploymentStatus": [sent(STATUS_CMD)],
        f"get-command-invocation:{STATUS_CMD}": [invocation(output=pre)],
    }


def deploy_script(pre=NOT_DEPLOYED, deploy_result=None, post=HEALTHY_NEW):
    script = base_script(pre)
    script["send-command:ToI-DeploymentStatus"].append(sent(POST_CMD))
    script["send-command:ToI-DeployBackend"] = [sent(DEPLOY_CMD)]
    script[f"get-command-invocation:{DEPLOY_CMD}"] = [deploy_result or invocation()]
    script[f"get-command-invocation:{POST_CMD}"] = [invocation(output=post)]
    return script


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def run_execute(fake, env=None):
    clock = Clock()
    summary = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".md", encoding="utf-8")
    summary.close()
    env = dict(env or execute_env())
    env["GITHUB_STEP_SUMMARY"] = summary.name
    out = io.StringIO()
    with redirect_stdout(out):
        code = bd.execute(env, aws=bd.Aws(runner=fake, base_env=env), sleep=clock.sleep, clock=clock,
                          now=lambda: datetime(2026, 11, 26, 3, 0, 0, tzinfo=timezone.utc))
    text = Path(summary.name).read_text(encoding="utf-8")
    os.unlink(summary.name)
    return code, out.getvalue(), text, clock


class DispatchTests(unittest.TestCase):
    def test_accepts_main_dispatch(self):
        self.assertEqual(bd.check_dispatch(dispatch_env()), "admin-user")

    def test_rejects_other_refs_events_and_workflows(self):
        for override in ({"GITHUB_REF": "refs/heads/dev"}, {"GITHUB_EVENT_NAME": "push"},
                         {"GITHUB_REPOSITORY": "fork/walkwithdog-BE"},
                         {"GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/other.yml@refs/heads/main"},
                         {"GITHUB_TRIGGERING_ACTOR": "someone-else"}, {"GITHUB_SHA": "short"}):
            with self.subTest(override=override), self.assertRaises(bd.Blocked):
                bd.check_dispatch(dispatch_env(**override))

    def test_requires_admin_dispatcher(self):
        bd.check_admin({"user": {"login": "admin-user"}, "permission": "admin"}, "admin-user")
        for permission in ({"user": {"login": "admin-user"}, "permission": "write"},
                           {"user": {"login": "other"}, "permission": "admin"}, {}):
            with self.subTest(permission=permission), self.assertRaises(bd.Blocked):
                bd.check_admin(permission, "admin-user")

    def test_rejects_malformed_inputs(self):
        for override in ({"OPERATION": "rollback"}, {"OPERATION": None}, {"PUBLISH_RUN_ID": "0"},
                         {"PUBLISH_RUN_ID": "12;rm"}, {"PUBLISH_RUN_ATTEMPT": "x"},
                         {"IMAGE_DIGEST": "sha256:" + "D" * 64}, {"IMAGE_DIGEST": "latest"},
                         {"IMAGE_DIGEST": DIGEST + "\nextra=1"}):
            with self.subTest(override=override), self.assertRaises(bd.Blocked):
                bd.check_inputs(dispatch_env(**override))

    def test_rejects_wrong_settings(self):
        for override in ({"AWS_REGION": "us-east-1"}, {"ECR_REPOSITORY": "other"}, {"AWS_ACCOUNT_ID": "123"},
                         {"EC2_INSTANCE_ID": "i-xyz"}, {"SSM_DEPLOY_ROLE_ARN": None},
                         {"SSM_DEPLOY_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/toi-github-actions-role"},
                         {"SSM_DEPLOY_ROLE_ARN": f"arn:aws:iam::222222222222:role/{bd.ROLE_NAME}"}):
            with self.subTest(override=override), self.assertRaises(bd.Blocked):
                bd.check_settings(dispatch_env(**override))


class PublishRunTests(unittest.TestCase):
    def test_accepts_successful_latest_publish_dispatch(self):
        self.assertEqual(bd.check_publish_run(publish_run(), publish_run(), RUN_ID, 1), SOURCE)

    def test_rejects_untrusted_publish_runs(self):
        cases = [
            (publish_run(path=".github/workflows/release-ci.yml"), None),
            (publish_run(event="push"), None),
            (publish_run(head_branch="dev"), None),
            (publish_run(conclusion="failure"), None),
            (publish_run(status="in_progress", conclusion=None), None),
            (publish_run(repository={"full_name": "fork/walkwithdog-BE"}), None),
            (publish_run(head_repository={"full_name": "fork/walkwithdog-BE"}), None),
            (publish_run(id=RUN_ID + 1), None),
            (publish_run(), publish_run(run_attempt=2)),
            (publish_run(), publish_run(head_sha="f" * 40)),
            (publish_run(head_sha="short"), None),
        ]
        for run, latest in cases:
            with self.subTest(run=run, latest=latest), self.assertRaises(bd.Blocked):
                bd.check_publish_run(run, latest or run, RUN_ID, 1)

    def test_source_must_be_on_main(self):
        bd.check_source_on_main({"status": "identical"})
        for status in ("diverged", "behind", None):
            with self.subTest(status=status), self.assertRaises(bd.Blocked):
                bd.check_source_on_main({"status": status})


class ValidateTests(unittest.TestCase):
    def run_validate(self, env, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            output.write_text("", encoding="utf-8")
            env = dict(env, GITHUB_OUTPUT=str(output))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = bd.run_validate(env, **kwargs)
            return code, output.read_text(encoding="utf-8"), stdout.getvalue()

    def test_blocks_until_evidence_contract_is_integrated(self):
        code, outputs, stdout = self.run_validate(dispatch_env(), fetch=github())
        self.assertEqual(code, 1)
        self.assertEqual(outputs, "")
        self.assertIn("evidence contract", stdout)
        self.assertNotIn(SECRET, stdout)

    def test_writes_outputs_after_confirmed_evidence(self):
        code, outputs, stdout = self.run_validate(dispatch_env(GITHUB_RUN_ATTEMPT="3"), fetch=github(),
                                                  verify_evidence=confirmed_evidence)
        self.assertEqual(code, 0)
        self.assertEqual(outputs, f"image_digest={DIGEST}\nsource_sha={SOURCE}\npublish_run_id={RUN_ID}\n"
                                  "publish_run_attempt=1\nvalidated_attempt=3\n")
        self.assertIn(f"publish run {RUN_ID} attempt 1", stdout)
        self.assertNotIn(SECRET, stdout)

    def test_github_api_errors_are_blocked_without_traceback(self):
        import urllib.error
        for error in (urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", {}, None),
                      urllib.error.URLError("down"), ValueError("bad json")):
            with self.subTest(error=type(error).__name__):
                with patch.object(bd.urllib.request, "urlopen", side_effect=error):
                    code, outputs, stdout = self.run_validate(dispatch_env(), fetch=bd.api)
                self.assertEqual((code, outputs), (1, ""))
                self.assertIn("GitHub API", stdout)
                self.assertNotIn(SECRET, stdout)

    def test_evidence_mismatch_blocks(self):
        def other(**context):
            return {"image_digest": OTHER_DIGEST, "source_sha": context["source_sha"]}
        code, outputs, _ = self.run_validate(dispatch_env(), fetch=github(), verify_evidence=other)
        self.assertEqual((code, outputs), (1, ""))

    def test_non_admin_and_failed_runs_block_before_evidence(self):
        def never(**_context):
            raise AssertionError("evidence must not be read")
        for fetch in (github(permission="write"), github(run=publish_run(conclusion="failure")),
                      github(compare="diverged")):
            with self.subTest():
                code, outputs, _ = self.run_validate(dispatch_env(), fetch=fetch, verify_evidence=never)
                self.assertEqual((code, outputs), (1, ""))


class ClassifyTests(unittest.TestCase):
    def test_not_deployed_is_distinct_from_faults(self):
        report = bd.classify(NOT_DEPLOYED)
        self.assertEqual(report["kind"], "not_deployed")
        self.assertEqual(len(report["checksums"]), 3)

    def test_healthy_with_and_without_checksums(self):
        self.assertEqual(bd.classify(HEALTHY_OLD)["kind"], "healthy")
        self.assertEqual(bd.classify(status_text(
            f"status=ready current={DIGEST} previous=- last_result=unchanged", checksums=False))["kind"], "healthy")

    def test_fault_states(self):
        cases = {
            "busy": "deployment=busy\n",
            "in_progress": status_text(f"status=in_progress current={OTHER_DIGEST} previous=- last_result=running"),
            "blocked": status_text("status=blocked current=- previous=- last_result=failed", api="api absent"),
            "api missing": status_text(f"status=ready current={DIGEST} previous=- last_result=success",
                                       api="api absent"),
            "api unhealthy": status_text(f"status=ready current={DIGEST} previous=- last_result=success",
                                         api=API_OK.replace("healthy", "unhealthy")),
            "db missing first": status_text("status=ready current=- previous=- last_result=none",
                                            api="api absent", db="db absent"),
            "trace without current": status_text(f"status=ready current=- previous={OTHER_DIGEST} "
                                                 "last_result=failed", api="api absent"),
            "api without record": status_text("status=ready current=- previous=- last_result=none"),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertIn(bd.classify(text)["kind"], ("busy", "fault"))

    def test_first_retry_requires_every_condition(self):
        self.assertEqual(bd.classify(FIRST_RETRY)["kind"], "first_retry")
        unhealthy_db = DB_OK.replace("healthy", "unhealthy")
        near_misses = {
            "blocked": status_text("status=blocked current=- previous=- last_result=failed", api="api absent"),
            "in_progress": status_text("status=in_progress current=- previous=- last_result=failed",
                                       api="api absent"),
            "current set": status_text(f"status=ready current={DIGEST} previous=- last_result=failed",
                                       api="api absent"),
            "previous set": status_text(f"status=ready current=- previous={OTHER_DIGEST} last_result=failed",
                                        api="api absent"),
            "running result": status_text("status=ready current=- previous=- last_result=running",
                                          api="api absent"),
            "success result": status_text("status=ready current=- previous=- last_result=success",
                                          api="api absent"),
            "api present": status_text("status=ready current=- previous=- last_result=failed"),
            "api exited": status_text("status=ready current=- previous=- last_result=failed",
                                      api=API_OK.replace("running healthy", "exited none")),
            "db absent": status_text("status=ready current=- previous=- last_result=failed",
                                     api="api absent", db="db absent"),
            "db unhealthy": status_text("status=ready current=- previous=- last_result=failed",
                                        api="api absent", db=unhealthy_db),
            "db starting": status_text("status=ready current=- previous=- last_result=failed",
                                       api="api absent", db=DB_OK.replace("healthy", "starting")),
        }
        for name, text in near_misses.items():
            with self.subTest(name=name):
                self.assertEqual(bd.classify(text)["kind"], "fault")

    def test_malformed_output_blocks(self):
        reordered = "\n".join([CHECKSUMS[1], CHECKSUMS[0], CHECKSUMS[2]] + NOT_DEPLOYED.splitlines()[3:]) + "\n"
        for text in ("", "status=ready\n", NOT_DEPLOYED.replace("/opt/toi", "/tmp"), reordered,
                     status_text("status=ready current=latest previous=- last_result=none"),
                     status_text("status=ready current=- previous=- last_result=none", api="db absent"), None):
            with self.subTest(text=text), self.assertRaises(bd.Blocked):
                bd.classify(text)


class ExecuteTests(unittest.TestCase):
    def assert_no_leak(self, stdout, summary):
        for text in (stdout, summary):
            self.assertNotIn(SECRET, text)

    def test_preflight_not_deployed_passes_without_deploy(self):
        fake = FakeAws(**base_script())
        code, stdout, summary, _ = run_execute(fake)
        self.assertEqual(code, 0, stdout)
        self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 0)
        self.assertIn("not_deployed", summary)
        self.assert_no_leak(stdout, summary)

    def test_first_retry_runs_one_deploy_and_is_verified(self):
        code, stdout, summary, _ = run_execute(FakeAws(**base_script(pre=FIRST_RETRY)))
        self.assertEqual(code, 0, stdout)
        self.assertIn("first_retry", summary)
        fake = FakeAws(**deploy_script(pre=FIRST_RETRY))
        code, stdout, summary, _ = run_execute(fake, execute_env(OPERATION="deploy"))
        self.assertEqual(code, 0, stdout)
        self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 1)
        self.assertIn("deployment verified", summary)

    def test_failed_first_retry_is_not_retried_automatically(self):
        failed_again = invocation("Failed", 7)
        fake = FakeAws(**deploy_script(pre=FIRST_RETRY, deploy_result=failed_again, post=FIRST_RETRY))
        code, _, summary, _ = run_execute(fake, execute_env(OPERATION="deploy"))
        self.assertEqual(code, 1)
        self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 1)
        self.assertIn(f"exit_code=7 ({bd.EXIT_MEANINGS[7]})", summary)

    def test_preflight_healthy_passes(self):
        code, stdout, _, _ = run_execute(FakeAws(**base_script(pre=HEALTHY_OLD)))
        self.assertEqual(code, 0, stdout)

    def test_fault_or_busy_host_blocks_before_deploy(self):
        cases = (("deployment=busy\n", "retry after it finishes"),
                 (status_text("status=blocked current=- previous=- last_result=failed", api="api absent"),
                  "ROLLBACK.md"))
        for pre, message in cases:
            for operation in ("preflight", "deploy"):
                with self.subTest(pre=pre, operation=operation):
                    fake = FakeAws(**deploy_script(pre=pre))
                    code, stdout, summary, _ = run_execute(fake, execute_env(OPERATION=operation))
                    self.assertEqual(code, 1)
                    self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 0)
                    self.assertIn(message, summary)

    def test_partial_rerun_or_other_rerunner_is_blocked_before_aws(self):
        for override in ({"GITHUB_RUN_ATTEMPT": "2"}, {"VALIDATED_ATTEMPT": None},
                         {"GITHUB_TRIGGERING_ACTOR": "write-user"}, {"PUBLISH_RUN_ID": ""}):
            with self.subTest(override=override):
                fake = FakeAws()
                code, stdout, _, _ = run_execute(fake, execute_env(OPERATION="deploy", **override))
                self.assertEqual((code, fake.calls), (1, []))

    def test_summary_records_publish_run_and_command(self):
        code, _, summary, _ = run_execute(FakeAws(**deploy_script()), execute_env(OPERATION="deploy"))
        self.assertEqual(code, 0)
        for text in (f"publish run {RUN_ID} attempt 1", SOURCE, DIGEST, DEPLOY_CMD, "exit_code=0"):
            self.assertIn(text, summary)

    def test_status_command_failure_blocks(self):
        script = base_script()
        script[f"get-command-invocation:{STATUS_CMD}"] = [invocation("Failed", 5)]
        code, stdout, summary, _ = run_execute(FakeAws(**script))
        self.assertEqual(code, 1)
        self.assertIn("response_code=5", stdout)
        self.assert_no_leak(stdout, summary)

    def test_wrong_identity_or_image_blocks_before_ssm(self):
        cases = [
            {"get-caller-identity": [identity("toi-github-actions-role")]},
            {"describe-images": [aws_error("ImageNotFoundException")]},
            {"describe-images": [ecr_image(digest=OTHER_DIGEST)]},
            {"describe-images": [ecr_image(tags=("f" * 40,))]},
            {"get-caller-identity": [aws_error("ExpiredToken")]},
        ]
        for override in cases:
            with self.subTest(override=list(override)):
                script = base_script()
                script.update(override)
                fake = FakeAws(**script)
                code, stdout, summary, _ = run_execute(fake)
                self.assertEqual(code, 1)
                self.assertEqual(fake.count("send-command:ToI-DeploymentStatus"), 0)
                self.assert_no_leak(stdout, summary)

    def test_digest_handoff_must_match_dispatch_input(self):
        fake = FakeAws()
        code, _, _, _ = run_execute(fake, execute_env(INPUT_IMAGE_DIGEST=OTHER_DIGEST))
        self.assertEqual((code, fake.calls), (1, []))

    def test_deploy_success_is_verified_by_post_status(self):
        fake = FakeAws(**deploy_script())
        code, stdout, summary, _ = run_execute(fake, execute_env(OPERATION="deploy"))
        self.assertEqual(code, 0, stdout)
        deploy_calls = [argv for argv, _ in fake.calls if "ToI-DeployBackend" in argv]
        self.assertEqual(len(deploy_calls), 1)
        argv = deploy_calls[0]
        self.assertEqual(argv[argv.index("--document-version") + 1], "1")
        self.assertEqual(json.loads(argv[argv.index("--parameters") + 1]), {"ImageDigest": [DIGEST]})
        self.assertEqual(argv[argv.index("--comment") + 1], "toi-backend-deploy deploy run 9001 attempt 1")
        self.assertNotIn("commands", argv)
        self.assertIn("deployment verified", summary)
        self.assert_no_leak(stdout, summary)

    def test_post_status_must_show_requested_digest(self):
        for post in (HEALTHY_OLD, status_text(f"status=ready current={DIGEST} previous=- last_result=success",
                                              api=API_OK.replace("healthy", "starting"))):
            with self.subTest(post=post):
                code, _, _, _ = run_execute(FakeAws(**deploy_script(post=post)), execute_env(OPERATION="deploy"))
                self.assertEqual(code, 1)

    def test_deploy_exit_codes_fail_with_meaning(self):
        for exit_code in range(2, 10):
            with self.subTest(exit_code=exit_code):
                script = deploy_script(deploy_result=invocation("Failed", exit_code), post=HEALTHY_OLD)
                code, stdout, summary, _ = run_execute(FakeAws(**script), execute_env(OPERATION="deploy"))
                self.assertEqual(code, 1)
                self.assertIn(f"exit_code={exit_code} ({bd.EXIT_MEANINGS[exit_code]})", summary)
                self.assert_no_leak(stdout, summary)

    def test_rolled_back_deploy_is_a_failure(self):
        script = deploy_script(deploy_result=invocation("Failed", 8), post=HEALTHY_OLD)
        code, _, summary, _ = run_execute(FakeAws(**script), execute_env(OPERATION="deploy"))
        self.assertEqual(code, 1)
        self.assertIn("previous healthy service was restored", summary)

    def test_ssm_timeout_and_cancel_are_failures(self):
        for status in ("TimedOut", "Cancelled"):
            with self.subTest(status=status):
                script = deploy_script(deploy_result=invocation(status, -1), post=HEALTHY_OLD)
                code, _, _, _ = run_execute(FakeAws(**script), execute_env(OPERATION="deploy"))
                self.assertEqual(code, 1)

    def test_polling_deadline_never_cancels(self):
        script = deploy_script()
        pending = [invocation("InProgress", -1)] * 400
        script[f"get-command-invocation:{DEPLOY_CMD}"] = pending
        fake = FakeAws(**script)
        code, stdout, _, clock = run_execute(fake, execute_env(OPERATION="deploy"))
        self.assertEqual(code, 1)
        self.assertIn("not cancelled", stdout)
        self.assertGreaterEqual(clock.value, bd.DEPLOY_LIMIT)
        self.assertFalse(any("cancel-command" in argv for argv, _ in fake.calls))

    def test_invocation_not_yet_visible_is_polled(self):
        script = deploy_script()
        script[f"get-command-invocation:{DEPLOY_CMD}"] = [aws_error("InvocationDoesNotExist"),
                                                         invocation("InProgress", -1), invocation()]
        code, stdout, _, _ = run_execute(FakeAws(**script), execute_env(OPERATION="deploy"))
        self.assertEqual(code, 0, stdout)

    def test_send_command_disables_cli_retries(self):
        fake = FakeAws(**deploy_script())
        run_execute(fake, execute_env(OPERATION="deploy"))
        for argv, kwargs in fake.calls:
            if argv[2] == "send-command":
                self.assertEqual(kwargs["env"]["AWS_MAX_ATTEMPTS"], "1")
            else:
                self.assertNotEqual(kwargs["env"].get("AWS_MAX_ATTEMPTS"), "1")
            self.assertIn("--no-cli-pager", argv)
            self.assertEqual(argv[argv.index("--region") + 1], "ap-northeast-2")


class LostResponseTests(unittest.TestCase):
    COMMENT = "toi-backend-deploy deploy run 9001 attempt 1"

    def listed(self, *commands):
        return ok({"Commands": list(commands)})

    def command(self, **overrides):
        item = {"CommandId": DEPLOY_CMD, "Comment": self.COMMENT, "DocumentName": "ToI-DeployBackend",
                "DocumentVersion": "1", "InstanceIds": [INSTANCE], "Parameters": {"ImageDigest": [DIGEST]}}
        item.update(overrides)
        return item

    def script(self, send_result, *lookups):
        script = deploy_script()
        script["send-command:ToI-DeployBackend"] = [send_result]
        script["list-commands"] = list(lookups)
        return script

    def test_lost_response_tracks_the_single_matching_command(self):
        for send_result in (aws_error("InternalServerError"), Result(0, "not json"), ok({"Command": {}}),
                            subprocess.TimeoutExpired("aws", 60)):
            with self.subTest(send_result=send_result):
                fake = FakeAws(**self.script(send_result, self.listed(self.command(Comment="other run"),
                                                                       self.command())))
                code, stdout, _, _ = run_execute(fake, execute_env(OPERATION="deploy"))
                self.assertEqual(code, 0, stdout)
                self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 1)
                self.assertIn("without resending", stdout)
                argv = next(argv for argv, _ in fake.calls if argv[2] == "list-commands")
                filters = json.loads(argv[argv.index("--filters") + 1])
                self.assertIn({"key": "InvokedAfter", "value": "2026-11-26T02:58:00Z"}, filters)

    def test_command_found_after_eventual_consistency_delay(self):
        fake = FakeAws(**self.script(aws_error("ThrottlingException"), self.listed(), self.listed(self.command())))
        code, stdout, _, _ = run_execute(fake, execute_env(OPERATION="deploy"))
        self.assertEqual(code, 0, stdout)
        self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 1)

    def test_not_found_stops_without_resending(self):
        fake = FakeAws(**self.script(aws_error("InternalServerError"), self.listed(), self.listed(), self.listed()))
        code, stdout, summary, _ = run_execute(fake, execute_env(OPERATION="deploy"))
        self.assertEqual(code, 1)
        self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 1)
        self.assertEqual(fake.count("list-commands"), 3)
        self.assertIn("not resending", stdout)
        self.assertNotIn(SECRET, stdout + summary)

    def test_ambiguous_or_mismatched_commands_stop(self):
        cases = [
            self.listed(self.command(), self.command(CommandId=POST_CMD)),
            self.listed(self.command(DocumentVersion="2")),
            self.listed(self.command(InstanceIds=["i-0fedcba9876543210"])),
            self.listed(self.command(Parameters={"ImageDigest": [OTHER_DIGEST]})),
            self.listed(self.command(CommandId="not-an-id")),
        ]
        for lookup in cases:
            with self.subTest(lookup=lookup.stdout[:80]):
                fake = FakeAws(**self.script(aws_error("InternalServerError"), lookup))
                code, _, _, _ = run_execute(fake, execute_env(OPERATION="deploy"))
                self.assertEqual(code, 1)
                self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 1)
                self.assertEqual(fake.count(f"get-command-invocation:{DEPLOY_CMD}"), 0)

    def test_lookup_failure_stops(self):
        fake = FakeAws(**self.script(aws_error("InternalServerError"), aws_error("AccessDeniedException")))
        code, stdout, _, _ = run_execute(fake, execute_env(OPERATION="deploy"))
        self.assertEqual(code, 1)
        self.assertIn("AccessDeniedException", stdout)
        self.assertEqual(fake.count("send-command:ToI-DeployBackend"), 1)

    def test_status_command_lost_response_is_not_resent(self):
        script = base_script()
        script["send-command:ToI-DeploymentStatus"] = [aws_error("InternalServerError")]
        script["list-commands"] = [self.listed(), self.listed(), self.listed()]
        fake = FakeAws(**script)
        code, _, _, _ = run_execute(fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.count("send-command:ToI-DeploymentStatus"), 1)


class WorkflowAndPolicyTests(unittest.TestCase):
    WORKFLOW = (ROOT / bd.WORKFLOW).read_text(encoding="utf-8")
    POLICY = json.loads((ROOT / "deploy" / "iam" / "ssm-deploy-policy.json").read_text(encoding="utf-8"))

    def job(self, name):
        match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", self.WORKFLOW, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_workflow_triggers_permissions_and_concurrency(self):
        text = self.WORKFLOW
        self.assertRegex(text, r"(?m)^on:\n  workflow_dispatch:\n")
        self.assertRegex(text, r"(?m)^permissions: \{\}$")
        self.assertRegex(text, r"(?m)^  group: toi-be-backend-deploy\n  cancel-in-progress: false$")
        self.assertRegex(text, r"default: preflight")
        self.assertEqual(text.count("id-token: write"), 1)
        self.assertIn("id-token: write", self.job("deploy"))
        self.assertNotIn("id-token", self.job("validate"))
        for job in ("validate", "deploy"):
            self.assertIn("github.ref == 'refs/heads/main' && github.ref_protected", self.job(job))

    def test_workflow_uses_only_the_dedicated_role_and_documents(self):
        text = self.WORKFLOW
        self.assertIn("role-to-assume: ${{ vars.SSM_DEPLOY_ROLE_ARN }}", text)
        self.assertNotIn("AWS_ROLE_ARN", text.replace("SSM_DEPLOY_ROLE_ARN", ""))
        for forbidden in ("AWS-RunShellScript", "commands", "secrets.", "cancel-command"):
            self.assertNotIn(forbidden, text)
        self.assertNotRegex(text, r"run: .*\$\{\{ inputs\.")

    def test_policy_is_least_privilege(self):
        statements = self.POLICY["Statement"]
        actions = sorted(a for s in statements for a in ([s["Action"]] if isinstance(s["Action"], str)
                                                         else s["Action"]))
        self.assertEqual(actions, ["ecr:DescribeImages", "ssm:GetCommandInvocation", "ssm:ListCommands",
                                   "ssm:SendCommand"])
        self.assertTrue(all(s["Effect"] == "Allow" for s in statements))
        send = next(s for s in statements if s["Action"] == "ssm:SendCommand")
        self.assertEqual(sorted(r.rsplit("/", 1)[1] for r in send["Resource"]),
                         ["REPLACE_WITH_EC2_INSTANCE_ID", "ToI-DeployBackend", "ToI-DeploymentStatus"])
        self.assertTrue(all(":ap-northeast-2:" in r for r in send["Resource"]))
        ecr = next(s for s in statements if s["Action"] == "ecr:DescribeImages")
        self.assertTrue(ecr["Resource"].endswith(":repository/toi-be"))
        wildcard = [s for s in statements if s["Resource"] == "*"]
        self.assertEqual([sorted(s["Action"]) for s in wildcard], [["ssm:GetCommandInvocation", "ssm:ListCommands"]])


if __name__ == "__main__":
    unittest.main()
