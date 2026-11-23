"""Prepare a worktree or deliver one approved dev PR using authenticated GitHub CLI."""

import argparse
import hashlib
import json
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
REVIEW = re.compile(r"<!-- toi-review\n(.*?)\n-->", re.DOTALL)
PROTECTED = (".github/", "docs/dev-automation/", "scripts/dev_", "scripts/issue_delivery.py",
             "scripts/check_migrations.py", "tests/test_dev_delivery.py", "CLAUDE.md", "AGENTS.md", ".gitignore")


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
    if user not in logins("TOI_ISSUE_APPROVERS"):
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


def check_issue(issue, approval, approval_time):
    if ("pull_request" in issue or issue["state"] != "open"
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
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
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
    approval, approved_at = read_approval(args.approval_comment)
    check_issue(api(f"issues/{args.issue}"), approval, approved_at)
    state = initial_state(args, args.issue, approval)
    previous = load_json(args.state) if args.state.exists() else state
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


def check_protection(protection):
    status = protection["required_status_checks"]
    reviews = protection.get("required_pull_request_reviews") or {}
    if (not status["strict"] or not any(check["context"] == CHECK and check.get("app_id") == 15368
                                       for check in status["checks"])
            or not protection["enforce_admins"]["enabled"]
            or protection["allow_force_pushes"]["enabled"]
            or protection["allow_deletions"]["enabled"]
            or reviews.get("required_approving_review_count", 0) < 1
            or not reviews.get("dismiss_stale_reviews")
            or not reviews.get("require_last_push_approval")
            or reviews.get("bypass_pull_request_allowances", {}).get("users")
            or reviews.get("bypass_pull_request_allowances", {}).get("teams")
            or reviews.get("bypass_pull_request_allowances", {}).get("apps")):
        raise GateError("dev must enforce strict GitHub Actions checks, first-party review and branch protection")


def check_reviews(reviews, trusted, independent, number, head):
    latest = {}
    for review in reviews:
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["login"].lower()] = review
    if any(review["state"] == "CHANGES_REQUESTED" and name in trusted for name, review in latest.items()):
        raise GateError("An independent reviewer still requests changes")
    for name, review in latest.items():
        if (name in independent and review["state"] == "APPROVED" and review["commit_id"] == head):
            try:
                evidence = marker(review["body"], REVIEW)
            except (ValueError, TypeError):
                continue
            if evidence == {"issue": number, "head": head, "blockers": []}:
                return
    raise GateError("No independent approval of this exact head with an empty blockers list")


def check_pr(pr, state, actual):
    issue, declared = parse_scope(pr["body"])
    if (pr["base"]["ref"] != "dev" or pr["head"]["repo"]["full_name"] != REPO
            or pr["base"]["repo"]["full_name"] != REPO
            or pr["head"]["ref"] != f"issue/{state['issue']}"
            or issue != state["issue"] or declared != actual or not actual <= set(state["paths"])):
        raise GateError("Repository, Issue, branch or exact changed paths do not match the approved scope")
    if any(path.startswith(PROTECTED) and path != REGISTRY.as_posix() for path in actual):
        raise GateError("Maintenance or gate changes require separate human review and merge")
    if pr.get("auto_merge"):
        raise GateError("Unexpected GitHub auto-merge request")


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


def deliver(args):
    state = load_json(args.state)
    pr = api(f"pulls/{args.pr}")
    head = pr["head"]["sha"]
    if not OBJECT_ID.fullmatch(head):
        raise GateError("PR head is not a full Git SHA")
    if state.get("pr", args.pr) != args.pr or not state.get("branch"):
        raise GateError("State does not match the prepared PR")
    files = rows(f"pulls/{args.pr}/files?per_page=100")
    actual = {path for item in files for path in (item["filename"], item.get("previous_filename")) if path}
    if len(files) != pr["changed_files"]:
        raise GateError("The GitHub files inventory is incomplete")
    check_pr(pr, state, actual)
    if pr["merged"]:
        result = verify_merge(pr, head)
        write_state(args.state, {**state, "pr": args.pr, "verified_head": head, **result})
        print(json.dumps(result))
        return
    approval, approved_at = read_approval(state["approval_comment"])
    if (approval["issue_digest"] != state["issue_digest"]
            or paths(approval["paths"]) != set(state["paths"])):
        raise GateError("Approval changed since the worktree was prepared")
    check_issue(api(f"issues/{state['issue']}"), approval, approved_at)
    base = dev_sha()
    if (pr["state"] != "open" or pr["draft"] or pr["base"]["sha"] != base or pr["mergeable"] is not True
            or pr["mergeable_state"] != "clean"
            or api(f"compare/{base}...{head}")["merge_base_commit"]["sha"] != base):
        raise GateError("PR must be ready, CLEAN and include the current dev commit")
    check_protection(api("branches/dev/protection"))
    actor = gh("user")["login"].lower()
    author = pr["user"]["login"].lower()
    trusted = logins("TOI_ISSUE_REVIEWERS")
    independent = {name for name in trusted - {actor, author}
                   if api(f"collaborators/{name}/permission")["permission"] in {"write", "maintain", "admin"}}
    check_reviews(rows(f"pulls/{args.pr}/reviews?per_page=100"), trusted, independent, state["issue"], head)

    runs = api(f"actions/workflows/dev-ci.yml/runs?head_sha={head}&event=pull_request&per_page=20")["workflow_runs"]
    if not runs:
        raise GateError(f"No {WORKFLOW} run for the exact Head")
    run = runs[0]
    if (run["path"].split("@")[0] != WORKFLOW or run["head_sha"] != head or run["conclusion"] != "success"
            or not any(item["number"] == args.pr and item["base"]["sha"] == base for item in run["pull_requests"])):
        raise GateError("Latest CI did not pass for this PR and exact base/head pair")
    jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    if not any(job["name"] == CHECK and job["conclusion"] == "success" for job in jobs):
        raise GateError("Required CI job did not pass")
    if not args.execute:
        print(f"Verified PR #{args.pr} at {head}. Preview only.")
        return
    require_enabled()
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
    merge = commands.add_parser("merge")
    merge.add_argument("pr", type=positive)
    merge.add_argument("--execute", action="store_true")
    for command in (create, setup, merge):
        command.add_argument("--state", type=Path, required=True)
    for command in (create, setup):
        command.add_argument("--approval-comment", type=positive, required=True)
    args = parser.parse_args()
    try:
        if args.command == "digest":
            draft = load_json(args.draft)
            print(issue_digest(draft["title"], draft["body"]))
        else:
            {"register": register, "prepare": prepare, "merge": deliver}[args.command](args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Delivery blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
