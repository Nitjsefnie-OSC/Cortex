"""Attach a launcher process to the plugin's managed PostgreSQL server.

Stdlib only, POSIX only (``fcntl``). Three ``flock`` files under the managed
root order everything, with no timer anywhere:

- ``clients.lock``: every attached process holds it shared until it exits;
  the supervisor's exclusive request is granted when the last one is gone.
- ``start.lock``: held exclusively while the server is started; a client
  waits on it, then reads ``status.json``.
- ``supervisor.lock``: held for the supervisor's whole life, so "is one
  running" is a non-blocking lock attempt.

The server is only ever started here, never installed: binaries and cluster
come from ``launcher_pg_provision`` (the Setup hook). source: ADR-1110
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DATABASE = "cortex"  # source: ADR-1110 (the database name every Cortex URL names)
USER = "postgres"  # source: ADR-1110 (the superuser initdb -U creates)
HOST = "127.0.0.1"  # source: ADR-1110 (TCP on loopback only; unix sockets off)
ROOT_ENV = "CORTEX_MANAGED_PG_ROOT"  # source: ADR-1110 (inherited by children)
_HELD: list[int] = []  # client locks, kept open until the process exits


class ManagedPgError(RuntimeError):
    """The managed server could not be made available; the message says why."""


@dataclass(frozen=True)
class PgPaths:
    """Every file of one managed server, all under ``root``."""

    root: Path

    @property
    def dist(self) -> Path:
        return self.root / "dist"

    @property
    def cluster(self) -> Path:
        return self.root / "cluster"

    @property
    def password(self) -> Path:
        return self.root / "password"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def installed(self) -> Path:
        """The tree and cluster Setup installed (an install fact, not a selection)."""
        return self.root / "installed.json"

    @property
    def provisioned(self) -> Path:
        """Written only after the server started and its schema was set up."""
        return self.root / "provisioned.json"

    @property
    def log(self) -> Path:
        return self.root / "supervisor.log"

    @property
    def server_log(self) -> Path:
        return self.root / "postgres.log"

    def lock(self, name: str) -> Path:
        return self.root / f"{name}.lock"


def paths_for(data_root: str) -> PgPaths:
    """``$CLAUDE_PLUGIN_DATA/pg`` (the plugin root in a dev checkout)."""
    return PgPaths(Path(data_root) / "pg")


def open_lock(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def flock(fd: int, *, exclusive: bool, blocking: bool = True) -> bool:
    """Take the lock; ``False`` only when non-blocking and already held."""
    import fcntl  # noqa: PLC0415 — POSIX only; never imported on Windows

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(fd, mode if blocking else mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def write_json(path: Path, payload: dict) -> None:
    """Replace ``path`` atomically, readable by the owner only."""
    partial = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(partial, path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def database_url(paths: PgPaths, port: int) -> str:
    """The URL of the running server; the password is read from its file."""
    secret = quote(paths.password.read_text(encoding="utf-8").strip(), safe="")
    return f"postgresql://{USER}:{secret}@{HOST}:{port}/{DATABASE}"


def serves(environ: dict) -> PgPaths | None:
    """The managed root ``environ`` already names, when its URL is that server's.

    A launcher started by a process attached to the server (a background
    worker a hook spawns) inherits ``DATABASE_URL`` and ``ROOT_ENV``; it must
    attach in turn, or the server could be stopped under it.
    """
    root = environ.get(ROOT_ENV, "")
    if not root:
        return None
    paths = PgPaths(Path(root))
    status = read_json(paths.status)
    if status.get("state") != "running" or not paths.password.is_file():
        return None
    url = database_url(paths, int(status["port"]))
    return paths if environ.get("DATABASE_URL") == url else None


def _spawn_supervisor(paths: PgPaths, fds: tuple[int, int]) -> None:
    """Start the detached supervisor; it inherits the two locks in ``fds``."""
    script = Path(__file__).resolve().parent / "launcher_pg_supervisor.py"
    argv = [sys.executable, str(script), str(paths.root), *map(str, fds)]
    log = os.open(paths.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            pass_fds=fds,
            start_new_session=True,
        )
    finally:
        os.close(log)


def _ensure_supervisor(paths: PgPaths) -> None:
    """Spawn the supervisor unless one is alive.

    The start lock comes first: a client that found ``supervisor.lock`` held
    by a peer that had not yet taken the start lock would read a status
    nobody had written (measured on CI, PR #748).
    """
    start = open_lock(paths.lock("start"))
    flock(start, exclusive=True)  # waits out a start in progress
    supervisor = open_lock(paths.lock("supervisor"))
    try:
        if flock(supervisor, exclusive=True, blocking=False):
            _spawn_supervisor(paths, (supervisor, start))
    finally:
        os.close(supervisor)
        os.close(start)


def attach(paths: PgPaths) -> str:
    """Hold this process as a client of the server and return its URL.

    Postcondition: the server is running, or ``ManagedPgError`` carries the
    supervisor's own error. Exports ``ROOT_ENV`` for this process's children.
    """
    if os.name == "nt":
        raise ManagedPgError("the managed PostgreSQL server does not run on Windows")
    if not paths.root.is_dir():
        raise ManagedPgError(f"the managed PostgreSQL root {paths.root} is missing")
    clients = open_lock(paths.lock("clients"))
    flock(clients, exclusive=False)
    _HELD.append(clients)
    _ensure_supervisor(paths)
    start = open_lock(paths.lock("start"))
    try:
        flock(start, exclusive=False)  # granted once a start in progress is over
    finally:
        os.close(start)
    status = read_json(paths.status)
    if status.get("state") != "running":
        raise ManagedPgError(
            "the Cortex PostgreSQL server is not running: "
            f"{status.get('error') or status.get('state') or 'no status written'}"
            f"\nSee {paths.log}"
        )
    os.environ[ROOT_ENV] = str(paths.root)
    return database_url(paths, int(status["port"]))


def detach() -> None:
    """Release this process's client locks (the supervisor may then stop)."""
    while _HELD:
        os.close(_HELD.pop())
