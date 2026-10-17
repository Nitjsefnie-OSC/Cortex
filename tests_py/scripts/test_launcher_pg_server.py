"""scripts/launcher_pg_server.py: a stale postmaster.pid never blocks a start.

PR #748's review (2026-10-16, blocker 4): the pg0 supervisor treated the
``PermissionError`` of ``kill(pid, 0)`` as "alive", so a ``postmaster.pid``
whose PID had been reused by another user's process blocked the server until
a human deleted the file. Here a PID is this cluster's postmaster only when it
is alive as this user AND it is this tree's ``bin/postgres`` (or, when an
emulator hides the executable, its recorded port accepts connections).

source: ADR-1110
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"  # source: launcher_pg.HOST
pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX only (ADR-1110)")


@pytest.fixture
def server(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_pg_server

    return launcher_pg_server


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _pid_file(cluster: Path, pid: int, port: int | None = None) -> Path:
    cluster.mkdir(parents=True, exist_ok=True)
    lock = cluster / "postmaster.pid"
    port = _closed_port() if port is None else port
    lock.write_text(f"{pid}\n{cluster}\n1792209382\n{port}\n\n{HOST}\n")
    return lock


_WAIT = (
    "import sys; sys.stdin.read()"  # source: test helper (blocks until stdin closes)
)
_LISTEN = (
    "import socket, sys; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen();"
    " print(s.getsockname()[1], flush=True); sys.stdin.read()"
)  # source: test helper (a live process listening on loopback)


@pytest.fixture
def child():
    started: list[subprocess.Popen] = []

    def spawn(code: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        started.append(proc)
        return proc

    yield spawn
    for proc in started:
        proc.stdin.close()
        proc.wait()


def _live(server, cluster: Path, postgres: Path):
    return server.live_postmaster(cluster, postgres, HOST)


def test_a_pid_owned_by_another_user_is_not_a_postmaster(server, tmp_path):
    # PID 1 (launchd / init) runs as root: kill(1, 0) is EPERM for a user.
    if os.geteuid() == 0:
        pytest.skip("root may signal PID 1")
    _pid_file(tmp_path / "cluster", 1)
    assert _live(server, tmp_path / "cluster", tmp_path / "postgres") is None


def test_a_reused_pid_of_this_user_is_not_a_postmaster(server, tmp_path, child):
    proc = child(_WAIT)
    _pid_file(tmp_path / "cluster", proc.pid)
    assert _live(server, tmp_path / "cluster", tmp_path / "postgres") is None


def test_this_trees_live_postmaster_is_recognised_by_its_executable(
    server, tmp_path, child
):
    proc = child(_WAIT)
    exe = server.executable_of(proc.pid)
    assert exe, "the platform reports the executable of a process of this user"
    postgres = tmp_path / "bin" / "postgres"
    postgres.parent.mkdir()
    postgres.symlink_to(exe)
    _pid_file(tmp_path / "cluster", proc.pid, port=50123)
    assert _live(server, tmp_path / "cluster", postgres) == (proc.pid, 50123)


def test_an_emulated_postmaster_is_recognised_by_its_listening_port(
    server, tmp_path, child
):
    # Under qemu or Rosetta /proc/<pid>/exe names the emulator, not postgres.
    proc = child(_LISTEN)
    port = int(proc.stdout.readline())
    _pid_file(tmp_path / "cluster", proc.pid, port=port)
    assert _live(server, tmp_path / "cluster", tmp_path / "postgres") == (
        proc.pid,
        port,
    )


def test_a_dead_pid_is_not_a_postmaster(server, tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _pid_file(tmp_path / "cluster", proc.pid)
    assert _live(server, tmp_path / "cluster", tmp_path / "postgres") is None


def test_a_torn_pid_file_is_removed(server, tmp_path):
    cluster = tmp_path / "cluster"
    cluster.mkdir()
    (cluster / "postmaster.pid").write_text("12")
    assert _live(server, cluster, tmp_path / "postgres") is None
    assert not (cluster / "postmaster.pid").exists()


def _fake_pg_ctl(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "pg_ctl").write_text(f"#!/bin/sh\n{body}\n")
    (bin_dir / "pg_ctl").chmod(0o755)


def test_start_removes_a_reused_pid_file_and_runs_pg_ctl(server, tmp_path, child):
    proc = child(_WAIT)
    calls = tmp_path / "calls"
    _fake_pg_ctl(tmp_path / "bin", f'echo "$@" >> {calls}')
    lock = _pid_file(tmp_path / "cluster", proc.pid)
    port = server.start(tmp_path / "bin", tmp_path / "cluster", tmp_path / "log", HOST)
    assert not lock.exists()  # PostgreSQL would refuse a live PID of this user
    recorded = calls.read_text()
    assert recorded.startswith(f"-D {tmp_path / 'cluster'} -l {tmp_path / 'log'} -w")
    assert f"-o -p {port} start" in recorded


def test_stop_never_removes_the_pid_file_of_a_live_postmaster(server, tmp_path, child):
    proc = child(_LISTEN)
    port = int(proc.stdout.readline())
    calls = tmp_path / "calls"
    _fake_pg_ctl(tmp_path / "bin", f'echo "$@" >> {calls}')
    lock = _pid_file(tmp_path / "cluster", proc.pid, port=port)
    server.stop(tmp_path / "bin", tmp_path / "cluster", HOST)
    assert lock.exists()
    assert calls.read_text().strip().endswith("-m fast -w stop")


def test_a_failing_pg_ctl_raises_with_its_output(server, tmp_path):
    _fake_pg_ctl(tmp_path / "bin", "echo 'could not start server' >&2\nexit 1")
    with pytest.raises(server.ServerError, match="could not start server"):
        server.start(tmp_path / "bin", tmp_path / "cluster", tmp_path / "log", HOST)
