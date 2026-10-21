#!/usr/bin/env python3
"""Expose declared runtimes as action outputs without evaluating config as code.

Source: docs.github.com/actions/reference/workflow-commands-for-github-actions.
"""

import json
import os
from pathlib import Path


def outputs(config):
    result = {}
    for name in (
        "python_version",
        "node_version",
        "rust_toolchain",
        "uv_version",
        "pnpm_version",
        "claude_version",
    ):
        value = config.get(name) or ""
        if not isinstance(value, str) or any(c in value for c in "\r\n"):
            raise ValueError(f"Invalid {name}")
        result[name] = value
    result["postgres"] = str(config.get("postgres", False)).lower()
    result["environment_runners"] = json.dumps(config.get("environment_runners", []))
    result["auxiliary_python_versions"] = json.dumps(
        config.get("auxiliary_python_versions", [])
    )
    if len(config.get("auxiliary_python_versions", [])) > 1:
        raise ValueError(
            "The shared setup supports one additional compatibility Python"
        )
    result["auxiliary_python_version"] = next(
        iter(config.get("auxiliary_python_versions", [])), ""
    )
    return result


if __name__ == "__main__":
    config = json.loads(Path(".github/ci/config.json").read_text())
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in outputs(config).items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"Multiline output: {key}")
            stream.write(f"{key}={value}\n")
