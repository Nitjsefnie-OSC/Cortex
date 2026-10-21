"""The store broker as a real process on the test database (ADR-1112
increment 3): first spawn, reuse, concurrent launch, crash and relaunch
over a stale socket, idle exit. Clients use a launcher that runs the broker
module with this interpreter (the plugin launcher is exercised by install-e2e).

source: ADR-1112
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from mcp_server.infrastructure import broker_endpoint
from mcp_server.infrastructure.broker_client import BrokerUnavailableError
from mcp_server.infrastructure.broker_unix import SOCKET_NAME, runtime_directory
from tests_py.conftest import _USE_PG

pytestmark = [
    pytest.mark.skipif(not _USE_PG, reason="requires isolated PostgreSQL"),
    pytest.mark.skipif(
        sys.platform == "win32", reason="Unix transport; the pipe has its own job"
    ),
]

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def broker_env(monkeypatch):
    # sockaddr_un holds about 104 bytes on macOS: a short root under /tmp
    root = Path(tempfile.mkdtemp(prefix="sb-", dir=Path("/tmp").resolve()))
    children: list[subprocess.Popen] = []

    def launcher(module, arguments, environment, fds):
        env = {**os.environ, **environment, "CORTEX_CLAUDE_DIR": str(root)}
        env["CORTEX_BROKER_IDLE_SECONDS"] = "2"
        children.append(
            subprocess.Popen(
                [sys.executable, "-m", module, *arguments],
                pass_fds=fds,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                cwd=_REPO,
                env=env,
            )
        )

    monkeypatch.setattr(broker_endpoint, "_launcher", launcher)
    yield root, children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    subprocess.run(["rm", "-rf", str(root)], check=False)


def _store(root: Path):
    return broker_endpoint.broker_store(os.environ["DATABASE_URL"], root, "test")


def test_first_spawn_then_reuse_by_a_second_client(broker_env):
    root, children = broker_env
    first, second = _store(root), _store(root)
    assert first.get_memory(-1) is None
    assert second.get_memory(-1) is None
    assert len(children) == 1
    assert first.broker_pid() == second.broker_pid() == children[0].pid


def test_concurrent_first_requests_start_one_broker(broker_env):
    root, children = broker_env
    pids: list[int] = []

    def client():
        store = _store(root)
        store.get_memory(-1)
        pids.append(store.broker_pid())

    threads = [threading.Thread(target=client) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(pids) == 8 and len(set(pids)) == 1
    assert len(children) == 1


def test_a_crashed_broker_is_relaunched_and_no_write_is_replayed(broker_env):
    root, children = broker_env
    store = _store(root)
    store.get_memory(-1)
    os.kill(children[0].pid, signal.SIGKILL)
    children[0].wait(timeout=10)
    key = broker_endpoint.store_key(os.environ["DATABASE_URL"])
    assert (runtime_directory(root, key) / SOCKET_NAME).exists()  # stale
    with pytest.raises(BrokerUnavailableError):
        store.get_memory(-1)  # the in-flight channel broke: reported, not retried
    assert store.get_memory(-1) is None  # next request: one new broker
    assert len(children) == 2 and store.broker_pid() == children[1].pid


def test_the_broker_exits_when_idle_and_removes_its_socket(broker_env):
    root, children = broker_env
    store = _store(root)
    store.get_memory(-1)
    store.close()
    assert children[0].wait(timeout=20) == 0
    key = broker_endpoint.store_key(os.environ["DATABASE_URL"])
    assert not (runtime_directory(root, key) / SOCKET_NAME).exists()


def test_the_database_url_is_not_on_the_command_line(broker_env):
    root, children = broker_env
    _store(root).get_memory(-1)
    command = subprocess.run(
        ["ps", "-o", "command=", "-p", str(children[0].pid)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "postgresql://" not in command and "--listener-fd" in command
