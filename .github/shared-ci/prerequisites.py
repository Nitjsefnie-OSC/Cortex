#!/usr/bin/env python3
"""Install the prerequisites declared by each repository's existing CI.

Sources: per-repository .github/ci/README.md inventories and pgvector/pgvector.
"""

import json
import os
import re
import subprocess
from pathlib import Path


def run(*args):
    subprocess.run(args, check=True)


def setup(config):
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
        for key, value in config.get("required_environment", {}).items():
            if not (key in {"CI", "DATABASE_URL"} or key.startswith("CORTEX_")):
                raise ValueError(f"Unsupported repository environment variable: {key}")
            if any(char in str(value) for char in "\r\n"):
                raise ValueError(f"Multiline environment value: {key}")
            stream.write(f"{key}={value}\n")
    if os.environ.get("RUNNER_OS") == "Linux":
        packages = config.get("linux_packages", [])
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", p) for p in packages):
            raise ValueError("Invalid apt package")
        if packages:
            run("sudo", "apt-get", "update", "-qq")
            run(
                "sudo", "apt-get", "install", "-y", "--no-install-recommends", *packages
            )
        if config.get("postgres") and os.environ.get("CI_ENVIRONMENT_ONLY") != "true":
            run(
                "docker",
                "run",
                "--detach",
                "--name",
                "shared-ci-postgres",
                "--publish",
                "5432:5432",
                "--env",
                "POSTGRES_PASSWORD=postgres",
                "--env",
                "POSTGRES_DB=cortex_test",
                "pgvector/pgvector:pg17",
            )
            run(
                "bash",
                "-c",
                "until docker exec shared-ci-postgres pg_isready -U postgres; "
                "do sleep 1; done",
            )
    if config.get("pnpm_version"):
        run("npm", "install", "--global", "pnpm@" + config["pnpm_version"])
    if config.get("claude_version"):
        run(
            "npm",
            "install",
            "--global",
            "@anthropic-ai/claude-code@" + config["claude_version"],
        )
    if config.get("rust_toolchain"):
        run(
            "rustup",
            "toolchain",
            "install",
            config["rust_toolchain"],
            "--profile",
            "minimal",
            "--component",
            "llvm-tools-preview",
        )
        run("rustup", "default", config["rust_toolchain"])


if __name__ == "__main__":
    setup(json.loads(Path(".github/ci/config.json").read_text()))
