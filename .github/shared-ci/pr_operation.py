#!/usr/bin/env python3
"""Bound PR operations for Claude; no force pushes or protection bypasses.

Sources: git-push documentation and cli.github.com/manual/gh_pr_merge.
"""

import json
import os
import subprocess
import sys


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def operate(operation):
    number = os.environ["PR_NUMBER"]
    repository = os.environ["GITHUB_REPOSITORY"]
    pr = json.loads(run("gh", "api", f"repos/{repository}/pulls/{number}"))
    metadata = json.loads(run("gh", "api", f"repos/{repository}"))
    head = os.environ["REVIEW_HEAD"]
    if (
        pr["state"] != "open"
        or pr["draft"]
        or pr["head"]["sha"] != head
        or pr["head"]["repo"]["full_name"] != repository
    ):
        raise ValueError("PR identity or head changed")
    if pr["head"]["ref"] in {pr["base"]["ref"], metadata["default_branch"], "main"}:
        raise ValueError("Refusing an operation on a base or default branch")
    if operation == "push":
        run("git", "merge-base", "--is-ancestor", head, "HEAD")
        run("git", "push", "origin", f"HEAD:refs/heads/{pr['head']['ref']}")
    elif operation == "auto-merge":
        if os.environ.get("ENABLE_AUTOMERGE") != "true":
            raise ValueError("Auto-merge awaits installation of required review status")
        if run("git", "rev-parse", "HEAD") != head:
            raise ValueError("Local fixes require a new CI/review run")
        run(
            "gh",
            "pr",
            "merge",
            number,
            "--repo",
            repository,
            "--auto",
            "--squash",
            "--match-head-commit",
            head,
        )
    else:
        raise ValueError(f"Unknown operation: {operation}")


if __name__ == "__main__":
    operate(sys.argv[1])
