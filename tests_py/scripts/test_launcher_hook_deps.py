"""No hook launch installs the ML set in-band (issue #747).

Before ADR-1104, ``scripts/launcher.py`` ran ``ensure_all_deps`` for
``mcp_server.hooks.session_start``: a synchronous ``uv pip install`` of the ML
set (torch, sentence-transformers, about 1 GB) inside the hook process. When
the deps directory's ML stamp did not name the current uv.lock, the
SessionStart launch blocked on that install, and
``tests_py/hooks/test_entry.py`` hung in ``communicate()`` on the launcher
subprocess until pytest's 300 s timeout aborted the suite. ADR-1104 moved the
ML set to one detached job (``launcher_ml.start``); this test pins that every
hook ``.claude-plugin/plugin.json`` wires installs the base set and nothing
else before it runs, so the in-band install cannot come back unnoticed.

source: ADR-1104
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
# source: plugin.json hook command shape, "<root>/scripts/launcher.py" <module>
_LAUNCHED_HOOK = re.compile(r'launcher\.py"\s+(mcp_server\.hooks\.\w+)')

# Runs the real launcher.main() with the install seams and the dispatch
# replaced by recorders, then prints what the launch asked for.
_CHILD = r"""
import json, runpy, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import launcher
calls = {"sets": [], "jobs": []}
launcher.launcher_deps.ensure_deps = lambda d: calls["sets"].append("base")
launcher.launcher_deps.ensure_all_deps = lambda d: calls["sets"].append("base+ml")
def start(deps_dir, target):
    calls["jobs"].append(deps_dir)
    return "installing", ""
launcher.launcher_ml.start = start
runpy.run_module = lambda module, **kwargs: None
sys.argv = ["launcher.py", sys.argv[1]]
launcher.main()
print(json.dumps(calls))
"""


def _hook_commands() -> list[str]:
    manifest = json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entries in manifest["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]


def _wired_hook_modules() -> list[str]:
    found = (_LAUNCHED_HOOK.search(command) for command in _hook_commands())
    return sorted({match.group(1) for match in found if match})


def _launch(module: str, tmp_path: Path) -> dict:
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


def test_every_manifest_hook_command_names_the_module_it_launches() -> None:
    unparsed = [c for c in _hook_commands() if not _LAUNCHED_HOOK.search(c)]
    assert unparsed == []
    assert "mcp_server.hooks.session_start" in _wired_hook_modules()


@pytest.mark.parametrize("module", _wired_hook_modules())
def test_a_hook_launch_installs_only_the_base_set(module: str, tmp_path) -> None:
    assert _launch(module, tmp_path)["sets"] == ["base"]


def test_session_start_hands_the_ml_set_to_the_background_job(tmp_path) -> None:
    calls = _launch("mcp_server.hooks.session_start", tmp_path)
    assert calls["sets"] == ["base"]
    assert calls["jobs"] == [str(tmp_path / "plugin-data" / "deps")]
