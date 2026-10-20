"""DDL/schema-migration + query-execution mixin for PgMemoryStore.

``compute_ddl_hash`` / ``read_schema_hash`` / ``_get_database_url`` are
module-level functions re-exported by ``pg_store.py`` — ``mcp_server.migrate``
(a standalone entry point that must decide "is the DB current" without
constructing a full store) imports them from that facade path.

source: ADR-0545"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any, cast

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from mcp_server.infrastructure.pg_schema import get_all_ddl
from mcp_server.infrastructure.pg_store_host import MaterializedCursor, PgStoreHost
from mcp_server.infrastructure.memory_config import get_memory_settings

if TYPE_CHECKING:
    from typing_extensions import LiteralString


# source: ADR-0545
logger = logging.getLogger("mcp_server.infrastructure.pg_store")


def compute_ddl_hash() -> str:
    """SHA-256 fingerprint of the code's full ordered DDL set.

    source: ADR-0545"""
    return hashlib.sha256("\n".join(get_all_ddl()).encode("utf-8")).hexdigest()


def read_schema_hash(conn: psycopg.Connection[DictRow]) -> str | None:
    """Read the recorded DDL hash from ``schema_meta`` on ``conn``, or None.

    source: ADR-0545"""
    try:
        row = conn.execute("SELECT ddl_hash FROM schema_meta WHERE id = 1;").fetchone()
        return row["ddl_hash"] if row else None
    except psycopg.Error:
        return None


def _get_database_url() -> str:
    """Get DATABASE_URL from environment or MemorySettings default.

    source: ADR-0545"""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url or "${" in url:
        url = get_memory_settings().DATABASE_URL
    return url


# source: ADR-0545
_SCHEMA_LOCK_ID = 1357020271

# source: ADR-0545
_SCHEMA_META_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_meta ("
    " id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),"
    " ddl_hash text NOT NULL,"
    " applied_at timestamptz NOT NULL DEFAULT now());"
)


def _record_schema_hash(conn: psycopg.Connection[DictRow], ddl_hash: str) -> None:
    """Persist the just-applied DDL revision (upsert the single row).

    source: ADR-0545"""
    conn.execute(_SCHEMA_META_DDL)
    conn.execute(
        "INSERT INTO schema_meta (id, ddl_hash, applied_at)"
        " VALUES (1, %s, now())"
        " ON CONFLICT (id) DO UPDATE SET"
        " ddl_hash = EXCLUDED.ddl_hash, applied_at = EXCLUDED.applied_at;",
        (ddl_hash,),
    )


def _apply_ddl_locked(
    conn: psycopg.Connection[DictRow], ddl_list: list[LiteralString], ddl_hash: str
) -> bool:
    """Apply ``ddl_list`` under the schema advisory lock; True iff applied.

    A peer that applied the same revision while this one waited makes it a
    no-op. The lock is released even on failure.

    source: ADR-0545"""
    conn.execute("SELECT pg_advisory_lock(%s);", (_SCHEMA_LOCK_ID,))
    try:
        if read_schema_hash(conn) == ddl_hash:
            return False
        for ddl in ddl_list:
            try:
                conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001 — last-resort boundary — failure is logged; degraded mode continues
                logger.warning(
                    "Schema statement failed: %s — %s", ddl.split("\n")[0][:50], exc
                )
        _record_schema_hash(conn, ddl_hash)
        conn.commit()
        return True
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s);", (_SCHEMA_LOCK_ID,))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — last-resort boundary — failure is logged; degraded mode continues
            logger.warning("Failed to release schema advisory lock: %s", exc)


def apply_schema(database_url: str) -> bool:
    """The explicit migration step: bring ``database_url`` to the code's
    schema revision; True iff DDL ran, False when already current.

    The only DDL path for a database that already has a recorded revision
    (``python -m mcp_server.migrate``, ``scripts/setup_db.py``, the test
    harness); opening a store never calls it (ADR-1112).

    source: ADR-1112"""
    ddl_hash = compute_ddl_hash()
    with psycopg.Connection[DictRow].connect(
        database_url, row_factory=dict_row, autocommit=True
    ) as conn:
        if read_schema_hash(conn) == ddl_hash:
            return False
        return _apply_ddl_locked(conn, get_all_ddl(), ddl_hash)


class PgDdlMixin(PgStoreHost):
    """Pooled query execution + DDL/schema migration on PostgreSQL."""

    def _execute(
        self, query: str | sql.Composable, params: Any = None, **kwargs: Any
    ) -> MaterializedCursor:
        """Execute a query with stale-plan recovery and reconnection.

                When ``POOL_DISABLED`` is set (kill switch), falls back to the
                persistent ``_conn`` — pre-Phase-5 behavior.

        source: ADR-0545"""

        if get_memory_settings().POOL_DISABLED:
            return self._execute_on_conn(self._conn, query, params, **kwargs)

        with self.interactive_pool.connection() as conn:
            return self._execute_on_conn(conn, query, params, **kwargs)

    def _execute_on_conn(
        self,
        conn: psycopg.Connection[DictRow],
        query: str | sql.Composable,
        params: Any,
        **kwargs: Any,
    ) -> MaterializedCursor:
        """Run a query on a given connection with retry-on-stale-plan.

        Returns a materialized cursor (rows pre-fetched) so callers can
        keep using .fetchone() / .fetchall() after the connection is
        returned to the pool.
        """
        # source: ADR-0545
        typed_query = cast("LiteralString | sql.SQL | sql.Composed", query)
        try:
            cur = conn.execute(typed_query, params, **kwargs)
        except psycopg.errors.FeatureNotSupported:
            logger.info("Stale prepared plan detected, deallocating and retrying")
            try:
                conn.rollback()
            except Exception as exc:  # noqa: BLE001 — recovery continues to the retry below
                logger.debug("rollback during stale-plan recovery failed: %s", exc)
            try:
                conn.execute("DEALLOCATE ALL")
            except Exception as exc:  # noqa: BLE001 — recovery continues to the retry below
                logger.debug(
                    "DEALLOCATE ALL during stale-plan recovery failed: %s", exc
                )
            cur = conn.execute(typed_query, params, **kwargs)
        except psycopg.OperationalError:
            logger.warning("Database connection lost on pool checkout, retrying")
            cur = conn.execute(typed_query, params, **kwargs)
        return MaterializedCursor(cur)

    def _init_schema(self) -> None:
        """Read the recorded schema revision; run DDL only on first run.

        A process that opens a store never migrates an existing schema:
        ``ALTER TABLE ... IF NOT EXISTS`` takes its AccessExclusive lock
        before it checks anything, so a runtime no-op migration queues
        behind any open transaction and every later read queues behind it
        (incident 2026-10-20, ADR-1112). Schema changes go through the
        explicit step, ``apply_schema`` (``python -m mcp_server.migrate``).

        Pre: self._conn is a live psycopg connection (autocommit).
        Post: on a database with no recorded revision (first run, no
        readers yet) the schema is at the code's revision; on a database
        recorded at another revision nothing is executed and a warning
        names the explicit step.

        source: ADR-1112"""
        ddl_hash = compute_ddl_hash()
        recorded = read_schema_hash(self._conn)
        if recorded == ddl_hash:
            return
        if recorded is None:
            _apply_ddl_locked(self._conn, get_all_ddl(), ddl_hash)
            return
        logger.warning(
            "PostgreSQL schema is recorded at revision %s, this code expects %s; "
            "no DDL runs when a store opens (ADR-1112). Run "
            "'python -m mcp_server.migrate' once to migrate, or check whether "
            "another Cortex install on a different revision shares this database.",
            recorded[:16],
            ddl_hash[:16],
        )

    @property
    def has_vec(self) -> bool:
        """Always true — pgvector is mandatory.

        source: ADR-0545"""
        return True
