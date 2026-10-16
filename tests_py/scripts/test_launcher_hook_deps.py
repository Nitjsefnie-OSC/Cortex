"""No hook launch installs a dependency set in-band (issue #747, ADR-1108).

Before ADR-1104, ``scripts/launcher.py`` ran ``ensure_all_deps`` for
``mcp_server.hooks.session_start``: a synchronous ``uv pip install`` of the ML
set (torch, sentence-transformers, about 1 GB) inside the hook process, and
``tests_py/hooks/test_entry.py`` hung in ``communicate()`` until pytest's
300 s timeout aborted the suite. ADR-1104 moved the ML set to one detached job
and left the base set in the hook; ADR-1108 removed the base set from every
hook too: only the Setup hook (``launcher.py --setup``), the MCP server and the
detached jobs install. This test runs the real ``launcher.main()`` for EVERY
hook ``.claude-plugin/plugin.json`` wires, with the install seams, the
subprocess calls and the dispatch replaced by recorders (no real job starts),
and requires that no launch installs anything: with the base set missing it
starts the one detached base job and exits as ``launcher_base_job.refuse``
decides; with it installed it dispatches the hook, and SessionStart hands the
ML set to the background job.

source: ADR-1108
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]  # source: tests_py/scripts/<file>
# source: .claude-plugin/plugin.json -- the hook commands the host runs
_PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
# source: plugin.json hook command shape, "<root>/scripts/launcher.py" <argument>
_LAUNCHED = re.compile(r'launcher\.py"\s+(mcp_server\.hooks\.\w+|--setup)\'')
_SESSION_START = "mcp_server.hooks.session_start"  # source: plugin.json SessionStart
# source: scripts/launcher_base_job.py JOB
_BASE_JOB = "--install-base-job"

# Runs the real launcher.main() with every install seam, subprocess call and
# the dispatch replaced by recorders, then prints what the launch asked for.
_CHILD = r"""
import json, os, runpy, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import launcher
seen = {"sets": [], "ml_jobs": [], "popen": [], "run": [], "dispatched": []}
deps = launcher.launcher_deps
deps.ensure_deps = lambda d: seen["sets"].append("base")
deps.ensure_all_deps = lambda d: seen["sets"].append("base+ml")
deps._install_locked_set = lambda *a, **k: seen["sets"].append("locked_set")
if os.environ.get("SET_READY") == "1":
    deps.set_ready = lambda d, kind: True
def ml_start(deps_dir, target):
    seen["ml_jobs"].append(deps_dir)
    return "installing", ""
launcher.launcher_ml.start = ml_start
def no_run(argv, *a, **k):
    seen["run"].append(list(argv))
    raise AssertionError("subprocess.run in a hook launch: " + repr(argv))
class RecordedPopen:
    def __init__(self, argv, **kwargs):
        seen["popen"].append(list(argv))
subprocess.run = no_run
subprocess.Popen = RecordedPopen
runpy.run_module = lambda module, **kw: seen["dispatched"].append(module)
sys.argv = ["launcher.py", sys.argv[1]]
code = 0
try:
    launcher.main()
except SystemExit as error:
    code = error.code
print(json.dumps({"code": code, **seen}))
"""


def _hook_commands() -> list[str]:
    manifest = json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entries in manifest["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]


def _launched() -> list[str]:
    found = (_LAUNCHED.search(command) for command in _hook_commands())
    return [match.group(1) for match in found if match]


def _wired_hook_modules() -> list[str]:
    return sorted({arg for arg in _launched() if arg != "--setup"})


def _gates() -> frozenset[str]:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import launcher_base_job
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
    return launcher_base_job.GATES


def _launch(module: str, tmp_path: Path, **options: str) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        CORTEX_CLAUDE_DIR=str(tmp_path / "claude-dir"),
        CLAUDE_PLUGIN_DATA=str(tmp_path / "plugin-data"),
        CLAUDE_PLUGIN_ROOT=str(REPO_ROOT),
        CORTEX_MEMORY_STORE_BACKEND="sqlite",
    )
    env.update(options)
    env.pop("DATABASE_URL", None)
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, module],
        cwd=REPO_ROOT,
        input="{}",
        capture_output=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_every_manifest_hook_command_is_a_hook_module_or_the_setup_hook() -> None:
    assert len(_launched()) == len(_hook_commands())
    assert _launched().count("--setup") == 1
    assert _SESSION_START in _wired_hook_modules()


@pytest.mark.parametrize("module", _wired_hook_modules())
def test_a_hook_launch_with_the_set_missing_installs_nothing(
    module: str, tmp_path
) -> None:
    seen = _launch(module, tmp_path)
    assert (seen["sets"], seen["run"], seen["dispatched"]) == ([], [], [])
    assert [argv[-1] for argv in seen["popen"]] == [_BASE_JOB]
    expected = 0 if module in _gates() or module == _SESSION_START else 1
    assert seen["code"] == expected


@pytest.mark.parametrize("module", _wired_hook_modules())
def test_a_hook_launch_with_the_set_installed_installs_nothing(
    module: str, tmp_path
) -> None:
    seen = _launch(module, tmp_path, SET_READY="1")
    assert (seen["code"], seen["sets"], seen["run"]) == (0, [], [])
    assert seen["dispatched"] == [module]
    assert _BASE_JOB not in [argv[-1] for argv in seen["popen"]]


def test_session_start_hands_the_ml_set_to_the_background_job(tmp_path) -> None:
    seen = _launch(_SESSION_START, tmp_path, SET_READY="1")
    assert seen["sets"] == []
    assert len(seen["ml_jobs"]) == 1
    assert Path(seen["ml_jobs"][0]).parent == tmp_path / "plugin-data" / "sets"
