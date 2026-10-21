"""Where a client finds its store broker, and how it launches one.

One broker per resolved store: the key is a digest of the database URL, so
two stores on one machine get two brokers and one store never gets two. On
Unix the broker's private directory is ``<Cortex root>/.store-broker/<key>``
(0700, socket 0600, peer UID checked, ``broker_unix``); on Windows the
endpoint is a named pipe restricted to the user's SID (``broker_pipe``).

The URL travels to the spawned broker in its environment
(``CORTEX_BROKER_DATABASE_URL``), never on its command line, where any
local user could read it in the process table (ADR-1112, threat model,
information disclosure).

The launch itself needs the plugin launcher (deps isolation, issue #667),
which lives in ``hooks``; it is injected by the composition root
(``hooks.wiring``) through ``configure_broker_launcher``.

source: ADR-1112 decisions 1, 2 and owner decision 2
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp_server.infrastructure.broker_client import BrokerClient, BrokerStore

# source: ADR-1112 the child reads its store from here, not from argv
URL_ENVIRONMENT = "CORTEX_BROKER_DATABASE_URL"
# source: ADR-1112 hex digits of the store key (64 bits: no collision in practice)
_KEY_LENGTH = 16

# (module, arguments, environment additions, inherited fds) -> started
Launcher = Callable[[str, list[str], dict[str, str], tuple[int, ...]], None]
_launcher: Launcher | None = None


class BrokerLaunchError(RuntimeError):
    """No broker answered and none could be started; never a direct connection."""


def configure_broker_launcher(launcher: Launcher) -> None:
    global _launcher
    _launcher = launcher


def store_key(database_url: str) -> str:
    return hashlib.sha256(database_url.encode("utf-8")).hexdigest()[:_KEY_LENGTH]


def _launch(arguments: list[str], url: str, fds: tuple[int, ...]) -> None:
    if _launcher is None:
        raise BrokerLaunchError(
            "no store broker launcher configured; the process entry point must "
            "call hooks.wiring.wire_composition_root()"
        )
    _launcher("mcp_server.hooks.store_broker", arguments, {URL_ENVIRONMENT: url}, fds)


def _unix_connect(root: Path, url: str) -> Callable[[float], Any]:
    from mcp_server.infrastructure import broker_unix  # noqa: PLC0415 — Unix transport only

    key = store_key(url)

    def spawn(listener: Any, lease: int) -> None:
        arguments = ["--listener-fd", str(listener.fileno()), "--lease-fd", str(lease)]
        _launch(arguments, url, (listener.fileno(), lease))

    def connect(deadline: float) -> Any:
        return broker_unix.connect_or_spawn(root, key, deadline, spawn)

    return connect


def _pipe_connect(url: str) -> Callable[[float], Any]:
    from mcp_server.infrastructure import broker_pipe  # noqa: PLC0415 — Windows transport only

    name = broker_pipe.pipe_name(store_key(url))

    def connect(deadline: float) -> Any:
        return broker_pipe.connect_or_spawn(
            name, deadline, lambda: _launch(["--pipe-name", name], url, ())
        )

    return connect


def broker_store(database_url: str, root: Path, client_kind: str) -> BrokerStore:
    """A store for ``database_url`` served by its broker (launched on demand)."""
    from mcp_server.infrastructure.broker_executor import allowed_methods  # noqa: PLC0415 — needs the PG store class
    from mcp_server.infrastructure.pg_store import PgMemoryStore  # noqa: PLC0415 — same

    if sys.platform == "win32":
        connect = _pipe_connect(database_url)
    else:
        connect = _unix_connect(root, database_url)
    client = BrokerClient(connect, client_kind)
    return BrokerStore(client, allowed_methods(PgMemoryStore))
