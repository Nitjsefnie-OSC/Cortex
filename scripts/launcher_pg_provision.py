"""Setup's managed-PostgreSQL step: install, initialise, start, prove, then select.

Run by the Setup hook (``launcher_setup``) after the base set is installed, so
psycopg is importable. In order, under the managed root's install lock:

1. the pinned archives (``launcher_pg_assets``), sha256-verified;
2. a cluster, once: ``initdb --pwfile`` (the password never reaches argv),
   scram-sha-256, UTF8, C locale, TCP on 127.0.0.1 only, no unix socket;
3. the server, through the same supervisor every launch attaches to;
4. the ``cortex`` database, ``vector`` and ``pg_trgm``, pgvector's iterative
   scan, the schema (``setup_db._init_schema``: any failed statement fails);
5. only then ``provisioned.json`` and, for an eligible store, the ``managed``
   marker. A failure before that writes no selection: the store stays SQLite.

source: ADR-1110
source: ADR-1108
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import launcher_pg
import launcher_pg_assets

_PASSWORD_BYTES = 24  # source: ADR-1110 (192 random bits)
# source: ADR-1110 (loopback TCP only; an empty list disables unix sockets,
# whose path limit the plugin data directory can exceed)
_SERVER_CONF = "\nlisten_addresses = '127.0.0.1'\nunix_socket_directories = ''\n"
_EXTENSIONS = ("vector", "pg_trgm")  # source: mcp_server/infrastructure/pg_schema.py


def _password(paths: launcher_pg.PgPaths) -> Path:
    if not paths.password.is_file():
        fd = os.open(paths.password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(_PASSWORD_BYTES) + "\n")
    return paths.password


def _initdb(paths: launcher_pg.PgPaths, tree: Path) -> None:
    """Create the cluster once, atomically: a failed initdb leaves no cluster."""
    if (paths.cluster / "PG_VERSION").is_file():
        return
    scratch = paths.root / f".cluster-{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    done = subprocess.run(
        [str(tree / "bin" / "initdb"), "-D", str(scratch), "-U", launcher_pg.USER,
         f"--pwfile={_password(paths)}", "--auth=scram-sha-256", "--encoding=UTF8",
         "--locale=C", "--no-instructions"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if done.returncode:
        shutil.rmtree(scratch, ignore_errors=True)
        output = f"{done.stdout}{done.stderr}".strip()
        raise launcher_pg.ManagedPgError(f"initdb exited {done.returncode}: {output}")
    with (scratch / "postgresql.conf").open("a", encoding="utf-8") as conf:
        conf.write(_SERVER_CONF)
    shutil.rmtree(paths.cluster, ignore_errors=True)  # no PG_VERSION: not a cluster
    os.replace(scratch, paths.cluster)


def _prepare_database(url: str) -> None:
    """The ``cortex`` database, both extensions, pgvector's check, the schema."""
    import psycopg  # noqa: PLC0415 — in the base set, importable once Setup installed it
    import setup_db  # noqa: PLC0415 — sibling script, stdlib at import time
    from psycopg import sql  # noqa: PLC0415 — same dependency as above
    from psycopg.conninfo import make_conninfo  # noqa: PLC0415 — same dependency as above

    with psycopg.connect(
        make_conninfo(url, dbname="postgres"), autocommit=True
    ) as conn:
        found = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (launcher_pg.DATABASE,)
        ).fetchone()
        if found is None:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(launcher_pg.DATABASE)
                )
            )
    with psycopg.connect(url, autocommit=True) as conn:
        for name in _EXTENSIONS:
            conn.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                    sql.Identifier(name)
                )
            )
        conn.execute(setup_db.ITERATIVE_SCAN_CHECK)
    ok, error = setup_db._init_schema(url)
    if not ok:
        raise launcher_pg.ManagedPgError(f"schema initialization failed: {error}")


