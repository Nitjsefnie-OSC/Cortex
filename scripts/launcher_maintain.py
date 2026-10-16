"""Install the next version's set ahead of its update, and drop old sets — stdlib only.

A hook (SessionStart, UserPromptSubmit) asks ``start_if_needed``: when the
marketplace clone holds a newer plugin version whose uv.lock set is not
installed, or an installed set is no longer referenced, it starts ONE detached
``--maintain-job`` and returns. The job runs the CLONE's own launcher with
``--setup`` (so the clone's uv.lock and project define the set), then collects
the unreferenced sets. The hook itself reads three small JSON files and hashes
at most three uv.lock files; it installs nothing and removes only the markers
of dead users (``<key>.users/<pid>``, via ``launcher_gc.in_use``).

The job reads only what is on this machine: it never asks the marketplace's
git remote whether a release is out (owner decision 2026-10-16), so the next
version's set is installed ahead only when the local clone already holds it.

source: ADR-1108
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import launcher_gc
import launcher_installed
import launcher_layout
import launcher_lock
import launcher_logs
import launcher_ml
import launcher_setup

JOB = "--maintain-job"  # source: ADR-1108


@dataclass(frozen=True)
class Plan:
    """What the job would do: the tree whose set to install, whether sets are stale."""

    install: Path | None
    stale: bool

    def empty(self) -> bool:
        return self.install is None and not self.stale


def job_lock(data: str) -> str:
    return str(Path(data) / "maintain.lock")


def job_log(data: str) -> Path:
    return Path(data) / "maintain.log"


def plan(running_root: str, data: str) -> Plan:
    """The work to do now. Installs nothing; removes no set, only the markers
    of dead users that ``launcher_gc.in_use`` drops while it reads.

    Old sets are work only when a doomed path is not already recorded as
    impossible to remove (``launcher_gc.failed_removals``): a path the last
    collection could not remove does not start a job on every prompt; a new
    unreferenced set, or a successful Setup, collects again.
    """
    installed = launcher_installed.find(running_root, data)
    if installed is None:
        return Plan(None, False)
    install = None
    following = launcher_installed.next_tree(installed)
    if (
        following is not None
        and launcher_installed.version_of(following)
        != launcher_installed.version_of(Path(running_root))
        and (following / "scripts" / "launcher_setup.py").is_file()
    ):
        key = launcher_layout.set_key(following)
        if key is not None and not launcher_layout.both_stamped(data, key):
            install = following
    keep = launcher_gc.referenced_keys(running_root, data)
    stale = keep is not None and launcher_gc.has_new_work(data, keep)
    return Plan(install, stale)


def start_if_needed(running_root: str, deps_dir: str, launcher: str) -> bool:
    """Start the detached job when there is work and none runs; True iff started.

    Never waits, installs or removes a set. A host record that cannot be read
    is "no work" and is named on stderr (``launcher_installed._read``); a
    ``sets/`` or user registry that cannot be listed raises OSError, which
    ``after_runtime`` prints.
    """
    data = str(launcher_layout.data_dir(deps_dir))
    if plan(running_root, data).empty():
        return False
    with launcher_lock.held_if_free(job_lock(data)) as free:
        if not free:
            return False
    log = launcher_logs.rotate(job_log(data))
    env = dict(os.environ)
    env["CORTEX_RUNNING_ROOT"] = running_root
    with log.open("ab") as sink:
        subprocess.Popen(
            [sys.executable, launcher, JOB],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            env=env,
            **launcher_ml.detached(),
        )
    return True


def _setup(tree: Path, running_root: str, data: str) -> int:
    """Run ``tree``'s own launcher with --setup for ``data``; its exit code."""
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(tree)
    env["CLAUDE_PLUGIN_DATA"] = data
    env["CORTEX_RUNNING_ROOT"] = running_root
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / "launcher.py"), launcher_setup.SETUP],
        stdin=subprocess.DEVNULL,
        env=env,
    ).returncode


def run(running_root: str, data: str) -> int:
    """The job body: install the next set when due, then collect old sets.

    Postcondition: a second job returns at once (the lock); the marketplace
    clone's own ``launcher.py --setup`` ran to completion (its exit code is
    returned) or there was none to run; unreferenced sets were collected (by
    that Setup run on success, here otherwise). 1 when a path could not be
    removed: ``launcher_gc.collect`` printed each one with its cause and
    recorded it. Runs no git and reaches no remote.
    """
    with launcher_lock.held_if_free(job_lock(data)) as mine:
        if not mine:
            return 0
        work = plan(running_root, data)
        if work.install is not None:
            return _setup(work.install, running_root, data)
        launcher_gc.collect(running_root, data)
        return 1 if launcher_gc.failed_removals(data) else 0


# The hooks that look for the next version's set and for old sets to drop:
# once per session start and once per prompt (source: ADR-1108).
HOOKS = frozenset({"mcp_server.hooks.session_start", "mcp_server.hooks.auto_recall"})


def after_runtime(module: str, plugin_root: str, deps_dir: str, launcher: str) -> None:
    """Register the server's use of its set; hooks look for maintenance work.

    FAILS_ON: the data directory is unwritable (no log, no marker). A hook then
    says so on stderr and runs on; the server cannot register and fails, because
    a set removed under a live server is worse than no server.
    """
    if module == "mcp_server":
        launcher_gc.mark_in_use(deps_dir)
    elif module in HOOKS:
        try:
            start_if_needed(plugin_root, deps_dir, launcher)
        except OSError as exc:
            print(f"[cortex-launcher] maintenance not started: {exc}", file=sys.stderr)


def main(plugin_root: str, deps_dir: str) -> int:
    """``--maintain-job``: the running version is the one that started the job."""
    running = os.environ.get("CORTEX_RUNNING_ROOT") or plugin_root
    return run(running, str(launcher_layout.data_dir(deps_dir)))
