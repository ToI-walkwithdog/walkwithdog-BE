"""Prepare or deliver one approved dev PR using authenticated GitHub CLI."""

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
CLAUDE_SESSION = "CLAUDE_CODE_SESSION_ID"
# Editor context the VS Code extension attaches to a prompt as separate text blocks.
CLAUDE_IDE_CONTEXT = re.compile(r"<(ide_opened_file|ide_selection)>.*</\1>", re.DOTALL)
CLAUDE_PROMPT_SOURCES = {"sdk", "typed"}
CLAUDE_TURN_MARKERS = {"turn_duration", "away_summary"}
CONVERSATION_SOURCES = ("codex_approval", "claude_approval")
MODES = ("main", "worktree")
# Approved branch names: <type>/<lowercase kebab-case purpose>, no Issue number required.
BRANCH = re.compile(r"(feature|fix|refactor|chore|test|docs)/[a-z0-9]+(?:-[a-z0-9]+)*")
BRANCH_MAX_LENGTH = 80
AI_TOOL_NAMES = ("codex", "claude", "chatgpt", "gpt", "openai", "anthropic", "copilot", "gemini")
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


def branch_name(value):
    if (not isinstance(value, str) or len(value) > BRANCH_MAX_LENGTH or not BRANCH.fullmatch(value)
            or any(name in value for name in AI_TOOL_NAMES)):
        raise GateError("The approved branch must be <type>/<lowercase-kebab-name> (feature, fix, refactor, "
                        "chore, test or docs) without AI tool names")
    return value


def approved_branch(approval, issue):
    """The branch an approval names. Records approved before branch names were part of the
    approval have no "branch" field and keep the former issue/<number> branch."""
    if "branch" not in approval:
        return f"issue/{issue}"
    return branch_name(approval["branch"])


def work_mode(value):
    if value not in MODES:
        raise GateError("Work mode must be main or worktree")
    return value


def approved_mode(approval, state):
    if "mode" in approval:
        branch_name(approval.get("branch"))
        return work_mode(approval["mode"])
    # Approvals predating work modes keep their already recorded worktree.
    return "worktree" if "worktree" in state and "mode" not in state else "main"


