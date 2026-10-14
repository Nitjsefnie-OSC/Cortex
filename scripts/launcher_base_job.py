"""A hook never installs dependencies: it starts this job and says so — stdlib only.

Every hook runs inside a host time budget (5-30 s). Installing the base set
takes 28-109 s and bootstrapping uv or a CPython takes 29 s more, so a hook
that installs is cancelled by the host mid-install, leaving an empty
``deps.tmp-<pid>`` scratch directory behind (64 of them in one plugin data
directory, 2026-10-14). A hook launch whose base set is not installed
therefore exits 1 with the cause and starts at most ONE detached job that
installs it; the MCP server (whose host waits on the stand-in, ADR-1104) and
an explicit ``--install-deps`` are the other places an install runs. The job
holds a lock for its life, so ten hooks starting at once start one job.

source: docs/adr/ADR-1106-no-dependency-install-inside-a-hook.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import launcher_deps
import launcher_lock
import launcher_ml

# The argument the detached job is started with (launcher.py <JOB>).
JOB = "--install-base-job"  # source: ADR-1106


def job_lock(deps_dir: str) -> str:
    """The lock the running job holds: beside ``deps_dir``, in plugin data."""
    return f"{deps_dir}.base-job.lock"


def job_log(deps_dir: str) -> Path:
    """Where the job's output goes: the first place to look when it fails."""
    return Path(deps_dir).parent / "deps-install.log"


def _running(deps_dir: str) -> bool:
    with launcher_lock.held_if_free(job_lock(deps_dir)) as free:
        return not free


def start(deps_dir: str, launcher: str) -> bool:
    """Start the detached install job unless one runs; True iff started now.

    Never waits for the job.
    """
    if _running(deps_dir):
        return False
    log = job_log(deps_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as sink:
        subprocess.Popen(
            [sys.executable, launcher, JOB],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            **launcher_ml.detached(),
        )
    return True


def refuse(deps_dir: str, launcher: str, cause: str) -> None:
    """Report that Cortex is not ready, start the job, exit 1. Installs nothing."""
    started = start(deps_dir, launcher)
    job = (
        "started the install in the background"
        if started
        else "an install is already running in the background"
    )
    print(
        f"[cortex-launcher] Cortex not ready: {cause}. This hook ran nothing and "
        f"installed nothing (a hook's time budget does not fit an install); "
        f"{job} (log: {job_log(deps_dir)}). Start a new session when it "
        f"finishes, or run: {sys.executable} {launcher} mcp_server --install-deps",
        file=sys.stderr,
    )
    sys.exit(1)


def require_ready(deps_dir: str, launcher: str) -> None:
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
        )


def run(deps_dir: str, launcher: str) -> int:
    """The job's body; the exit code. A second job returns at once.

    The install runs under the deps lock, so it never runs beside the
    MCP server's or the semantic-search job's own install. Once the base set is
    stamped the semantic-search job starts (``launcher_ml.start``, which
    honours the opt-out and never waits): the launch that would have started
    it, the SessionStart hook, was the one that refused to install.
    """
    with launcher_lock.held_if_free(job_lock(deps_dir)) as mine:
        if not mine:
            print("[cortex-launcher] dependency install already running")
            return 0
        launcher_deps.ensure_deps(deps_dir)
        ready = launcher_deps.set_ready(deps_dir, "base")
    if not ready:
        return 1
    launcher_ml.start(deps_dir, launcher)
    return 0
