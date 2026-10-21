#!/usr/bin/env python3
"""Bind a Claude review to an open PR and validate native review receipts.

Sources: GitHub REST pull/run/status APIs; pinned claude-code-action SDK output.
The workflow runs this trusted engine, never a script from the candidate PR.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Source: native code-review command steps 1-4 (three preparation + four review agents).
NATIVE_CODE_REVIEW_AGENTS = 7


def api(path, data=None):
    args = ["gh", "api", path]
    if data is not None:
        args.extend(["--method", "POST", "--input", "-"])
    result = subprocess.run(
        args,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout else None


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            value = str(value)
            if "\n" in value or "\r" in value:
                raise ValueError(f"Invalid output: {key}")
            stream.write(f"{key}={value}\n")


def eligible(pr, repository, head):
    labels = {x["name"].lower() for x in pr.get("labels", [])}
    return (
        pr["state"] == "open"
        and not pr["draft"]
        and pr["head"]["repo"]["full_name"] == repository
        and pr["head"]["sha"] == head
        and not labels.intersection({"hold", "do-not-merge", "do not merge", "blocked"})
    )


def select():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    run = event.get("workflow_run")
    if not run or run["event"] != "pull_request" or run["status"] != "completed":
        output(eligible="false")
        return
    candidates = api(f"repos/{repository}/commits/{run['head_sha']}/pulls")
    for candidate in candidates:
        pr = api(f"repos/{repository}/pulls/{candidate['number']}")
        if not eligible(pr, repository, run["head_sha"]):
            continue
        author = pr["user"]["login"]
        if author not in {"dependabot[bot]", "claude[bot]"}:
            permission = api(f"repos/{repository}/collaborators/{author}/permission")
            if permission["permission"] not in {"admin", "maintain", "write"}:
                continue
        output(
            eligible="true",
            number=pr["number"],
            head=pr["head"]["sha"],
            branch=pr["head"]["ref"],
            base=pr["base"]["ref"],
            ci_run=run["id"],
            ci_conclusion=run["conclusion"],
        )
        return
    output(eligible="false")


def status(state, description):
    repository = os.environ["GITHUB_REPOSITORY"]
    api(
        f"repos/{repository}/statuses/{os.environ['REVIEW_HEAD']}",
        {
            "state": state,
            "context": "Claude bot",
            "description": description,
            "target_url": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        },
    )


def native_receipts(messages):
    """Require real Skill tool calls with successful tool results, not prose."""
    calls, successes = {}, set()
    for message in messages:
        for block in message.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Skill":
                skill = block.get("input", {}).get("skill", "").split(":")[-1]
                if skill in {"code-review", "security-review"}:
                    calls[block["id"]] = skill
            if block.get("type") == "tool_result" and not block.get("is_error", False):
                successes.add(block.get("tool_use_id"))
    return {skill for identifier, skill in calls.items() if identifier in successes}


def review_work(messages):
    """Reject a loaded skill that stopped before doing its native review work.

    Source: anthropics/claude-code plugins/code-review/commands/code-review.md,
    steps 1-4: eligibility, guidance, summary, then four independent reviewers.
    Security-review's native diff command is evidenced by the reported #769 failure.
    """
    phase, agents, diffs, successes = "", set(), set(), set()
    for message in messages:
        for block in message.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result" and not block.get("is_error", False):
                successes.add(block.get("tool_use_id"))
            if block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input", {})
            if name == "Skill":
                phase = args.get("skill", "").split(":")[-1]
            elif phase == "code-review" and name in {"Agent", "Task"}:
                agents.add(block["id"])
            elif (
                phase == "security-review"
                and name == "Bash"
                and "git diff" in args.get("command", "")
            ):
                diffs.add(block["id"])
    # Source: three preparation agents + four reviewers in native command steps 1-4.
    if len(agents & successes) < NATIVE_CODE_REVIEW_AGENTS:
        raise ValueError(
            "Native code review stopped before its review agents completed"
        )
    if not diffs & successes:
        raise ValueError("Native security review did not execute its diff")


def verify(messages, report, head):
    if report.get("head") != head or report.get("outcome") != "ready":
        raise ValueError("Claude did not declare this head ready")
    if native_receipts(messages) != {"code-review", "security-review"}:
        raise ValueError("Missing successful native review invocation")
    review_work(messages)
    for review in ("code_review", "security_review"):
        if report.get(review) != "passed":
            raise ValueError(f"Native review not passed: {review}")
    results = [m for m in messages if m.get("type") == "result"]
    if (
        not results
        or results[-1].get("is_error")
        or results[-1].get("permission_denials")
    ):
        raise ValueError("Claude failed or a required operation was denied")


def finish():
    repository = os.environ["GITHUB_REPOSITORY"]
    head = os.environ["REVIEW_HEAD"]
    pr = api(f"repos/{repository}/pulls/{os.environ['PR_NUMBER']}")
    if pr["head"]["sha"] != head:
        status("error", "Head changed; the next CI run will review it")
        return
    try:
        if not eligible(pr, repository, head):
            raise ValueError("PR is held, closed, draft, or no longer eligible")
        path = os.environ.get("EXECUTION_FILE", "")
        if not path:
            raise ValueError("Claude produced no execution receipt")
        messages = json.loads(Path(path).read_text())
        report = json.loads(os.environ.get("STRUCTURED_OUTPUT") or "{}")
        verify(messages, report, head)
        run = api(f"repos/{repository}/actions/runs/{os.environ['CI_RUN']}")
        if run["head_sha"] != head or run["conclusion"] != "success":
            raise ValueError("Tests have not passed on this reviewed head")
    except (ValueError, OSError) as error:
        status("failure", str(error)[:140])
        raise
    status("success", "Native reviews passed on the current tested head")


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "select":
        select()
    elif command == "pending":
        status("pending", "Native reviews and corrections in progress")
    elif command == "finish":
        finish()
    else:
        raise SystemExit(f"Unknown command: {command}")
