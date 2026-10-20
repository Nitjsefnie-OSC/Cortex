"""Versioned schema migrations (ADR-1112 increment 1).

Each database test runs on its own scratch database on the test server,
named like the suite's per-process databases (``cortex_test_pw<pid>_<hex>``)
so an orphan is dropped by ``_pg_throwaway_db.drop_dead_orphaned_databases``.

source: ADR-1112
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

import pytest

from mcp_server.infrastructure import pg_migrations as mig
from tests_py import _pg_throwaway_db
from tests_py.conftest import _USE_PG

psycopg = pytest.importorskip("psycopg")

needs_pg = pytest.mark.skipif(not _USE_PG, reason="requires isolated PostgreSQL")


def _state(version, recorded, fresh=False) -> mig.SchemaState:
    return mig.SchemaState(fresh=fresh, version=version, recorded_hash=recorded)


CURRENT = mig.SCHEMA_HASHES[mig.SCHEMA_VERSION]


def test_the_code_ddl_is_the_released_fingerprint_of_its_version():
    """A DDL change without a new SCHEMA_VERSION entry fails here."""
    from mcp_server.infrastructure.pg_schema_fingerprint import compute_ddl_hash  # noqa: PLC0415

    assert compute_ddl_hash() == CURRENT


def test_versions_only_grow_and_every_one_has_a_fingerprint():
    assert sorted(mig.SCHEMA_HASHES) == list(range(1, mig.SCHEMA_VERSION + 1))


@pytest.mark.parametrize(
    ("state", "error"),
    [
        (_state(mig.SCHEMA_VERSION + 1, "x"), mig.SchemaTooNewError),
        (_state(None, "c867454173d6227e-codex-wheel"), mig.SchemaBehindError),
        (_state(None, None), mig.SchemaBehindError),
        (_state(mig.SCHEMA_VERSION - 1, CURRENT), mig.SchemaBehindError),
        (
            _state(mig.SCHEMA_VERSION, "rewritten-by-an-older-install"),
            mig.SchemaTamperedError,
        ),
        (_state(mig.SCHEMA_VERSION, None), mig.SchemaTamperedError),
    ],
)
def test_runtime_refuses_every_state_that_is_not_current(state, error):
    with pytest.raises(error):
        mig.check_runtime(state)


def test_runtime_accepts_current_and_legacy_current_and_reports_first_run():
    assert mig.check_runtime(_state(mig.SCHEMA_VERSION, CURRENT)) is True
    assert mig.check_runtime(_state(None, CURRENT)) is True  # migrated before ADR-1112
    assert mig.check_runtime(_state(None, None, fresh=True)) is False


@pytest.fixture
def scratch_url() -> Iterator[str]:
    base = os.environ["DATABASE_URL"]
    maint = _pg_throwaway_db.maintenance_url(base)
    name = f"cortex_test_pw{os.getpid()}_{os.urandom(4).hex()}"
    with psycopg.connect(maint, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield base.rpartition("/")[0] + "/" + name
    finally:
        _pg_throwaway_db.drop_isolated_database(maint, name)


def _read(url: str, sql: str):
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(sql).fetchall()


@needs_pg
def test_first_migration_records_version_and_fingerprint(scratch_url):
    assert mig.apply_schema(scratch_url) > 0
    assert _read(scratch_url, "SELECT version, ddl_hash FROM schema_version") == [
        (mig.SCHEMA_VERSION, CURRENT)
    ]
    assert _read(scratch_url, "SELECT ddl_hash FROM schema_meta") == [(CURRENT,)]
    assert mig.apply_schema(scratch_url) == 0


@needs_pg
def test_a_newer_schema_is_refused_and_left_untouched(scratch_url):
    mig.apply_schema(scratch_url)
    future = mig.SCHEMA_VERSION + 1
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO schema_version VALUES (%s, 'future', 'future', now())",
            (future,),
        )
    with pytest.raises(mig.SchemaTooNewError):
        mig.apply_schema(scratch_url)
    from mcp_server.infrastructure.pg_store import PgMemoryStore  # noqa: PLC0415 — PG-only import

    with pytest.raises(mig.SchemaTooNewError):
        PgMemoryStore(database_url=scratch_url)
    assert _read(scratch_url, "SELECT max(version) FROM schema_version") == [(future,)]


@needs_pg
def test_a_failed_statement_records_nothing_and_is_retried(scratch_url, monkeypatch):
    """Audit probe D (docs/audits/2026-10-21-three-pillar-audit.md E13)."""
    real = mig.get_all_ddl()
    broken = [*real[:3], "ALTER TABLE no_such_table ADD COLUMN x integer", *real[3:]]
    monkeypatch.setattr(mig, "get_all_ddl", lambda: broken)
    with pytest.raises(mig.MigrationFailedError, match="1 of"):
        mig.apply_schema(scratch_url)
    recorded = _read(
        scratch_url,
        "SELECT to_regclass('public.schema_version'),"
        " to_regclass('public.schema_meta')",
    )
    assert recorded == [(None, None)]  # neither a version nor a fingerprint
    monkeypatch.setattr(mig, "get_all_ddl", lambda: real)
    assert mig.apply_schema(scratch_url) > 0  # the next run retries everything


@needs_pg
def test_a_database_migrated_before_versioning_opens_and_gets_its_version(scratch_url):
    mig.apply_schema(scratch_url)
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        conn.execute("DROP TABLE schema_version")  # the pre-ADR-1112 state
    from mcp_server.infrastructure.pg_store import PgMemoryStore  # noqa: PLC0415 — PG-only import

    PgMemoryStore(database_url=scratch_url).close()  # legacy current: opens
    assert mig.apply_schema(scratch_url) == 0  # records the version, no DDL
    assert _read(scratch_url, "SELECT version FROM schema_version") == [
        (mig.SCHEMA_VERSION,)
    ]


@needs_pg
def test_two_concurrent_migrations_apply_once(scratch_url):
    results: list[int] = []
    workers = [
        threading.Thread(target=lambda: results.append(mig.apply_schema(scratch_url)))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    assert sorted(results)[0] == 0 and sorted(results)[1] > 0


@needs_pg
def test_a_failed_state_read_never_becomes_a_first_run(monkeypatch):
    """The residual path of increment 0: ``read_schema_hash`` returned None on
    any error, and None meant 'first run, apply DDL'."""
    from mcp_server.infrastructure import pg_store_ddl  # noqa: PLC0415 — PG-only import
    from mcp_server.infrastructure.pg_store import PgMemoryStore  # noqa: PLC0415 — PG-only import

    def broken(conn):
        raise psycopg.errors.InsufficientPrivilege("permission denied for schema_meta")

    migrations: list[object] = []
    monkeypatch.setattr(pg_store_ddl, "read_state", broken)
    monkeypatch.setattr(pg_store_ddl, "migrate_locked", migrations.append)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        PgMemoryStore(database_url=os.environ["DATABASE_URL"])
    assert migrations == []
