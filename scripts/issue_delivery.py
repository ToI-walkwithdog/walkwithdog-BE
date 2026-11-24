"""Prepare a worktree or deliver one approved dev PR using authenticated GitHub CLI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from scripts.dev_gate import GateError, OBJECT_ID, REGISTRY, git, load_json, marker, parse_scope, paths


REPO = "ToI-walkwithdog/walkwithdog-BE"
ENABLE = "TOI_ISSUE_DELIVERY_ENABLED"
WORKFLOW = ".github/workflows/dev-ci.yml"
CHECK = "Dev quality gate"
APPROVAL = re.compile(r"<!-- toi-approval\n(.*?)\n-->", re.DOTALL)
CODEX_REQUEST = re.compile(r"```toi-approval-request\n(.*?)\n```", re.DOTALL)
CODEX_THREAD = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
REVIEW = re.compile(r"<!-- toi-review\n(.*?)\n-->", re.DOTALL)
PROTECTED = (".github/", "docs/dev-automation/", "scripts/dev_", "scripts/issue_delivery.py",
             "scripts/check_migrations.py", "tests/test_dev_delivery.py", "CLAUDE.md", "AGENTS.md", ".gitignore")
TEST_CONFIG = {"conftest.py", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "requirements.txt"}


def gh(endpoint, *args):
    return json.loads(subprocess.check_output(["gh", "api", "--hostname", "github.com", endpoint, *args]))


def api(path, *args):
    return gh(f"repos/{REPO}/{path}", *args)


def rows(path):
    return [row for page in api(path, "--paginate", "--slurp") for row in page]


def variable(name):
    return api(f"actions/variables/{name}")["value"]


def logins(name):
    return {login.strip().lower() for login in variable(name).split(",") if login.strip()}


def positive(number):
    value = int(number)
    if value < 1:
        raise argparse.ArgumentTypeError("Expected a positive GitHub identifier")
    return value


def issue_digest(title, body):
    raw = json.dumps([title, (body or "").replace("\r\n", "\n")], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def read_approval(comment_id):
    comment = api(f"issues/comments/{comment_id}")
    user = comment["user"]["login"].lower()
    if comment["user"]["type"] != "User" or user not in logins("TOI_ISSUE_APPROVERS"):
        raise GateError("Approval writer is not in the repository's trusted approver list")
    approval = marker(comment["body"], APPROVAL)
    if (set(approval) != {"issue", "issue_digest", "paths"} or not
            re.fullmatch(r"[a-f0-9]{64}", approval["issue_digest"])):
        raise GateError("Approval requires issue, issue_digest and exact paths")
    paths(approval["paths"])
    if approval["issue"] is None:
        inbox = positive(variable("TOI_ISSUE_APPROVAL_INBOX"))
        if not comment["issue_url"].endswith(f"/issues/{inbox}"):
            raise GateError("New-issue approval must come from the configured intake issue")
    elif (type(approval["issue"]) is not int or approval["issue"] < 1
          or not comment["issue_url"].endswith(f"/issues/{approval['issue']}")):
        raise GateError("Approval comment must belong to the approved Issue")
    return approval, comment["created_at"]


def codex_session(thread):
    if not isinstance(thread, str) or not CODEX_THREAD.fullmatch(thread):
        raise GateError("A verified Codex thread ID is required")
    root = (Path.home() / ".codex" / "sessions").resolve()
    matches = list(root.glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    if len(matches) != 1 or matches[0].resolve() != matches[0] or not matches[0].is_file():
        raise GateError("The original local Codex session log is missing or ambiguous")
    return matches[0]


def codex_approval_message(issue):
    return (f"Issue #{issue}의 확정된 요구사항과 변경 경로를 확인했어. "
            "해당 Issue의 구현, 독립 리뷰, 테스트, PR 생성, 조건부 dev 병합까지 승인해.")


def read_codex_approval(issue, thread, timestamp=None):
    """Bind one user event to the preceding proposal in the same local Codex session."""
    matched_session = False
    previous_proposal = None
    selected = None
    with codex_session(thread).open(encoding="utf-8") as rollout:
        for line in rollout:
            if not line.endswith("\n"):
                break  # The runtime may still be appending the last record.
            record = json.loads(line)
            payload = record.get("payload", {})
            if record["type"] == "session_meta":
                if payload["id"] != thread:
                    raise GateError("Codex session metadata does not match the thread")
                matched_session = True
            if record["type"] != "event_msg":
                continue
            if payload["type"] == "agent_message" and payload.get("phase") == "final_answer":
                previous_proposal = payload.get("message")
            if payload["type"] == "user_message":
                if timestamp is None or record["timestamp"] == timestamp:
                    if timestamp is not None and selected is not None:
                        raise GateError("The recorded Codex approval is ambiguous")
                    selected = (record["timestamp"], payload.get("message"), previous_proposal)
                previous_proposal = None

    if not matched_session or selected is None or not selected[0]:
        raise GateError("The original Codex user approval event is missing")
    approved_at, prompt, proposal = selected
    if not isinstance(prompt, str) or prompt.strip() != codex_approval_message(issue):
        raise GateError("The Codex user message is not explicit approval for this Issue")
    if not isinstance(proposal, str):
        raise GateError("The Codex approval must immediately follow the approved scope proposal")
    scope = marker(proposal, CODEX_REQUEST)
    if (set(scope) != {"repository", "issue", "title", "issue_digest", "paths"}
            or scope["repository"] != REPO or type(scope["issue"]) is not int or scope["issue"] != issue
            or not isinstance(scope["title"], str) or not scope["title"].strip()
            or not isinstance(scope["issue_digest"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", scope["issue_digest"])):
        raise GateError("The preceding Codex proposal must identify this repository, Issue and exact scope")
    return {**scope, "paths": sorted(paths(scope["paths"])), "thread": thread,
            "timestamp": approved_at, "message": prompt}


def saved_codex_approval(state):
    saved = state.get("codex_approval")
    if not isinstance(saved, dict) or "approval_comment" in state or not saved.get("timestamp"):
        raise GateError("An unambiguous local Codex approval record is required")
    current = read_codex_approval(state["issue"], saved["thread"], saved["timestamp"])
    if current != saved:
        raise GateError("The saved Codex approval no longer matches the original session log")
    return current


def check_issue(issue, approval, approval_time):
    if ("pull_request" in issue or issue["state"] != "open"
            or ("title" in approval and issue["title"] != approval["title"])
            or issue_digest(issue["title"], issue["body"]) != approval["issue_digest"]):
        raise GateError("Issue is closed or its approved title/body has changed")
    if approval["issue"] is None:
        if issue["created_at"] < approval_time:
            raise GateError("Issue predates the approved draft")
    elif issue["number"] != approval["issue"]:
        raise GateError("Approval belongs to another Issue")


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def initial_state(args, issue, approval):
    return {"issue": issue, "approval_comment": args.approval_comment,
            "issue_digest": approval["issue_digest"], "paths": sorted(approval["paths"])}


def require_enabled():
    if variable(ENABLE) != "true":
        raise GateError(f"{ENABLE} is disabled")


def register(args):
    require_enabled()
    approval, approved_at = read_approval(args.approval_comment)
    draft = load_json(args.draft)
    if approval["issue"] is not None or issue_digest(draft["title"], draft["body"]) != approval["issue_digest"]:
        raise GateError("Draft differs from the approved new Issue")
    pending = {"approval_comment": args.approval_comment, "issue_digest": approval["issue_digest"],
               "paths": sorted(approval["paths"])}
    recorded = load_json(args.state) if args.state.exists() else None
    if recorded is not None:
        if any(recorded.get(key) != value for key, value in pending.items()):
            raise GateError("State already belongs to another task")
        if "issue" in recorded:
            issue = api(f"issues/{recorded['issue']}")
            check_issue(issue, approval, approved_at)
            print(issue["html_url"])
            return
    # Recover an exact register result before attempting another write. The operator
    # still searches related issues during the approved requirement review.
    matches = [item for item in rows("issues?state=all&per_page=100")
               if "pull_request" not in item and item["title"] == draft["title"]]
    if matches:
        if len(matches) != 1:
            raise GateError("Matching Issue titles require manual reconciliation")
        issue = matches[0]
        check_issue(issue, approval, approved_at)
    else:
        if recorded is not None:
            raise GateError("Previous Issue write has an unknown outcome; reconcile it on GitHub")
        args.state.parent.mkdir(parents=True, exist_ok=True)
        with args.state.open("x", encoding="utf-8") as stream:
            json.dump(pending, stream)
        issue = api("issues", "--method", "POST", "-f", f"title={draft['title']}", "-f", f"body={draft['body']}")
        check_issue(issue, approval, approved_at)
    write_state(args.state, initial_state(args, issue["number"], approval))
    print(issue["html_url"])


def dev_sha():
    sha = api("git/ref/heads/dev")["object"]["sha"]
    if not OBJECT_ID.fullmatch(sha):
        raise GateError("GitHub did not return a full dev SHA")
    return sha


def prepare(args):
    previous = load_json(args.state) if args.state.exists() else None
    if args.codex_approval:
        approval = (saved_codex_approval(previous) if previous is not None
                    else read_codex_approval(args.issue, os.environ.get("CODEX_THREAD_ID")))
        approved_at = approval["timestamp"]
        state = {"issue": args.issue, "issue_digest": approval["issue_digest"],
                 "paths": approval["paths"], "codex_approval": approval}
    else:
        if previous is not None and "codex_approval" in previous:
            raise GateError("Resume the original Codex approval source")
        approval, approved_at = read_approval(args.approval_comment)
        state = initial_state(args, args.issue, approval)
    check_issue(api(f"issues/{args.issue}"), approval, approved_at)
    if previous is None:
        previous = state
    if any(previous.get(key) != value for key, value in state.items()):
        raise GateError("State already belongs to another task")
    if approval["issue"] is None and not args.state.exists():
        raise GateError("Use register to bind the approved new Issue first")
    remote = git("remote", "get-url", "origin").decode().strip().removesuffix(".git")
    if remote not in {f"https://github.com/{REPO}", f"git@github.com:{REPO}", f"ssh://git@github.com/{REPO}"}:
        raise GateError("origin must point to the approved repository")
    target = args.worktree.resolve()
    branch = f"issue/{args.issue}"
    if args.state.resolve().is_relative_to(target):
        raise GateError("Keep the state file outside the feature worktree")
    if "worktree" in previous and previous["worktree"] != str(target):
        raise GateError("Resume the originally recorded worktree")
    if target.exists():
        branch_name = git("-C", str(target), "branch", "--show-current").decode().strip()
        top = Path(git("-C", str(target), "rev-parse", "--show-toplevel").decode().strip()).resolve()
        common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
        selected = Path(git("-C", str(target), "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
        if branch_name != branch or top != target or common != selected:
            raise GateError("Existing worktree is not the recorded Issue branch in this clone")
        write_state(args.state, {**previous, "worktree": str(target), "branch": branch})
        print(f"Resume {branch} at {target}")
        return
    if "worktree" in previous:
        raise GateError("Recorded worktree is missing; recover its branch before continuing")
    base = dev_sha()
    git("fetch", "--no-tags", "origin", "refs/heads/dev:refs/remotes/origin/dev")
    if git("rev-parse", "refs/remotes/origin/dev").decode().strip() != base or dev_sha() != base:
        raise GateError("dev moved during fetch; retry preparation")
    git("worktree", "add", "-b", branch, str(target), base)
    write_state(args.state, {**state, "base": base, "branch": branch, "worktree": str(target)})
    print(f"Prepared {branch} at {base} in {target}")


def check_protection(protection, branch="dev"):
    reviews = protection.get("required_pull_request_reviews") or {}
    bypass = reviews.get("bypass_pull_request_allowances") or {}
    if (protection.get("enforce_admins", {}).get("enabled") is not True
            or protection.get("allow_force_pushes", {}).get("enabled") is not False
            or protection.get("allow_deletions", {}).get("enabled") is not False
            or reviews.get("required_approving_review_count") != 0
            or reviews.get("require_code_owner_reviews")
            or reviews.get("require_last_push_approval")
            or any(bypass.get(kind) for kind in ("users", "teams", "apps"))):
        raise GateError(f"{branch} must require PRs with zero GitHub approvals, no bypass, "
                        "no force pushes and no branch deletion")
    if branch == "dev":
        status = protection.get("required_status_checks") or {}
        if (not status.get("strict")
                or not any(check["context"] == CHECK and check.get("app_id") == 15368
                           for check in status.get("checks", []))
                or protection.get("required_linear_history", {}).get("enabled")):
            raise GateError("dev must require the up-to-date GitHub Actions check and allow merge commits")


def check_reviews(body, number, head):
    evidence = marker(body, REVIEW)
    if (set(evidence) != {"issue", "head", "context", "summary", "blockers"}
            or type(evidence["issue"]) is not int or evidence["issue"] != number
            or not OBJECT_ID.fullmatch(head) or evidence["head"] != head
            or not all(isinstance(evidence[key], str) and evidence[key].strip()
                       for key in ("context", "summary"))
            or evidence["blockers"] != []):
        raise GateError("PR needs a separate-context review record for this Issue and exact Head "
                        "with a summary and no blockers")
    # GitHub records the PR body editor, not the identity of the agent that produced this evidence.


def check_pr(pr, state, actual):
    issue, declared = parse_scope(pr["body"])
    if (pr["base"]["ref"] != "dev" or pr["head"]["repo"]["full_name"] != REPO
            or pr["base"]["repo"]["full_name"] != REPO
            or pr["head"]["ref"] != f"issue/{state['issue']}" or issue != state["issue"]):
        raise GateError("PR repository, Issue or branch does not match the approved scope")
    check_change_scope(state, actual, declared)
    if pr.get("auto_merge"):
        raise GateError("Unexpected GitHub auto-merge request")


def check_change_scope(state, actual, declared):
    if declared != actual or not actual <= set(state["paths"]):
        raise GateError("Exact changed paths do not match the approved scope")
    if any(path.rsplit("/", 1)[-1] in TEST_CONFIG
           or (path.startswith(PROTECTED) and path != REGISTRY.as_posix()) for path in actual):
        raise GateError("Maintenance or gate changes require separate human review and merge")


def check_live_approval(state):
    if ("approval_comment" in state) == ("codex_approval" in state):
        raise GateError("Exactly one recorded approval source is required")
    if "codex_approval" in state:
        approval = saved_codex_approval(state)
        approved_at = approval["timestamp"]
    else:
        approval, approved_at = read_approval(state["approval_comment"])
    if (approval["issue_digest"] != state["issue_digest"]
            or paths(approval["paths"]) != set(state["paths"])):
        raise GateError("Approval changed since the worktree was prepared")
    check_issue(api(f"issues/{state['issue']}"), approval, approved_at)


def open_pr(args):
    require_enabled()
    state = load_json(args.state)
    branch = f"issue/{state['issue']}"
    if state.get("branch") != branch:
        raise GateError("State does not belong to a prepared Issue branch")
    check_live_approval(state)
    candidates = rows(f"pulls?state=all&base=dev&head={REPO.split('/')[0]}:{branch}&per_page=100")
    if len(candidates) > 1:
        raise GateError("Multiple PRs exist on the Issue branch; reconcile on GitHub")
    if candidates:
        pr = candidates[0]
        if state.get("pr", pr["number"]) != pr["number"] or (pr["state"] != "open" and not pr.get("merged_at")):
            raise GateError("Existing PR needs reconciliation; no new PR was created")
        actual = pull_paths(pr)
        check_pr(pr, state, actual)
    else:
        if "pr" in state:
            raise GateError("Recorded PR is missing from GitHub; reconcile before retrying")
        pending = args.state.with_suffix(args.state.suffix + ".pr-pending")
        if pending.exists():
            raise GateError("Previous PR write has an unknown outcome; reconcile it on GitHub")
        body = args.body.read_text(encoding="utf-8")
        issue, declared = parse_scope(body)
        if issue != state["issue"]:
            raise GateError("PR body must link the prepared Issue")
        base = dev_sha()
        head = api(f"git/ref/heads/{branch}")["object"]["sha"]
        comparison = api(f"compare/{base}...{head}")
        files = comparison["files"]
        if (comparison["merge_base_commit"]["sha"] != base or not files or len(files) >= 300):
            raise GateError("Push an up-to-date branch with a complete file inventory before creating its PR")
        actual = file_paths(files)
        check_change_scope(state, actual, declared)
        with pending.open("x", encoding="utf-8") as stream:
            json.dump({"branch": branch, "head": head}, stream)
        pr = api("pulls", "--method", "POST", "-f", f"title={args.title}", "-f", f"body={body}",
                 "-f", f"head={branch}", "-f", "base=dev", "-F", "draft=true")
        if pr["head"]["sha"] != head:
            raise GateError("PR Head changed during creation; reconcile the existing PR")
    write_state(args.state, {**state, "pr": pr["number"]})
    print(pr["html_url"])


def file_paths(files):
    return {path for item in files for path in (item["filename"], item.get("previous_filename")) if path}


def pull_paths(pr):
    files = rows(f"pulls/{pr['number']}/files?per_page=100")
    if len(files) != pr["changed_files"]:
        raise GateError("The GitHub files inventory is incomplete")
    return file_paths(files)


def verify_merge(pr, head):
    merge = pr["merge_commit_sha"]
    if not pr["merged"] or not merge or not OBJECT_ID.fullmatch(merge):
        raise GateError("No verified merge receipt on GitHub")
    commit = api(f"git/commits/{merge}")
    if len(commit["parents"]) != 2 or commit["parents"][1]["sha"] != head:
        raise GateError("Merged commit did not preserve the expected Head")
    expected_tree = api(f"git/commits/{head}")["tree"]["sha"]
    if commit["tree"]["sha"] != expected_tree:
        raise GateError("Merged tree differs from the reviewed Head")
    latest = dev_sha()
    if api(f"compare/{merge}...{latest}")["status"] not in {"identical", "ahead"}:
        raise GateError("Merge is not reachable from current dev")
    return {"merge_sha": merge, "dev_sha": latest}


def check_current_pr(pr, base, head):
    if (pr["state"] != "open" or pr["draft"] or pr["base"]["sha"] != base or pr["mergeable"] is not True
            or pr["mergeable_state"] != "clean"
            or api(f"compare/{base}...{head}")["merge_base_commit"]["sha"] != base):
        raise GateError("PR must be ready, CLEAN and include the current dev commit")


def check_ci(number, base, head):
    runs = api(f"actions/workflows/dev-ci.yml/runs?head_sha={head}&event=pull_request&per_page=20")["workflow_runs"]
    if not runs:
        raise GateError(f"No {WORKFLOW} run for the exact Head")
    run = runs[0]
    if (run["path"].split("@")[0] != WORKFLOW or run["head_sha"] != head or run["conclusion"] != "success"
            or not any(item["number"] == number and item["base"]["sha"] == base for item in run["pull_requests"])):
        raise GateError("Latest CI did not pass for this PR and exact base/head pair")
    jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    if not any(job["name"] == CHECK and job["conclusion"] == "success" for job in jobs):
        raise GateError("Required CI job did not pass")
    return run


def deliver(args):
    pr = api(f"pulls/{args.pr}")
    if parse_scope(pr["body"])[0] is None:
        raise GateError("Manual maintenance PRs are ineligible for Issue delivery")
    state = load_json(args.state)
    head = pr["head"]["sha"]
    if not OBJECT_ID.fullmatch(head):
        raise GateError("PR head is not a full Git SHA")
    if state.get("pr", args.pr) != args.pr or not state.get("branch"):
        raise GateError("State does not match the prepared PR")
    check_pr(pr, state, pull_paths(pr))
    if pr["merged"]:
        result = verify_merge(pr, head)
        write_state(args.state, {**state, "pr": args.pr, "verified_head": head, **result})
        print(json.dumps(result))
        return
    check_live_approval(state)
    base = dev_sha()
    check_current_pr(pr, base, head)
    check_protection(api("branches/dev/protection"))
    check_protection(api("branches/main/protection"), "main")
    check_reviews(pr["body"], state["issue"], head)
    run = check_ci(args.pr, base, head)
    if not args.execute:
        print(f"Verified PR #{args.pr} at {head}. Preview only.")
        return
    require_enabled()
    check_live_approval(state)
    current = api(f"pulls/{args.pr}")
    if (dev_sha() != base or current["base"]["sha"] != base or current["head"]["sha"] != head
            or current["mergeable_state"] != "clean" or current["state"] != "open"
            or current["draft"] or current["body"] != pr["body"]):
        raise GateError("PR or dev changed before merge; re-run the gate")
    write_state(args.state, {**state, "pr": args.pr, "verified_head": head, "ci_run": run["id"]})
    result = api(f"pulls/{args.pr}/merge", "--method", "PUT", "-f", "merge_method=merge", "-f", f"sha={head}")
    if not result["merged"]:
        raise GateError("GitHub refused the merge")
    receipt = verify_merge(api(f"pulls/{args.pr}"), head)
    if result["sha"] != receipt["merge_sha"]:
        raise GateError("GitHub receipts disagree")
    write_state(args.state, {**load_json(args.state), **receipt})
    print(json.dumps(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    digest = commands.add_parser("digest")
    digest.add_argument("draft", type=Path)
    create = commands.add_parser("register")
    create.add_argument("draft", type=Path)
    setup = commands.add_parser("prepare")
    setup.add_argument("issue", type=positive)
    setup.add_argument("worktree", type=Path)
    pr = commands.add_parser("pr")
    pr.add_argument("--title", required=True)
    pr.add_argument("--body", type=Path, required=True)
    merge = commands.add_parser("merge")
    merge.add_argument("pr", type=positive)
    merge.add_argument("--execute", action="store_true")
    for command in (create, setup, pr, merge):
        command.add_argument("--state", type=Path, required=True)
    create.add_argument("--approval-comment", type=positive, required=True)
    source = setup.add_mutually_exclusive_group(required=True)
    source.add_argument("--approval-comment", type=positive)
    source.add_argument("--codex-approval", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "digest":
            draft = load_json(args.draft)
            print(issue_digest(draft["title"], draft["body"]))
        else:
            {"register": register, "prepare": prepare, "pr": open_pr, "merge": deliver}[args.command](args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Delivery blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
