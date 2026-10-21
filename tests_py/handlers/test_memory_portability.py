"""Per-project export and import of memories (audit A8, GDPR art. 20; ADR-1113).

The audit found no export of a project's memories (``grep -rliE 'def .*export'
mcp_server/handlers`` empty). Each test runs on SQLite (temporary file) and on
PostgreSQL (the suite's private database, when reachable).
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from mcp_server.handlers import memory_portability as portability
from mcp_server.infrastructure.sqlite_store import SqliteMemoryStore
from scripts.secret_corpus import samples
from tests_py.conftest import _USE_PG  # type: ignore  # noqa: E402

RUN = uuid.uuid4().hex[:10]
PROJECT = f"/work/{RUN}/app"
SECRET = samples()["github-pat"][1]


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteMemoryStore(str(tmp_path / "memory.db"))
    else:
        if not _USE_PG:
            pytest.skip("PostgreSQL test database not reachable")
        from mcp_server.infrastructure.pg_store import PgMemoryStore

        s = PgMemoryStore()
    yield s
    s.close()


def _seed(store) -> None:
    rows = [
        (f"decision {RUN}: use SQLite by default", PROJECT, False),
        (f"note {RUN} in a sub-directory", f"{PROJECT}/src/api", False),
        (f"sibling {RUN} must not leak", f"/work/{RUN}/app-other", False),
        (f"parent {RUN} is another project", f"/work/{RUN}", False),
        (f"global {RUN} convention", "", True),
    ]
    for content, directory, is_global in rows:
        store.insert_memory(
            {
                "content": content,
                "tags": ["t"],
                "domain": "d",
                "directory_context": directory,
                "is_global": is_global,
            }
        )


def _contents(path: Path) -> list[str]:
    _header, records = portability.read_export(path)
    return sorted(r["content"] for r in records)


def test_export_holds_the_project_and_its_subdirectories_only(store, tmp_path):
    _seed(store)
    out = tmp_path / "export.jsonl"
    header = portability.export_project(store, PROJECT, out)
    assert header["count"] == 2
    assert _contents(out) == sorted(
        [f"decision {RUN}: use SQLite by default", f"note {RUN} in a sub-directory"]
    )


def test_global_memories_are_included_only_when_asked(store, tmp_path):
    _seed(store)
    out = tmp_path / "export.jsonl"
    portability.export_project(store, PROJECT, out, include_global=True)
    assert f"global {RUN} convention" in _contents(out)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_export_file_is_owner_only_and_self_describing(store, tmp_path):
    _seed(store)
    out = tmp_path / "export.jsonl"
    portability.export_project(store, PROJECT, out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    header = json.loads(out.read_text().splitlines()[0])
    assert header["format"] == "cortex-memory-export" and header["version"] == 1
    assert header["fields"] == list(portability.EXPORT_FIELDS)


def test_a_secret_stored_before_the_gate_leaves_the_store_redacted(store, tmp_path):
    """Rows written before ADR-1110 can hold a secret; the export never does."""
    mid = store.insert_memory(
        {
            "content": "placeholder",
            "tags": [],
            "domain": "d",
            "directory_context": PROJECT,
        }
    )
    raw_update = "UPDATE memories SET content = %s WHERE id = %s"
    # an ungated connection stands in for a row written before the gate
    if isinstance(store, SqliteMemoryStore):
        with sqlite3.connect(store._connection_registry._target) as raw:
            raw.execute(raw_update.replace("%s", "?"), (f"token {SECRET}", mid))
    else:
        import psycopg

        with psycopg.connect(store._url, autocommit=True) as raw:
            raw.execute(raw_update, (f"token {SECRET}", mid))
    out = tmp_path / "export.jsonl"
    portability.export_project(store, PROJECT, out)
    assert SECRET not in out.read_text()
    assert "[REDACTED:github-pat]" in out.read_text()


def test_round_trip_into_another_project_and_twice_adds_nothing(store, tmp_path):
    _seed(store)
    out = tmp_path / "export.jsonl"
    portability.export_project(store, PROJECT, out)
    target = f"/elsewhere/{RUN}/app"
    first = portability.import_export(store, out, into_project_root=target)
    again = portability.import_export(store, out, into_project_root=target)
    assert first == {"imported": 2, "skipped": 0}
    assert again == {"imported": 0, "skipped": 2}
    moved = tmp_path / "moved.jsonl"
    portability.export_project(store, target, moved)
    assert _contents(moved) == _contents(out)
    _header, records = portability.read_export(moved)
    assert {r["directory_context"] for r in records} == {target, f"{target}/src/api"}


def test_a_file_that_is_not_an_export_is_refused(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"format": "something-else"}\n')
    with pytest.raises(ValueError, match="not a cortex-memory-export"):
        portability.read_export(bad)
    truncated = tmp_path / "truncated.jsonl"
    header = {
        "format": "cortex-memory-export",
        "version": 1,
        "count": 3,
        "fields": list(portability.EXPORT_FIELDS),
        "project_root": "/p",
    }
    truncated.write_text(json.dumps(header) + "\n")
    with pytest.raises(ValueError, match="header says 3"):
        portability.read_export(truncated)


def _cli(db: Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``python -m mcp_server.portability`` against the SQLite file ``db``."""
    env = {
        **os.environ,
        "CORTEX_MEMORY_STORE_BACKEND": "sqlite",
        "CORTEX_MEMORY_DB_PATH": str(db),
        "CORTEX_MEMORY_SQLITE_FALLBACK_PATH": str(db),
        # offline: the embedding model is already cached or falls back
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "mcp_server.portability", *args],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=300,  # source: ADR-1113 — bound for a cold model load
    )


def test_the_command_line_exports_and_imports(tmp_path):
    """End to end through ``python -m mcp_server.portability`` on SQLite."""
    db = tmp_path / "cli.db"
    store = SqliteMemoryStore(str(db))
    _seed(store)
    store.close()
    out = tmp_path / "cli.jsonl"
    exported = _cli(db, "export", PROJECT, str(out))
    assert exported.returncode == 0, exported.stderr
    assert json.loads(exported.stdout)["count"] == 2
    imported = _cli(db, "import", str(out), "--into", f"/copy/{RUN}")
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout) == {"imported": 2, "skipped": 0}
    refused = _cli(db, "import", str(tmp_path / "missing.jsonl"))
    assert refused.returncode == 1
    assert "cortex portability:" in refused.stderr
