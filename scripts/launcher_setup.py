"""Install everything Cortex needs, in the foreground, once — stdlib only.

This is the plugin's ``Setup`` hook (Claude Code runs it for ``claude --init-only``,
before the first SessionStart) and the program the background pre-install runs
for the next version's set. It installs the base set and the semantic-search
set from uv.lock, then downloads the pinned embedding model, and it is the
only place besides the MCP server and the two detached jobs that installs.

Outcome: exit 0 and ``{"status": "ready"}`` in ``<set>.setup.json``, or exit 1
with the cause printed and ``{"status": "failed", "error": ...}``. The host
discards a Setup hook's output and exit code (measured: Claude Code 2.1.300,
``claude --init-only`` exits 0 whatever the hook did), so everything printed is
also appended to ``<data>/setup.log`` and the outcome file is what a caller
reads (``scripts/init_plugin.py``).

source: ADR-1108
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import launcher_deps
import launcher_gc
import launcher_layout
import launcher_lock
import launcher_logs
import launcher_ml

SETUP = (
    "--setup"  # source: ADR-1108 (the argument the Setup hook starts the launcher with)
)
# One cold install moves about 1 GB and 28 s + 86 s (ADR-1104); a holder of
# this lock that outlives the lock wait of the install itself is reported.
_LOCK_WAIT_SECONDS = 1800  # source: scripts/launcher_deps.py _LOCK_WAIT_SECONDS


def job_lock(deps_dir: str) -> str:
    return f"{deps_dir}.setup.lock"


def log_path(deps_dir: str) -> Path:
    """Everything a Setup run printed: the host shows none of it."""
    return launcher_layout.data_dir(deps_dir) / "setup.log"


def status_file(deps_dir: str) -> Path:
    return Path(f"{deps_dir}.setup.json")


def outcome(deps_dir: str) -> dict:
    """The last Setup run's outcome for this set, ``{}`` when none ran."""
    try:
        data = json.loads(status_file(deps_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(deps_dir: str, status: str, error: str = "") -> None:
    path = status_file(deps_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, "error": error}), encoding="utf-8")


class _Tee:
    """A text stream that also appends to the setup log."""

    def __init__(self, stream, sink) -> None:
        self._stream, self._sink = stream, sink

    def write(self, text: str) -> int:
        self._sink.write(text)
        self._sink.flush()
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _say(message: str) -> None:
    print(f"[cortex-setup] {message}", flush=True)


def _fail(deps_dir: str, error: str) -> int:
    _say(f"FAILED: {error} (log: {log_path(deps_dir)})")
    _record(deps_dir, "failed", error)
    return 1


def _warm_model(deps_dir: str, launcher: str, log) -> str:
    """Download the pinned embedding model; the failure's cause, "" on success."""
    env = dict(os.environ)
    env[launcher_ml.STATUS_ENV] = str(launcher_ml.status_file(deps_dir))
    done = subprocess.run(
        [sys.executable, launcher, launcher_ml.WARMUP_MODULE],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    status, why = launcher_ml.state(deps_dir)
    if done.returncode or status != "ready":
        return why or f"the model download exited {done.returncode}"
    return ""


def _install(deps_dir: str, launcher: str, log) -> int:
    started = time.monotonic()
    _say(f"installing the base dependency set into {deps_dir}")
    launcher_deps.ensure_deps(deps_dir)
    if not launcher_deps.set_ready(deps_dir, "base"):
        return _fail(deps_dir, "the locked base dependency set did not install")
    if os.environ.get(launcher_ml.ZERO_DOWNLOAD_ENV, "").lower() in ("1", "true"):
        _say(f"semantic search skipped: {launcher_ml.ZERO_DOWNLOAD_ENV} is set")
    else:
        _say("installing the semantic-search dependency set (about 1 GB)")
        launcher_deps.ensure_all_deps(deps_dir)
        if not launcher_deps.set_ready(deps_dir, "ml"):
            return _fail(deps_dir, "the locked semantic-search set did not install")
        _say("downloading the embedding model")
        cause = _warm_model(deps_dir, launcher, log)
        if cause:
            return _fail(deps_dir, f"the embedding model did not download: {cause}")
    _record(deps_dir, "ready")
    Path(f"{deps_dir}.base-job.failed").unlink(missing_ok=True)
    _say(f"ready in {time.monotonic() - started:.0f} s")
    return 0


def run(deps_dir: str, launcher: str) -> int:
    """Install base + semantic search + model for ``deps_dir``'s lock; exit code.

    Precondition: ``deps_dir`` is ``<data>/sets/<key>``; sys.path already
    carries it (the launcher did that).
    Postcondition: 0 iff both sets are stamped for this uv.lock and the model
    is cached (or the operator opted out of the download); otherwise 1 with the
    cause printed and recorded. A concurrent Setup of the same set is waited
    for, not duplicated. On success the sets no installed version references
    are removed.
    """
    log_file = launcher_logs.rotate(log_path(deps_dir))
    with log_file.open("a", encoding="utf-8") as log:
        out, err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(out, log), _Tee(err, log)
        try:
            return _run_locked(deps_dir, launcher, log)
        finally:
            sys.stdout, sys.stderr = out, err


def _run_locked(deps_dir: str, launcher: str, log) -> int:
    try:
        with launcher_lock.held(job_lock(deps_dir), _LOCK_WAIT_SECONDS):
            _record(deps_dir, "installing")
            code = _install(deps_dir, launcher, log)
    except launcher_lock.LockTimeoutError as exc:
        return _fail(deps_dir, str(exc))
    if code == 0:
        root = os.environ.get("CORTEX_RUNNING_ROOT") or os.environ.get(
            "CLAUDE_PLUGIN_ROOT", ""
        )
        data = str(launcher_layout.data_dir(deps_dir))
        removed = launcher_gc.collect(root, data) if root else []
        if removed:
            _say(f"removed {len(removed)} unreferenced paths of earlier sets")
    return code
