"""Start and stop one managed cluster with its own ``pg_ctl`` — stdlib only, POSIX.

Readiness is ``pg_ctl start -w`` (PostgreSQL's own wait on the postmaster's
``ready`` line in ``postmaster.pid``); no timer of ours decides anything.

``postmaster.pid`` left by a crash is classified by identity, not by liveness
alone: a PID the kernel no longer knows, or one owned by another user (EPERM:
a postmaster of this cluster runs as this user), is stale; a live PID of this
user is this cluster's postmaster when its executable is this tree's
``bin/postgres`` or, where the kernel reports another executable (an emulator
such as qemu or Rosetta runs the binary), when the port the file records
accepts connections on loopback. A live, reused PID of any other program is
stale too, and the file is removed before a start, because PostgreSQL itself
would otherwise refuse to start ("lock file already exists") until a human
deleted it. Stopping never removes the file: it only stops a live postmaster.

source: ADR-1110
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
from pathlib import Path

_PROC_PIDPATHINFO_MAXSIZE = 4096  # source: <libproc.h> (4 * MAXPATHLEN)
_PID_LINE, _PORT_LINE = (
    0,
    3,
)  # source: PostgreSQL miscinit.c (LOCK_FILE_LINE_PID / _PORT, 1-based 1 and 4)


class ServerError(RuntimeError):
    """``pg_ctl`` failed; the message carries its output."""


def executable_of(pid: int) -> str | None:
    """The executable path of ``pid`` (same user), ``None`` when unknown."""
    if sys.platform == "darwin":
        buffer = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        if libproc.proc_pidpath(pid, buffer, _PROC_PIDPATHINFO_MAXSIZE) <= 0:
            return None
        return buffer.value.decode()
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _alive_as_this_user(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # another user's process: never this cluster's postmaster
    return True


def _accepts(host: str, port: int) -> bool:
    """A loopback connect: refused at once when nothing listens (no timer)."""
    try:
        with socket.create_connection((host, port)):
            return True
    except OSError:
        return False


def _record(cluster: Path) -> tuple[int, int] | None:
    """``(pid, port)`` from ``postmaster.pid``; a torn file is removed."""
    lock = cluster / "postmaster.pid"
    try:
        lines = lock.read_text(encoding="utf-8").splitlines()
        return int(lines[_PID_LINE]), int(lines[_PORT_LINE])
    except FileNotFoundError:
        return None
    except (ValueError, IndexError):
        lock.unlink(missing_ok=True)  # a torn file: no postmaster wrote it whole
        return None


def live_postmaster(cluster: Path, postgres: Path, host: str) -> tuple[int, int] | None:
    """``(pid, port)`` of this cluster's live postmaster, else ``None``. Pure."""
    record = _record(cluster)
    if record is None or not _alive_as_this_user(record[0]):
        return None
    exe = executable_of(record[0])
    if exe is not None and Path(exe).resolve() == postgres.resolve():
        return record
    return record if _accepts(host, record[1]) else None


def free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def _pg_ctl(bin_dir: Path, *args: str) -> None:
    done = subprocess.run(
        [str(bin_dir / "pg_ctl"), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        output = f"{done.stdout}{done.stderr}".strip()
        raise ServerError(f"pg_ctl {args[-1]} exited {done.returncode}: {output}")


def start(bin_dir: Path, cluster: Path, log: Path, host: str) -> int:
    """Start the cluster (or adopt its live postmaster); return its port.

    A ``postmaster.pid`` that names no live postmaster of this cluster is
    removed first: PostgreSQL treats a live PID of this user as a running
    server and refuses to start."""
    running = live_postmaster(cluster, bin_dir / "postgres", host)
    if running is not None:
        return running[1]
    (cluster / "postmaster.pid").unlink(missing_ok=True)
    port = free_port(host)
    _pg_ctl(
        bin_dir, "-D", str(cluster), "-l", str(log), "-w", "-o", f"-p {port}", "start"
    )
    return port


def stop(bin_dir: Path, cluster: Path, host: str) -> None:
    """Fast shutdown, waited for by ``pg_ctl``; nothing to do when stopped."""
    if live_postmaster(cluster, bin_dir / "postgres", host) is None:
        return
    _pg_ctl(bin_dir, "-D", str(cluster), "-m", "fast", "-w", "stop")
