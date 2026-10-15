"""A hook never installs dependencies: it reports and starts a detached job.

Every hook has a host time budget (5-30 s) that a dependency install (uv
bootstrap 29 s, base set 28-58 s, cold 109 s measured 2026-10-14) does not
fit; a hook the host cancels mid-install leaves an empty ``deps.tmp-<pid>``
scratch directory behind (64 of them in the owner's plugin data). So a hook
launch with the base set missing says so (exit 1; exit 0 with a user-visible
notice for the gates and SessionStart), starts the one
detached install job, and runs nothing. The assertions are on the install
seams (``ensure_deps``, ``ensure_all_deps``, ``install_locked_set``, any
``subprocess.run``), not on elapsed time.

source: docs/adr/ADR-1108-no-dependency-install-inside-a-hook.md
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CHILD = r"""
import json, os, runpy, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
import launcher
seen = {'install': [], 'run': [], 'popen': [], 'dispatched': []}
deps = launcher.launcher_deps
deps.ensure_deps = lambda path: seen['install'].append('ensure_deps')
deps.ensure_all_deps = lambda path: seen['install'].append('ensure_all_deps')
deps._install_locked_set = lambda *a, **k: seen['install'].append('install_locked_set')
if os.environ.get('SET_READY') == '1':
    deps.set_ready = lambda path, kind: True
def no_run(argv, *a, **k):
    seen['run'].append(list(argv))
    raise AssertionError('subprocess.run in a hook launch: ' + repr(argv))
class FakePopen:
    def __init__(self, argv, **kwargs):
        seen['popen'].append({'argv': list(argv), 'detached': bool(
            kwargs.get('start_new_session') or kwargs.get('creationflags'))})
subprocess.run = no_run
subprocess.Popen = FakePopen
runpy.run_module = lambda module, **kw: seen['dispatched'].append(module)
sys.argv = ['launcher.py', os.environ['TARGET_MODULE']]
if os.environ.get('INSTALL_DEPS') == '1':
    sys.argv.append('--install-deps')
code = 0
try:
    launcher.main()
except SystemExit as error:
    code = error.code
print(json.dumps({'code': code, **seen}))
"""

# source: ADR-1104 (the host's SessionStart timeout, measured)
_HOST_HOOK_BUDGET_SECONDS = 30
# source: measured 2026-10-15, Claude Code 2.1.300: a Setup hook with no timeout was
# killed 599 s after it started (fleet/deps-out-of-hook/lab/dflt)
_HOST_DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
_SESSION = "mcp_server.hooks.session_start"
_HOOKS = (
    "mcp_server.hooks.session_start",
    "mcp_server.hooks.auto_recall",
    "mcp_server.hooks.post_tool_capture",
)


class HooksNeverInstall(unittest.TestCase):
    def setUp(self):
        tree = tempfile.TemporaryDirectory(prefix="cortex-launcher-hook-")
        self.addCleanup(tree.cleanup)
        self.root = Path(tree.name)
        sys.path.insert(0, str(_ROOT / "scripts"))
        try:
            import launcher_layout
        finally:
            sys.path.remove(str(_ROOT / "scripts"))
        self.deps_dir = launcher_layout.project_deps_dir(
            self.root / "plugin-data", _ROOT
        )

    def launch(self, module, **options):
        env = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "WINDIR")
            if key in os.environ
        }
        env.update(
            PYTHONIOENCODING="utf-8",
            CORTEX_CLAUDE_DIR=str(self.root),
            CLAUDE_PLUGIN_DATA=str(self.root / "plugin-data"),
            CLAUDE_PLUGIN_ROOT=str(_ROOT),
            CORTEX_MEMORY_STORE_BACKEND="sqlite",
            TARGET_MODULE=module,
        )
        env.update(options)
        done = subprocess.run(
            [sys.executable, "-S", "-c", _CHILD],
            cwd=_ROOT,
            input='{"tool_name":"Read"}',
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        *hook_out, last = done.stdout.strip().splitlines()
        seen = json.loads(last)
        seen["stdout"] = "\n".join(hook_out)
        return seen, done.stderr

    def test_missing_deps_fail_loudly_install_nothing_and_start_the_job(self):
        for module in _HOOKS:
            with self.subTest(module=module):
                seen, stderr = self.launch(module)
                self.assertEqual(seen["code"], 0 if module == _SESSION else 1)
                if module == _SESSION:
                    self.assertIn("installing its dependencies", seen["stdout"])
                self.assertEqual(seen["install"], [])
                self.assertEqual(seen["run"], [])
                self.assertEqual(seen["dispatched"], [])
                self.assertIn("Cortex not ready", stderr)
                self.assertIn("deps-install.log", stderr)
                (job,) = seen["popen"]
                self.assertEqual(
                    job["argv"][1:],
                    [str(_ROOT / "scripts" / "launcher.py"), "--install-base-job"],
                )
                self.assertTrue(job["detached"])

    def test_a_second_hook_does_not_start_a_second_job(self):
        data = self.root / "plugin-data"
        data.mkdir()
        sys.path.insert(0, str(_ROOT / "scripts"))
        try:
            import launcher_base_job
            import launcher_lock
        finally:
            sys.path.remove(str(_ROOT / "scripts"))
        with launcher_lock.held_if_free(
            launcher_base_job.job_lock(self.deps_dir)
        ) as mine:
            self.assertTrue(mine)
            seen, stderr = self.launch(_HOOKS[0])
        self.assertEqual((seen["code"], seen["popen"]), (0, []))
        self.assertIn("already running", stderr)

    def test_ready_deps_run_the_hook_without_an_install_or_a_job(self):
        for module in _HOOKS:
            with self.subTest(module=module):
                seen, _ = self.launch(module, SET_READY="1")
                self.assertEqual(seen["code"], 0)
                self.assertEqual(seen["install"], [])
                started = [job["argv"][-1] for job in seen["popen"]]
                self.assertNotIn("--install-base-job", started)
                self.assertEqual(seen["dispatched"], [module])

    def test_the_mcp_server_and_the_explicit_flag_still_install(self):
        seen, _ = self.launch("mcp_server", CORTEX_MCP_DIRECT="1")
        self.assertEqual(seen["install"], ["ensure_deps"])
        seen, _ = self.launch(_HOOKS[1], INSTALL_DEPS="1")
        self.assertEqual(seen["install"], ["ensure_all_deps"])

    def test_only_the_setup_hook_asks_the_host_for_more_than_30_seconds(self):
        """The SessionStart timeout went 30 -> 300 s so that the hook could
        install (ADR-1104); the install left the hook, so the budget did too.
        The Setup hook is the one place that installs and waits (ADR-1108):
        its timeout is explicit because the host's default kills it at 600 s
        (measured: Claude Code 2.1.300), shorter than a slow network's install."""
        manifest = json.loads((_ROOT / ".claude-plugin" / "plugin.json").read_text())
        timeouts = {
            command["command"].rsplit("launcher.py", 1)[1]: command.get("timeout")
            for groups in manifest["hooks"].values()
            for group in groups
            for command in group["hooks"]
        }
        setup = [t for module, t in timeouts.items() if module.endswith("--setup'")]
        self.assertEqual(len(setup), 1)
        self.assertGreater(setup[0], _HOST_DEFAULT_COMMAND_TIMEOUT_SECONDS)
        for module, timeout in timeouts.items():
            if module.endswith("--setup'"):
                continue
            with self.subTest(module=module):
                self.assertLessEqual(timeout or 0, _HOST_HOOK_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
