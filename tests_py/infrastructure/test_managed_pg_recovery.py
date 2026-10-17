"""managed_pg_server: an unrecorded postmaster, and one that outlived its supervisor.

PR #748's round-4 review, N2: a supervisor killed between spawning the
postmaster and recording it left an orphan; the next ``start()`` deleted the
orphan's ``postmaster.pid`` *before* trying ``postmaster.lock``, failed with a
message naming neither PID nor port, and recovery came only from
PostgreSQL's once-a-minute lock-file recheck shutting the orphan down. Now
``postmaster.pid`` is touched only while ``postmaster.lock`` is held, and the
holder nothing recorded is adopted when it proves itself with the cluster's
password, or named (PID and port) and left alone when it cannot.

source: ADR-1110
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests_py.infrastructure import fake_postgres

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX only (ADR-1110)")
HOST = "127.0.0.1"  # source: managed_pg.HOST
# source: test helper — start argv[3:] in its own session, holding the lock
# file argv[1] and the pipe end fd argv[2], as the supervisor does; print its pid
_ORPHAN = (
    "import fcntl, os, subprocess, sys\n"
    "lock = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(lock, fcntl.LOCK_EX)\n"
    "d = subprocess.DEVNULL\n"
    "p = subprocess.Popen(sys.argv[3:], start_new_session=True, stdin=d,"
    " stdout=d, stderr=d, pass_fds=(lock, int(sys.argv[2])))\n"
    "print(p.pid)\n"
)
# source: test helper — a process of this user that holds the lock file argv[1],
# listens on loopback, and reports every SIGINT and connection it receives
_HOLDER = (
    "import fcntl, os, signal, socket, sys, threading\n"
    "signal.signal(signal.SIGINT, lambda *a: print('signalled', flush=True))\n"
    "lock = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(lock, fcntl.LOCK_EX)\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
    "print(s.getsockname()[1], flush=True)\n"
    "def serve():\n"
    "    while True:\n"
    "        c, _ = s.accept(); print('dialled', flush=True); c.close()\n"
    "threading.Thread(target=serve, daemon=True).start()\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def pg(tmp_path):
    from mcp_server.infrastructure import managed_pg, managed_pg_server

    paths = managed_pg.PgPaths(tmp_path / "root")
    paths.cluster.mkdir(parents=True)
    paths.password.write_text("secret\n")
    return managed_pg, managed_pg_server, paths


def _orphan(server, paths, script) -> tuple[int, int]:
    """A ready fake postmaster holding postmaster.lock, child of no one here."""
    port = server.free_port(HOST)
    exit_r, exit_w = os.pipe()
    argv = [str(script), "-D", str(paths.cluster), "-p", str(port)]
    lock = str(paths.lock("postmaster"))
    command = [sys.executable, "-c", _ORPHAN, lock, str(exit_w), *argv]
    done = subprocess.run(
        command, capture_output=True, text=True, check=True, pass_fds=(exit_w,)
    )
    os.close(exit_w)
    pid = int(done.stdout)
    try:
        server._until(
            paths.cluster, exit_r, lambda: server._is_ready(paths.cluster, pid)
        )
    finally:
        os.close(exit_r)
    return pid, port


def test_an_adopted_postmaster_is_stopped_on_its_exit_event(pg, tmp_path):
    # Its supervisor died after recording it: no child of this process.
    managed_pg, server, paths = pg
    pid, port = _orphan(server, paths, fake_postgres.install(tmp_path / "bin"))
    from mcp_server.infrastructure import managed_pg_process

    started = managed_pg_process.started(pid)
    managed_pg.write_json(
        paths.postmaster, {"pid": pid, "started": started, "port": port}
    )
    assert server.start(tmp_path / "bin", paths) == (port, None)  # adopted
    server.stop(paths, None)
    assert server.live_postmaster(paths) is None


def test_an_unrecorded_postmaster_holding_the_lock_is_proven_and_adopted(pg, tmp_path):
    # Its supervisor died between spawning and recording it (N2).
    managed_pg, server, paths = pg
    pid, port = _orphan(server, paths, fake_postgres.install(tmp_path / "bin"))
    assert not paths.postmaster.exists()
    assert server.start(tmp_path / "bin", paths) == (port, None)
    assert managed_pg.read_json(paths.postmaster)["pid"] == pid  # recorded now
    assert server.live_postmaster(paths).pid == pid
    server.stop(paths, None)
    assert server.live_postmaster(paths) is None


def test_an_unrecorded_lock_holder_that_cannot_sign_is_named_and_left_alone(
    pg, tmp_path, monkeypatch
):
    managed_pg, server, paths = pg
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(paths.lock("postmaster"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    port = int(holder.stdout.readline())
    pid_file = paths.cluster / "postmaster.pid"
    pid_file.write_text(
        f"{holder.pid}\n{paths.cluster}\n0\n{port}\n\n{HOST}\n1 2\nready   \n"
    )
    original = server._recover_unrecorded

    def checked(paths_):
        # The order the review asked for: postmaster.lock first, the file after.
        assert pid_file.exists(), "postmaster.pid removed before postmaster.lock"
        return original(paths_)

    monkeypatch.setattr(server, "_recover_unrecorded", checked)
    try:
        with pytest.raises(
            server.ServerError, match=f"pid {holder.pid} on port {port}"
        ):
            server.start(fake_postgres.install(tmp_path / "bin").parent, paths)
        assert pid_file.exists()
        assert not paths.postmaster.exists()
    finally:
        holder.stdin.close()
        seen = holder.stdout.read().split()
        holder.wait()
    assert "signalled" not in seen


def _dead_but_running(tmp_path):
    """A root whose status says running while its recorded postmaster is dead."""
    from mcp_server.infrastructure import managed_pg

    paths = managed_pg.PgPaths(tmp_path / "root")
    paths.cluster.mkdir(parents=True)
    paths.password.write_text("secret\n")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    record = {"pid": dead.pid, "started": "1.0", "port": 1111}
    managed_pg.write_json(paths.postmaster, record)
    managed_pg.write_json(paths.status, {"state": "running", "port": 1111, "pid": 1})
    return paths


def _restart_as_the_supervisor_does(paths, alive_pid: int) -> None:
    """Under the start lock, the new record then the new status."""
    from mcp_server.infrastructure import managed_pg
    from mcp_server.infrastructure import managed_pg_process as process

    start = managed_pg.open_lock(paths.lock("start"))
    managed_pg.flock(start, exclusive=True)
    new = {"pid": alive_pid, "started": process.started(alive_pid), "port": 2222}
    managed_pg.write_json(paths.postmaster, new)
    managed_pg.write_json(paths.status, {"state": "running", "port": 2222, "pid": 1})
    os.close(start)


def _report_first_wait(monkeypatch):
    """A queue that receives "waiting" when the client first waits."""
    import queue

    from mcp_server.infrastructure import managed_pg_liveness

    events: queue.Queue = queue.Queue()
    said: list[bool] = []
    original = managed_pg_liveness.await_change

    def waiting(root, status):
        if not said:  # once: later wake-ups re-check and may wait again
            said.append(True)
            events.put("waiting")
        original(root, status)

    monkeypatch.setattr(managed_pg_liveness, "await_change", waiting)
    return events


def test_a_client_never_takes_a_running_status_whose_postmaster_is_dead(
    tmp_path, monkeypatch
):
    """Round-4 review N3, client's half: status still says "running" right
    after a death; the client waits for the restart, never hands out the dead
    URL. The test plays the supervisor (it holds supervisor.lock) and restarts
    only once the client says it waits. The restarted postmaster is another
    process: qemu-user fakes /proc/self/stat."""
    import threading

    from mcp_server.infrastructure import managed_pg

    paths = _dead_but_running(tmp_path)
    supervisor = managed_pg.open_lock(paths.lock("supervisor"))
    managed_pg.flock(supervisor, exclusive=True)
    events = _report_first_wait(monkeypatch)
    alive = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    client = threading.Thread(target=lambda: events.put(managed_pg._attach_once(paths)))
    client.start()
    try:
        first = events.get()  # a queue hand-off between threads, not a clock
        assert first == "waiting", f"handed out {first} for a dead postmaster"
        _restart_as_the_supervisor_does(paths, alive.pid)
        assert events.get().endswith("@127.0.0.1:2222/cortex")
    finally:
        client.join()
        managed_pg.detach()
        os.close(supervisor)
        alive.stdin.close()
        alive.wait()
