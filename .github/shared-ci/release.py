#!/usr/bin/env python3
"""Tag the tested main commit, then dispatch existing artifact publishers.

Sources: GitHub workflow trigger docs (GITHUB_TOKEN does not trigger tag pushes,
but workflow_dispatch is supported); each adapter's existing version source.
"""

import json
import os
import re
import subprocess
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def publish(repository, tag, sha, workflows):
    """Resume a tag whose dispatch was interrupted without duplicating runs."""
    for workflow in workflows:
        runs = json.loads(
            run(
                "gh",
                "run",
                "list",
                "--repo",
                repository,
                "--workflow",
                workflow,
                "--commit",
                sha,
                "--event",
                "workflow_dispatch",
                "--json",
                "databaseId,headBranch,status,conclusion",
            )
        )
        matching = [item for item in runs if item["headBranch"] == tag]
        if matching:
            latest = matching[0]
            if latest["status"] != "completed" or latest["conclusion"] == "success":
                continue
            run(
                "gh",
                "run",
                "rerun",
                str(latest["databaseId"]),
                "--repo",
                repository,
                "--failed",
            )
        else:
            run("gh", "workflow", "run", workflow, "--repo", repository, "--ref", tag)


def release():
    if os.environ["GITHUB_REF"] != "refs/heads/main":
        raise ValueError("Release tags require the tested main branch")
    version = dict(
        line.split("=", 1)
        for line in run("bash", ".github/ci/run.sh", "release-version").splitlines()
    )
    tag = version["tag"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", tag):
        raise ValueError(f"Invalid release tag: {tag}")
    sha = os.environ["GITHUB_SHA"]
    if run("git", "rev-parse", "HEAD") != sha:
        raise ValueError("Checkout differs from tested commit")
    repository = os.environ["GITHUB_REPOSITORY"]
    exists = (
        subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        if run("git", "rev-parse", f"refs/tags/{tag}^{{commit}}") != sha:
            print(f"{tag} belongs to an earlier commit; no version bump to release")
            return
    else:
        run(
            "gh",
            "api",
            f"repos/{repository}/git/refs",
            "--method",
            "POST",
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={sha}",
        )
    config = json.loads(Path(".github/ci/config.json").read_text())
    publish(repository, tag, sha, config.get("release_workflows", []))
    print(f"Tag {tag} at {sha}; publisher workflows started or already present")


if __name__ == "__main__":
    release()
