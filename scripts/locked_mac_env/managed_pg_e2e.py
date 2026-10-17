"""Prove the plugin's managed PostgreSQL end to end on this host, then leave nothing.

Runs what Setup and a launch run, in one process, against the real pinned
archives: Setup's managed step (download, sha256, initdb, start, database,
extensions, schema, marker), a launch's attach (``launcher_backend.apply``),
then ``CREATE EXTENSION vector`` and ``pg_trgm`` in a fresh database and a
query through each; finally it detaches and waits on the supervisor's lock
for the server to stop. Prints one JSON report; exit 0 only when every check
passes. Stdlib plus psycopg (the launcher's base set).

Issue #740's locked-down Mac is reproduced by the caller: run it under
``sandbox-exec`` with ``locked_mac_env/no_homebrew.sb`` (no read of
/opt/homebrew or /usr/local) and ``env -i`` with a fresh HOME.

    python3 managed_pg_e2e.py <plugin_root> <work_dir>

source: ADR-1110
source: issue #740
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROBE_DB = "cortex_e2e"  # source: ADR-1110 (a database Cortex never uses)
_ARGC = 3  # source: the usage line in this module's docstring


def _paths(plugin_root: str) -> None:
    for entry in (plugin_root, str(Path(plugin_root) / "scripts")):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _extensions_in_fresh_database(url: str) -> dict:
    import psycopg  # noqa: PLC0415 — the launcher's base set
    from psycopg.conninfo import make_conninfo  # noqa: PLC0415 — same dependency

    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {_PROBE_DB}")
        c.execute(f"CREATE DATABASE {_PROBE_DB}")
    with psycopg.connect(make_conninfo(url, dbname=_PROBE_DB), autocommit=True) as c:
        c.execute("CREATE EXTENSION vector")
        c.execute("CREATE EXTENSION pg_trgm")
        return {
            "extensions": dict(
                c.execute("SELECT extname, extversion FROM pg_extension").fetchall()
            ),
            "vector_distance": c.execute(
                "SELECT '[1,2,3]'::vector <-> '[1,2,4]'"
            ).fetchone()[0],
            "trigram_similarity": c.execute(
                "SELECT similarity('cortex', 'cortexx')"
            ).fetchone()[0],
            "ssl_library": c.execute("SHOW ssl_library").fetchone()[0],
            "version": c.execute("SELECT version()").fetchone()[0],
        }


def _cortex_schema(url: str) -> int:
    import psycopg  # noqa: PLC0415 — the launcher's base set

    with psycopg.connect(url) as c:
        return c.execute(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema = 'public'"
        ).fetchone()[0]


def _wait_stopped(root: Path) -> dict:
    """Block on the supervisor's lock (held for its whole life), then read status."""
    import launcher_pg  # noqa: PLC0415 — on sys.path after _paths

    fd = launcher_pg.open_lock(root / "supervisor.lock")
    try:
        launcher_pg.flock(fd, exclusive=True)
    finally:
        os.close(fd)
    return launcher_pg.read_json(root / "status.json")


def run(plugin_root: str, work: Path) -> dict:
    _paths(plugin_root)
    import launcher_backend  # noqa: PLC0415 — on sys.path after _paths
    import launcher_pg  # noqa: PLC0415 — same
    import launcher_pg_provision  # noqa: PLC0415 — same

    said: list[str] = []
    data = work / "data"
    cause = launcher_pg_provision.setup_step(str(data), said.append)
    report: dict = {"setup_said": said, "setup_cause": cause}
    report["provisioned"] = launcher_pg.read_json(data / "pg" / "provisioned.json")
    if report["provisioned"].get("state") != "ready":
        return report  # the store stays SQLite; the report says why
    launcher_backend.apply()
    url = os.environ["DATABASE_URL"]
    report["backend"] = os.environ.get("CORTEX_MEMORY_STORE_BACKEND")
    report["cortex_tables"] = _cortex_schema(url)
    report.update(_extensions_in_fresh_database(url))
    launcher_pg.detach()
    report["status_after_last_client"] = _wait_stopped(data / "pg")
    report["postmaster_pid_left"] = (
        data / "pg" / "cluster" / "postmaster.pid"
    ).exists()
    return report


def passed(report: dict) -> bool:
    ext = report.get("extensions", {})
    return (
        report.get("setup_cause") == ""
        and report.get("backend") == "postgresql"
        and report.get("cortex_tables", 0) > 0
        and ext.get("vector") is not None
        and ext.get("pg_trgm") is not None
        and report.get("status_after_last_client") == {"state": "stopped"}
        and report.get("postmaster_pid_left") is False
    )


def main(argv: list[str]) -> int:
    if len(argv) != _ARGC:
        print(f"usage: {Path(argv[0]).name} <plugin_root> <work_dir>", file=sys.stderr)
        return 2
    report = run(argv[1], Path(argv[2]))
    report["passed"] = passed(report)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