def check_branch_available(branch):
    """A new Issue branch must not exist locally or on GitHub, and no PR may have used it."""
    if git("branch", "--list", branch).strip():
        raise GateError("The approved branch already exists in this clone; resume its state or approve another name")
    if git("ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
        raise GateError("The approved branch already exists on GitHub; approve another name")
    if rows(f"pulls?state=all&head={REPO.split('/')[0]}:{branch}&per_page=100"):
        raise GateError("A pull request already used the approved branch name; approve another name")


def read_approval(comment_id):
    comment = api(f"issues/comments/{comment_id}")
    user = comment["user"]["login"].lower()
    if comment["user"]["type"] != "User" or user not in logins("TOI_ISSUE_APPROVERS"):
        raise GateError("Approval writer is not in the repository's trusted approver list")
    approval = marker(comment["body"], APPROVAL)
    if (set(approval) - {"branch", "mode"} != {"issue", "issue_digest", "paths"} or not
            re.fullmatch(r"[a-f0-9]{64}", approval["issue_digest"])):
        raise GateError("Approval requires issue, issue_digest and exact paths, with branch and mode for new work")
    paths(approval["paths"])
    if "branch" in approval:
        branch_name(approval["branch"])
    approved_mode(approval, {})
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


def approval_message(issue):
    return (f"Issue #{issue}의 확정된 요구사항과 변경 경로를 확인했어. "
            "해당 Issue의 구현, 독립 리뷰, 테스트, PR 생성, 조건부 dev 병합까지 승인해.")


codex_approval_message = approval_message


def conversation_scope(proposal, issue, tool):
    scope = marker(proposal, CODEX_REQUEST)
    if (set(scope) - {"branch", "mode"} != {"repository", "issue", "title", "issue_digest", "paths"}
            or scope["repository"] != REPO or type(scope["issue"]) is not int or scope["issue"] != issue
            or not isinstance(scope["title"], str) or not scope["title"].strip()
            or not isinstance(scope["issue_digest"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", scope["issue_digest"])):
        raise GateError(f"The preceding {tool} proposal must identify this repository, Issue and exact scope")
    if "branch" in scope:
        branch_name(scope["branch"])
    approved_mode(scope, {})
    return {**scope, "paths": sorted(paths(scope["paths"]))}


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
    return {**conversation_scope(proposal, issue, "Codex"), "thread": thread,
            "timestamp": approved_at, "message": prompt}


def saved_codex_approval(state):
    saved = state.get("codex_approval")
    if (not isinstance(saved, dict) or "approval_comment" in state or "claude_approval" in state
            or not saved.get("timestamp")):
        raise GateError("An unambiguous local Codex approval record is required")
    current = read_codex_approval(state["issue"], saved["thread"], saved["timestamp"])
    if current != saved:
        raise GateError("The saved Codex approval no longer matches the original session log")
    return current


def claude_session(session):
    if not isinstance(session, str) or not CODEX_THREAD.fullmatch(session):
        raise GateError("A verified Claude Code session ID is required")
    root = (Path.home() / ".claude" / "projects").resolve()
    # Subagent transcripts live in deeper folders and never match this pattern.
    matches = list(root.glob(f"*/{session}.jsonl"))
    if len(matches) != 1 or matches[0].resolve() != matches[0] or not matches[0].is_file():
        raise GateError("The original local Claude Code session log is missing or ambiguous")
    return matches[0]


def claude_human_prompt(record):
    """Recognize a prompt the person submitted, not tool output, summaries, reminders or subagents."""
    message = record.get("message")
    return (record.get("type") == "user" and isinstance(record.get("uuid"), str)
            and record.get("origin") == {"kind": "human"} and record.get("turnOrigin") == "human"
            and record.get("userType") == "external" and record.get("promptSource") in CLAUDE_PROMPT_SOURCES
            and record.get("isSidechain") is False and "toolUseResult" not in record
            and not any(record.get(key) for key in ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly"))
            and isinstance(message, dict) and message.get("role") == "user")


def claude_prompt_text(record):
    content = record["message"].get("content")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list) and all(isinstance(block, dict) and block.get("type") == "text"
                                           and isinstance(block.get("text"), str) for block in content):
        texts = [block["text"] for block in content]
    else:
        return None  # Images, documents or unknown blocks are never approval evidence.
    typed = [text for text in texts if not CLAUDE_IDE_CONTEXT.fullmatch(text.strip())]
    return typed[0] if len(typed) == 1 else None


def claude_final_answer(records, prompt, session):
    """Return the text of the assistant message that ended the turn right before this prompt."""
    parent = records.get(prompt.get("parentUuid"))
    while parent is not None and parent.get("type") == "system" and parent.get("subtype") in CLAUDE_TURN_MARKERS:
        parent = records.get(parent.get("parentUuid"))
    texts, message_id = [], None
    while parent is not None and parent.get("type") == "assistant":
        message = parent.get("message")
        if not isinstance(message, dict):
            return None
        message_id = message_id or message.get("id")
        if not message_id or message.get("id") != message_id:
            break  # An earlier API response belongs to the same turn but not to the final answer.
        content = message.get("content")
        if (parent.get("sessionId") != session or parent.get("isSidechain") is not False
                or parent.get("isApiErrorMessage") or message.get("role") != "assistant"
                or message.get("model") in (None, "<synthetic>") or message.get("stop_reason") != "end_turn"
                or not isinstance(content, list)):
            return None
        for block in reversed(content):
            if not isinstance(block, dict):
                return None
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.insert(0, block["text"])
            elif block.get("type") not in {"thinking", "redacted_thinking"}:
                return None
        parent = records.get(parent.get("parentUuid"))
    return "\n".join(texts) if texts else None


def read_claude_approval(issue, session, uuid=None):
    """Bind the latest person-typed prompt, or a recorded one, to the final answer it replied to."""
    log = claude_session(session)
    records, prompts = {}, []
    with log.open(encoding="utf-8") as transcript:
        for line in transcript:
            if not line.endswith("\n"):
                break  # The runtime may still be appending the last record.
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise GateError("The Claude Code session log has an invalid record")
            key = record.get("uuid")
            if key is not None:
                if key in records:
                    raise GateError("The Claude Code session log has duplicate record IDs")
                records[key] = record
            if claude_human_prompt(record):
                prompts.append(record)
    if uuid is None:
        selected = prompts[-1] if prompts else None
    else:
        selected = next((record for record in prompts if record["uuid"] == uuid), None)
    if selected is None or not isinstance(selected.get("timestamp"), str) or not selected["timestamp"]:
        raise GateError("The original Claude Code user approval prompt is missing")
    if selected.get("sessionId") != session:
        raise GateError("The Claude Code approval belongs to another session")
    prompt = claude_prompt_text(selected)
    if prompt is None or prompt.strip() != approval_message(issue):
        raise GateError("The Claude Code user message is not explicit approval for this Issue")
    proposal = claude_final_answer(records, selected, session)
    if proposal is None:
        raise GateError("The Claude Code approval must immediately follow the approved scope proposal")
    return {**conversation_scope(proposal, issue, "Claude Code"), "session": session, "log": str(log),
            "uuid": selected["uuid"], "timestamp": selected["timestamp"], "message": prompt}


def saved_claude_approval(state):
    saved = state.get("claude_approval")
    if (not isinstance(saved, dict) or "approval_comment" in state or "codex_approval" in state
            or not saved.get("uuid") or not saved.get("session")):
        raise GateError("An unambiguous local Claude Code approval record is required")
    if os.environ.get(CLAUDE_SESSION) != saved["session"]:
        raise GateError("Continue this approval in its original Claude Code session")
    current = read_claude_approval(state["issue"], saved["session"], saved["uuid"])
    if current != saved:
        raise GateError("The saved Claude Code approval no longer matches the original session log")
    return current


def check_issue(issue, approval, approval_time, merged=False):
    if ("pull_request" in issue or (issue["state"] != "open" and not (merged and issue["state"] == "closed"))
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
    pending = {**initial_state(args, None, approval),
               **{key: approval[key] for key in ("branch", "mode") if key in approval}}
    del pending["issue"]
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
    write_state(args.state, {**pending, "issue": issue["number"]})
    print(issue["html_url"])


def dev_sha():
    sha = api("git/ref/heads/dev")["object"]["sha"]
    if not OBJECT_ID.fullmatch(sha):
        raise GateError("GitHub did not return a full dev SHA")
    return sha


def fetch_dev():
    base = dev_sha()
    git("fetch", "--no-tags", "origin", "refs/heads/dev:refs/remotes/origin/dev")
    if git("rev-parse", "refs/remotes/origin/dev").decode().strip() != base or dev_sha() != base:
        raise GateError("dev moved during fetch; retry with the latest dev")
    return base


def repository_paths():
    remote = git("remote", "get-url", "origin").decode().strip().removesuffix(".git")
    if remote not in {f"https://github.com/{REPO}", f"git@github.com:{REPO}", f"ssh://git@github.com/{REPO}"}:
        raise GateError("origin must point to the approved repository")
    checkout = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    entries = git("worktree", "list", "--porcelain", "-z").decode().split("\0")
    worktrees = [Path(entry.removeprefix("worktree ")).resolve()
                 for entry in entries if entry.startswith("worktree ")]
    if not worktrees or checkout not in worktrees or Path.cwd().resolve() != checkout:
        raise GateError("Run from the root of a registered checkout in the approved repository")
    return checkout, worktrees


def worktree_path(main, branch):
    target = main.parent / f"{REPO.split('/')[1]}-worktrees" / branch.split("/")[-1]
    if target.resolve() != target:
        raise GateError("The approved worktree path must not redirect to another directory")
    return target


def checkout_branch(target, worktrees):
    if target not in worktrees:
        raise GateError("The recorded checkout is missing from this clone")
    prefix = ("-C", str(target), "rev-parse")
    top = Path(git(*prefix, "--show-toplevel").decode().strip()).resolve()
    common_args = ("--path-format=absolute", "--git-common-dir")
    common = Path(git("rev-parse", *common_args).decode().strip()).resolve()
    if top != target or Path(git(*prefix, *common_args).decode().strip()).resolve() != common:
        raise GateError("The recorded checkout belongs to another clone")
    return git("-C", str(target), "branch", "--show-current").decode().strip()


def require_clean(target):
    prefix = ("-C", str(target))
    if git(*prefix, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none").strip():
        raise GateError("The checkout has uncommitted or untracked changes; preserve them before continuing")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer",
                 "BISECT_START"):
        path = Path(git(*prefix, "rev-parse", "--git-path", name).decode().strip())
        if (target / path).exists():
            raise GateError("Finish the current Git operation before continuing")


def prepared_checkout(state, branches=None):
    if "mode" not in state:
        if "worktree" not in state or "checkout" in state:
            raise GateError("State does not belong to a prepared Issue branch")
        return None  # Historical PR and merge commands did not require a local checkout.
    if "checkout" not in state or "worktree" in state or state.get("preparing"):
        raise GateError("State does not belong to a prepared Issue branch")
    mode = work_mode(state["mode"])
    checkout, worktrees = repository_paths()
    main = worktrees[0]
    target = main if mode == "main" else worktree_path(main, state["branch"])
    if Path(state["checkout"]) != target or checkout not in {main, target}:
        raise GateError("Run from the recorded repository or approved worktree")
    allowed = branches if branches is not None else {state["branch"]}
    if checkout_branch(target, worktrees) not in allowed:
        raise GateError("The recorded checkout is not on the approved branch")
    return target


def prepare(args):
    previous = load_json(args.state) if args.state.exists() else None
    if args.codex_approval or getattr(args, "claude_approval", False):
        if args.codex_approval:
            source = "codex_approval"
            approval = (saved_codex_approval(previous) if previous is not None
                        else read_codex_approval(args.issue, os.environ.get("CODEX_THREAD_ID")))
        else:
            source = "claude_approval"
            approval = (saved_claude_approval(previous) if previous is not None
                        else read_claude_approval(args.issue, os.environ.get(CLAUDE_SESSION)))
        approved_at = approval["timestamp"]
        state = {"issue": args.issue, "issue_digest": approval["issue_digest"],
                 "paths": approval["paths"], source: approval}
    else:
        if previous is not None and any(key in previous for key in CONVERSATION_SOURCES):
            raise GateError("Resume the original conversation approval source")
        approval, approved_at = read_approval(args.approval_comment)
        state = initial_state(args, args.issue, approval)
    branch = approved_branch(approval, args.issue)
    check_issue(api(f"issues/{args.issue}"), approval, approved_at)
    if previous is None:
        previous = state
    if (any(previous.get(key) != value for key, value in state.items())
            or previous.get("branch", branch) != branch):
        raise GateError("State already belongs to another task")
    if approval["issue"] is None and not args.state.exists():
        raise GateError("Use register to bind the approved new Issue first")
    mode = approved_mode(approval, previous)
    requested_mode = getattr(args, "mode", None)
    if previous.get("mode", mode) != mode or (requested_mode is not None and requested_mode != mode):
        raise GateError("The selected or recorded work mode was not approved")
    checkout, worktrees = repository_paths()
    main = worktrees[0]
    legacy = "worktree" in previous
    if legacy and ("mode" in approval or "mode" in previous or "checkout" in previous):
        raise GateError("The legacy state cannot authorize a different work mode")
    if legacy:
        target = Path(previous["worktree"])
    else:
        target = main if mode == "main" else worktree_path(main, branch)
    supplied = getattr(args, "worktree", None)
    if supplied is not None and (mode != "worktree" or supplied.resolve() != target):
        raise GateError("The worktree path does not match the approved checkout")
    if any(args.state.resolve().is_relative_to(path) for path in (main, target)):
        raise GateError("Keep the state file outside the clone and approved checkout")
    if previous.get("checkout", str(target)) != str(target) or checkout not in {main, target}:
        raise GateError("Resume from the originally recorded repository or worktree")
    if "merge_sha" in previous:
        raise GateError("This Issue already has a verified merge")
    if legacy or ("checkout" in previous and not previous.get("preparing")):
        if checkout_branch(target, worktrees) != branch:
            raise GateError("The recorded checkout is not on the approved branch")
        print(f"Resume {branch} at {target}")
        return
    if "checkout" in previous and target in worktrees and checkout_branch(target, worktrees) == branch:
        # The checkout succeeded before the state could be finalized.
        base = previous["base"]
        if git("-C", str(target), "rev-parse", "HEAD").decode().strip() != base:
            raise GateError("Interrupted preparation has an unexpected branch HEAD")
    else:
        if checkout != main or (mode == "worktree" and target.exists()):
            raise GateError("Start from the main checkout and use a new approved worktree path")
        if mode == "main":
            require_clean(main)
        base = fetch_dev()
        check_branch_available(branch)
        write_state(args.state, {**previous, "base": base, "branch": branch, "mode": mode,
                                 "checkout": str(target), "preparing": True})
        if mode == "main":
            git("switch", "--no-overwrite-ignore", "-c", branch, base)
        else:
            git("worktree", "add", "-b", branch, str(target), base)
    write_state(args.state, {**{k: v for k, v in previous.items() if k != "preparing"},
                             "base": base, "branch": branch, "mode": mode, "checkout": str(target)})
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
            or not state.get("branch") or pr["head"]["ref"] != state["branch"] or issue != state["issue"]):
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


def check_live_approval(state, merged=False):
    if sum(key in state for key in ("approval_comment", *CONVERSATION_SOURCES)) != 1:
        raise GateError("Exactly one recorded approval source is required")
    if "codex_approval" in state:
        approval = saved_codex_approval(state)
        approved_at = approval["timestamp"]
    elif "claude_approval" in state:
        approval = saved_claude_approval(state)
        approved_at = approval["timestamp"]
    else:
        approval, approved_at = read_approval(state["approval_comment"])
    if (approval["issue_digest"] != state["issue_digest"]
            or paths(approval["paths"]) != set(state["paths"])):
        raise GateError("Approval changed since the checkout was prepared")
    if approved_branch(approval, state["issue"]) != state.get("branch"):
        raise GateError("The recorded branch is not the approved branch")
    mode = approved_mode(approval, state)
    if state.get("mode", "worktree" if "worktree" in state else "main") != mode or (
            "mode" in approval and "mode" not in state):
        raise GateError("The recorded work mode is not the approved work mode")
    check_issue(api(f"issues/{state['issue']}"), approval, approved_at, merged=merged)


def open_pr(args):
    require_enabled()
    state = load_json(args.state)
    branch = state.get("branch")
    if not isinstance(branch, str) or not branch or not {"worktree", "checkout"}.intersection(state):
        raise GateError("State does not belong to a prepared Issue branch")
    check_live_approval(state)  # also checks that this branch is the approved one
    target = prepared_checkout(state)
    candidates = rows(f"pulls?state=all&head={REPO.split('/')[0]}:{branch}&per_page=100")
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
        if target is not None:
            require_clean_head(target, head)
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


def require_clean_head(target, head):
    require_clean(target)
    if git("-C", str(target), "rev-parse", "HEAD").decode().strip() != head:
        raise GateError("Local HEAD differs from the pushed PR Head")


def sync_main_dev(state_path, state, receipt, head):
    target = prepared_checkout(state, {state["branch"], "dev"})
    if git("branch", "--show-current").decode().strip() == state["branch"]:
        require_clean_head(target, head)
    else:
        require_clean(target)
    latest = fetch_dev()
    git("merge-base", "--is-ancestor", receipt["merge_sha"], latest)
    if git("branch", "--list", "dev").strip():
        git("merge-base", "--is-ancestor", "refs/heads/dev", latest)
        git("switch", "--no-overwrite-ignore", "dev")
        git("merge", "--ff-only", "--no-overwrite-ignore", latest)
    else:
        git("switch", "--no-overwrite-ignore", "-c", "dev", "--track", "origin/dev")
    if (git("branch", "--show-current").decode().strip() != "dev"
            or git("rev-parse", "HEAD").decode().strip() != latest):
        raise GateError("Local dev did not reach the verified dev commit; re-run the merge receipt")
    write_state(state_path, {**state, "synced_dev": latest})


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
        print(json.dumps(result))
        if args.execute and "mode" in state:
            check_live_approval(state, merged=True)
            complete = {**state, "pr": args.pr, "verified_head": head, **result}
            if "merge_sha" not in state:
                write_state(args.state, complete)
            if state["mode"] == "main" and "synced_dev" not in state:
                sync_main_dev(args.state, complete, result, head)
        return
    check_live_approval(state)
    target = prepared_checkout(state)
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
    if target is not None:
        require_clean_head(target, head)
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
    complete = {**load_json(args.state), **receipt}
    write_state(args.state, complete)
    print(json.dumps(receipt))
    if state.get("mode") == "main":
        sync_main_dev(args.state, complete, receipt, head)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    digest = commands.add_parser("digest")
    digest.add_argument("draft", type=Path)
    create = commands.add_parser("register")
    create.add_argument("draft", type=Path)
    setup = commands.add_parser("prepare")
    setup.add_argument("issue", type=positive)
    setup.add_argument("worktree", type=Path, nargs="?")
    setup.add_argument("--mode", choices=MODES)
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
    source.add_argument("--claude-approval", action="store_true")
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
