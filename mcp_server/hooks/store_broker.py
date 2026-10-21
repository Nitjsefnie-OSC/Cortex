"""The store broker process: the only Cortex code that connects to PostgreSQL.

Started by the first client that finds no broker for its store (see
``infrastructure.broker_endpoint``): on Unix with the bound listener and
the kernel lease inherited (``--listener-fd``, ``--lease-fd``, the capture
worker's lifecycle, ADR-0512); on Windows with the pipe name. The database
URL comes from the environment, never the command line.

At start, before accepting anything, the broker applies pending migrations
once under the schema advisory lock (ADR-1112 decision 5); a schema it
cannot use stops it with the reason in its log, and every waiting client
gets an explicit error. It then serves until idle (no connection, no work)
for ``CORTEX_BROKER_IDLE_SECONDS``.

source: ADR-1112
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

from mcp_server.hooks.store_broker_policy import server_policy
from mcp_server.infrastructure.broker_endpoint import URL_ENVIRONMENT, store_key
from mcp_server.infrastructure.broker_executor import StoreExecutor
from mcp_server.infrastructure.broker_server import BrokerServer, Identity
from mcp_server.infrastructure.pg_migrations import apply_schema
from mcp_server.infrastructure.schema_versions import SCHEMA_VERSION, code_revision

logger = logging.getLogger("cortex.store_broker")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listener-fd", type=int)
    parser.add_argument("--lease-fd", type=int)
    parser.add_argument("--pipe-name")
    return parser.parse_args()


def _open_store(url: str):
    from mcp_server.infrastructure.pg_store import PgMemoryStore  # noqa: PLC0415 — the broker is the one process that loads the driver's store

    applied = apply_schema(url)
    logger.info(
        "schema version %s ready (%s statements applied)", SCHEMA_VERSION, applied
    )
    return PgMemoryStore(database_url=url)


def _serve_unix(args: argparse.Namespace, url: str, runtime: Path) -> None:
    from mcp_server.infrastructure import broker_unix  # noqa: PLC0415 — Unix transport only
    from mcp_server.infrastructure.capture_socket import remove_socket  # noqa: PLC0415 — same

    listener = socket.socket(fileno=args.listener_fd)
    path = Path(listener.getsockname())
    if path != runtime / broker_unix.SOCKET_NAME:
        raise ValueError(
            "inherited broker listener does not match the store's directory"
        )
    listener.settimeout(
        1.0
    )  # source: ADR-1112 the accept loop re-checks shutdown each second
    try:
        store = _open_store(url)
        try:
            server = BrokerServer(
                lambda: broker_unix.accept(listener),
                StoreExecutor(store),
                server_policy(),
                Identity(code_revision(), SCHEMA_VERSION),
                broker_unix.peer_label,
            )
            server.run(listener.close)
        finally:
            store.close()
    finally:
        listener.close()
        remove_socket(path)
        os.close(args.lease_fd)


def _serve_pipe(args: argparse.Namespace, url: str) -> None:
    from mcp_server.infrastructure import broker_pipe  # noqa: PLC0415 — Windows transport only

    listener = broker_pipe.PipeListener(args.pipe_name)
    try:
        store = _open_store(url)
        try:
            server = BrokerServer(
                listener.accept,
                StoreExecutor(store),
                server_policy(),
                Identity(code_revision(), SCHEMA_VERSION),
            )
            server.run(listener.close)
        finally:
            store.close()
    finally:
        listener.close()


def main() -> None:
    args = _arguments()
    url = os.environ.pop(URL_ENVIRONMENT, "")
    if not url:
        raise SystemExit(f"store broker: {URL_ENVIRONMENT} is not set")
    from mcp_server.hooks.store_broker_policy import configure_logging  # noqa: PLC0415 — composition root binds diagnostics
    from mcp_server.infrastructure.config import CLAUDE_DIR  # noqa: PLC0415 — composition resolves the configured root

    if sys.platform == "win32":
        configure_logging(CLAUDE_DIR / ".store-broker", store_key(url))
        _serve_pipe(args, url)
        return
    from mcp_server.infrastructure.broker_unix import runtime_directory  # noqa: PLC0415 — Unix transport only

    runtime = runtime_directory(CLAUDE_DIR, store_key(url))
    configure_logging(runtime, "broker")
    _serve_unix(args, url, runtime)


if __name__ == "__main__":
    from mcp_server.hooks.wiring import wire_composition_root  # noqa: PLC0415 — source: issue #560

    wire_composition_root()
    try:
        main()
    except Exception:
        logger.exception("store broker stopped")
        raise