def provision(paths: launcher_pg.PgPaths) -> None:
    """Install, initialise, start and prove the managed server; record it ready.

    Raises ``launcher_pg_assets.UnsupportedError`` on a host without a pinned
    archive, ``AssetError``/``ManagedPgError``/``OSError``/psycopg errors on any
    failure; ``provisioned.json`` is then left as it was.
    """
    target = launcher_pg_assets.host_triple()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.root.chmod(0o700)
    install = launcher_pg.open_lock(paths.lock("install"))
    try:
        launcher_pg.flock(install, exclusive=True)  # one install at a time, any key
        tree = launcher_pg_assets.install(paths.dist, target)
        _initdb(paths, tree)
        launcher_pg.write_json(paths.installed, {"tree": str(tree), "target": target})
        try:
            _prepare_database(launcher_pg.attach(paths))
        finally:
            launcher_pg.detach()
        record = {
            "state": "ready",
            "version": launcher_pg_assets.PG_VERSION,
            "target": target,
        }
        launcher_pg.write_json(paths.provisioned, record)
    finally:
        os.close(install)


def _select(paths: launcher_pg.PgPaths) -> str:
    """Write the ``managed`` marker unless a launch chose a store meanwhile.

    Eligibility is checked again here, right before the write: a first launch
    during provisioning writes its own marker (and may create the SQLite
    store), which must not be overwritten. Returns why nothing was written."""
    from mcp_server.infrastructure.backend_marker import (  # noqa: PLC0415 — resolvable once plugin_root is on sys.path
        BACKEND_MARKER_PATH,
        MANAGED,
        read_backend_marker,
        write_backend_marker,
    )
    import launcher_pg_select  # noqa: PLC0415 — needs mcp_server on sys.path

    marker = read_backend_marker()
    why = launcher_pg_select.why_not_eligible(os.environ, marker, BACKEND_MARKER_PATH)
    if why:
        return why
    write_backend_marker(MANAGED, launcher_pg_select.WRITTEN_BY, root=str(paths.root))
    return ""


def _target(data_dir: str) -> tuple[launcher_pg.PgPaths | None, bool, str]:
    """``(root, already managed, why not)`` for this Setup's managed step."""
    import launcher_pg_select  # noqa: PLC0415 — needs mcp_server on sys.path
    from mcp_server.infrastructure.backend_marker import (  # noqa: PLC0415 — resolvable once plugin_root is on sys.path
        BACKEND_MARKER_PATH,
        MANAGED,
        read_backend_marker,
    )

    marker = read_backend_marker()
    if marker is not None and marker["backend"] == MANAGED:
        return launcher_pg.PgPaths(Path(marker["root"])), True, ""
    why = launcher_pg_select.why_not_eligible(os.environ, marker, BACKEND_MARKER_PATH)
    if why:
        return None, False, why
    return launcher_pg.paths_for(data_dir), False, ""


def setup_step(data_dir: str, say) -> str:
    """Setup's managed-PostgreSQL step; ``""`` or the cause Setup must fail with.

    A store already on the managed server must stay reachable, so its failure
    fails Setup. For an eligible store a failure is printed and leaves SQLite
    selected (the fallback, ADR-1110); anything else is left untouched.
    """
    paths, managed, why = _target(data_dir)
    if paths is None:
        say(f"managed PostgreSQL not selected: {why}")
        return ""
    try:
        provision(paths)
    except launcher_pg_assets.UnsupportedError as exc:
        say(f"managed PostgreSQL unavailable here ({exc}); memories stay in SQLite")
        return str(exc) if managed else ""
    except Exception as exc:  # noqa: BLE001 — every failure is printed with its cause; selection decides fatality
        cause = f"managed PostgreSQL did not start: {type(exc).__name__}: {exc}"
        launcher_pg.write_json(paths.provisioned, {"state": "failed", "error": cause})
        if managed:
            return cause
        say(f"{cause}; memories stay in SQLite (log: {paths.log})")
        return ""
    why = "" if managed else _select(paths)
    if why:
        say(f"managed PostgreSQL started but not selected: {why}")
        return ""
    say(f"managed PostgreSQL ready at {paths.root}")
    return ""
