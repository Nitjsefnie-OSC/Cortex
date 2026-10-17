"""The detached process that owns one managed server for its lifetime.

``launcher_pg.attach`` spawns it holding ``supervisor.lock`` and ``start.lock``.
It starts the provisioned cluster (it never downloads or creates one: that is
Setup's job, ADR-1108), writes ``status.json`` only after ``pg_ctl`` reported
the server ready, releases the start lock, blocks until no client holds
``clients.lock`` and stops the server. Its stdout/stderr are
``supervisor.log``; the password never reaches it.

    python3 launcher_pg_supervisor.py <pg_root> <supervisor_fd> <start_fd>

source: ADR-1110
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import launcher_pg as _pg  # noqa: E402
import launcher_pg_server as _server  # noqa: E402

_ARGC = 4  # source: ADR-1110 (script + the three arguments above)


def _log(message: str) -> None:
    print(f"[cortex-pg] {message}", file=sys.stderr, flush=True)


def bin_dir(paths: _pg.PgPaths) -> Path:
    """The installed tree's ``bin``, as Setup recorded it in ``installed.json``."""
    tree = _pg.read_json(paths.installed).get("tree")
    if not tree:
        raise _pg.ManagedPgError(
            f"the managed PostgreSQL server is not installed ({paths.installed}"
            " records no tree); run `claude --init-only`"
        )
    binaries = Path(tree) / "bin"
    if not (binaries / "postgres").is_file():
        raise _pg.ManagedPgError(
            f"{binaries / 'postgres'} is missing; run `claude --init-only`"
        )
    return binaries


def _start(paths: _pg.PgPaths) -> Path:
    binaries = bin_dir(paths)
    port = _server.start(binaries, paths.cluster, paths.server_log, _pg.HOST)
    _pg.write_json(paths.status, {"state": "running", "port": port, "pid": os.getpid()})
    _log(f"server ready on {_pg.HOST}:{port}")
    return binaries


def _serve_until_last_client(
    paths: _pg.PgPaths, binaries: Path, supervisor: int
) -> None:
    clients = _pg.open_lock(paths.lock("clients"))
    _pg.flock(clients, exclusive=True)  # granted when the last client has exited
    final = {"state": "failed", "error": "interrupted while stopping"}
    try:
        _server.stop(binaries, paths.cluster, _pg.HOST)
        final = {"state": "stopped"}
        _log("server stopped: no client left")
    except Exception as exc:  # noqa: BLE001 — recorded for the next client, then re-raised
        final = {"state": "failed", "error": f"stop: {exc}"}
        raise
    finally:
        _pg.write_json(paths.status, final)
        os.close(supervisor)  # before clients: a new client must see no supervisor
        os.close(clients)


def supervise(paths: _pg.PgPaths, supervisor: int, start: int) -> int:
    try:
        binaries = _start(paths)
    except Exception as exc:  # noqa: BLE001 — written for the waiting clients, then re-raised
        _pg.write_json(paths.status, {"state": "failed", "error": str(exc)})
        _log(f"failed: {exc}")
        raise
    finally:
        os.close(start)
    _serve_until_last_client(paths, binaries, supervisor)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != _ARGC:
        _log(f"usage: {Path(argv[0]).name} <pg_root> <supervisor_fd> <start_fd>")
        return 2
    root, supervisor, start = argv[1:]
    return supervise(_pg.PgPaths(Path(root)), int(supervisor), int(start))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
