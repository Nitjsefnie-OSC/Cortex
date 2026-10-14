"""A hook never installs dependencies: it starts this job and says so — stdlib only.

Every hook runs inside a host time budget (5-30 s). Installing the base set
takes 28-109 s and bootstrapping uv or a CPython takes 29 s more, so a hook
that installs is cancelled by the host mid-install, leaving an empty
scratch directory behind (64 of them in one plugin data directory,
2026-10-14). A hook launch whose base set is not installed therefore exits
non-zero with the cause and starts at most ONE detached job that installs it,
unless the last such job FAILED for this uv.lock set: then it says so and
starts none, so a persistent failure is not retried by every hook call. The
MCP server (its host waits on the stand-in, ADR-1104), the Setup run and an
explicit ``--install-deps`` are the other places an install runs. The job
holds a lock for its life, so ten hooks starting at once start one job.

source: ADR-1108
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import launcher_deps
import launcher_layout
import launcher_logs
import launcher_lock
import launcher_ml
import launcher_setup

# The argument the detached job is started with (launcher.py <JOB>).
JOB = "--install-base-job"  # source: ADR-1108

# The hooks whose exit code 2 blocks the tool call they guard (Claude Code: exit
# 1 is non-blocking, exit 2 blocks). Without the base set these must fail closed.
BLOCKING = frozenset(
    {"mcp_server.hooks.decision_gate", "mcp_server.hooks.no_deps_gate"}
)  # source: .claude-plugin/plugin.json PreToolUse
# source: Claude Code 2.1.300 PreToolUse hook help: "Exit code 2 - show stderr
# to model and block tool call; other exit codes - ... continue with tool call"
_BLOCK_EXIT = 2


def job_lock(deps_dir: str) -> str:
    """The lock the running job holds: beside ``deps_dir``, in plugin data."""
    return f"{deps_dir}.base-job.lock"


def job_log(deps_dir: str) -> Path:
    """Where the job's output goes: the first place to look when it fails."""
    return launcher_layout.data_dir(deps_dir) / "deps-install.log"


def _failure_stamp(deps_dir: str) -> Path:
    return Path(f"{deps_dir}.base-job.failed")


def last_failure(deps_dir: str) -> str:
    """Why the last job for this set failed, "" when it did not (or was retried)."""
    try:
        return json.loads(_failure_stamp(deps_dir).read_text(encoding="utf-8"))["error"]
    except (OSError, ValueError, KeyError):
        return ""


def _running(deps_dir: str) -> bool:
    for lock in (job_lock(deps_dir), launcher_setup.job_lock(deps_dir)):
        with launcher_lock.held_if_free(lock) as free:
            if not free:
                return True
    return False


def start(deps_dir: str, launcher: str) -> bool:
    """Start the detached install job unless one runs or the last one failed.

    True iff started now. Never waits for the job. A failed job is not started
    again by a hook: the failure stamp stays until an install of this set
    succeeds (event-based: no timer), so a persistent failure (offline, a uv
    error) costs one job, not one per hook call.
    """
    if _running(deps_dir) or last_failure(deps_dir):
        return False
    log = launcher_logs.rotate(job_log(deps_dir))
    with log.open("ab") as sink:
        subprocess.Popen(
            [sys.executable, launcher, JOB],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            **launcher_ml.detached(),
        )
    return True


def refuse(deps_dir: str, launcher: str, cause: str, module: str = "") -> None:
    """Report that Cortex is not ready, start the job, exit. Installs nothing.

    Exit 2 for a blocking gate (fail closed: the guarded edit does not run),
    1 for every other hook.
    """
    started = start(deps_dir, launcher)
    failure = last_failure(deps_dir)
    if failure:
        job = f"the last background install failed: {failure}"
    elif started:
        job = "started the install in the background"
    else:
        job = "an install is already running in the background"
    print(
        f"[cortex-launcher] Cortex not ready: {cause}. This hook ran nothing and "
        f"installed nothing (a hook's time budget does not fit an install); "
        f"{job} (log: {job_log(deps_dir)}). To install now and wait, run "
        f"`claude --init-only`, or: {sys.executable} {launcher} {launcher_setup.SETUP}",
        file=sys.stderr,
    )
    sys.exit(_BLOCK_EXIT if module in BLOCKING else 1)


def require_ready(deps_dir: str, launcher: str, module: str = "") -> None:
    """Return when the base set is installed for this uv.lock and Python.

    Postcondition: returns, or ``refuse`` has exited. No install, no subprocess
    other than the detached job, on either branch.
    """
    if not launcher_deps.set_ready(deps_dir, "base"):
        refuse(
            deps_dir,
            launcher,
            "the locked base dependency set is not installed for this uv.lock "
            f"and Python {sys.version_info.major}.{sys.version_info.minor}",
            module,
        )


def run(deps_dir: str, launcher: str) -> int:
    """The job's body; the exit code. A second job returns at once.

    The install runs under the deps lock, so it never runs beside the
    MCP server's or the semantic-search job's own install. A failure leaves the
    failure stamp (``last_failure``); a success removes it, then starts the
    semantic-search job (``launcher_ml.start``, which honours the opt-out and
    never waits).
    """
    with launcher_lock.held_if_free(job_lock(deps_dir)) as mine:
        if not mine:
            print("[cortex-launcher] dependency install already running")
            return 0
        launcher_deps.ensure_deps(deps_dir)
        ready = launcher_deps.set_ready(deps_dir, "base")
    if not ready:
        _failure_stamp(deps_dir).write_text(
            json.dumps({"error": "the locked base set did not install"}),
            encoding="utf-8",
        )
        return 1
    _failure_stamp(deps_dir).unlink(missing_ok=True)
    launcher_ml.start(deps_dir, launcher)
    return 0
