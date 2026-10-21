"""A hard ``forget`` erases every copy of the memory (audit 2026-10-21, E15).

The audit found the hard-delete path leaving the full original text in
``memory_archives``, while PRIVACY.md promised removal "across every
substrate". Each test below writes one unique marker through the real write
paths that copy a memory's text (the memory row and its FTS/vector index, a
compression archive, wiki claim, draft, citation and compiled page, a
prospective trigger and a rule mined from it, an extracted entity, a promoted
wiki page file), hard-forgets the memory through the ``forget`` handler, then
searches the whole database (every text column of every table, found in the
catalog at run time) and the wiki tree for the marker. On SQLite the database
file itself (and its WAL) is searched byte by byte.

The fixture uses only APIs that exist on ``main``, so the same tests show the
pre-fix behaviour (they fail there).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from mcp_server.infrastructure.sqlite_store import SqliteMemoryStore
from tests_py._db_text_scan import locations
from tests_py.conftest import _USE_PG  # type: ignore  # noqa: E402

# unique per run: a copy left in a shared test database by an earlier run (or
# by the pre-fix code) can never satisfy or break this run's assertions
RUN = uuid.uuid4().hex[:12]
MARKER = f"ERASE-ME-{RUN} personal record of Jane Placeholder{RUN}"
KEEP = f"KEEP-ME-{RUN} unrelated memory"
DOMAIN = "forget-erasure-test"


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path, monkeypatch):
    from mcp_server.handlers import forget

    if request.param == "sqlite":
        s = SqliteMemoryStore(str(tmp_path / "memory.db"))
    else:
        if not _USE_PG:
            pytest.skip("PostgreSQL test database not reachable")
        from mcp_server.infrastructure.pg_store import PgMemoryStore

        s = PgMemoryStore()
    monkeypatch.setattr(forget, "_store", s)
    yield s
    s.close()


def _seed(store, wiki_root: Path) -> int:
    from mcp_server.infrastructure.pg_store_wiki_claims import insert_claim_events
    from mcp_server.infrastructure.pg_store_wiki_drafts import insert_draft
    from mcp_server.infrastructure.pg_store_wiki_notes import insert_citation
    from mcp_server.infrastructure.pg_store_wiki_pages import upsert_page

    mid = store.insert_memory({"content": MARKER, "tags": ["t"], "domain": DOMAIN})
    keep = store.insert_memory({"content": KEEP, "tags": ["t"], "domain": DOMAIN})
    # compression: the original goes to the archive, the row keeps a summary
    store.insert_archive(
        {
            "original_memory_id": mid,
            "content": MARKER,
            "embedding": None,
            "archive_reason": "compression",
        }
    )
    store.update_memory_compression(mid, "summary", None, 1, original_content=MARKER)
    entity = store.insert_entity(
        {"name": f"Jane Placeholder{RUN}", "type": "person", "domain": DOMAIN}
    )
    store.insert_memory_entity(mid, entity)
    shared = store.insert_entity(
        {"name": f"SharedConcept{RUN}", "type": "concept", "domain": DOMAIN}
    )
    store.insert_memory_entity(mid, shared)
    store.insert_memory_entity(keep, shared)
    symbol = store.insert_entity(
        {
            "name": f"parse_config{RUN}",
            "type": "function",
            "domain": DOMAIN,
            "origin": "ast_symbol",
        }
    )
    store.insert_memory_entity(mid, symbol)
    store.insert_prospective_memory(
        {
            "content": f"remind: {MARKER}",
            "trigger_condition": "deploy",
            "trigger_type": "keyword",
            "source_memory_id": mid,
        }
    )
    store.insert_rule(
        {
            "condition": f"content contains {MARKER}",
            "action": "boost:0.1",
            "source_memory_id": mid,
        }
    )
    conn = store._conn
    insert_claim_events(conn, [{"memory_id": mid, "text": f"claim: {MARKER}"}])
    insert_draft(
        conn,
        {"memory_id": mid, "title": f"draft {MARKER}", "kind": "note", "lead": MARKER},
    )
    rel = f"notes/{DOMAIN}/{mid}-erase-me.md"
    page_id, _ = upsert_page(
        conn,
        {
            "rel_path": rel,
            "slug": "erase-me",
            "kind": "note",
            "title": f"page {MARKER}",
            "body": MARKER,
            "memory_id": mid,
            "domain": DOMAIN,
        },
    )
    insert_citation(conn, page_id, domain=DOMAIN, memory_id=mid)
    conn.commit()
    page = wiki_root / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        f"---\nmemory_id: {mid}\n---\n# page\n\n{MARKER}\n", encoding="utf-8"
    )
    return mid


def _forget(mid: int) -> dict:
    from mcp_server.handlers.forget import handler

    return asyncio.run(handler({"memory_id": mid, "force": True}))


def test_hard_forget_leaves_the_text_in_no_table(store, tmp_path, monkeypatch):
    from mcp_server.handlers import forget

    monkeypatch.setattr(forget, "WIKI_ROOT", tmp_path / "wiki", raising=False)
    mid = _seed(store, tmp_path / "wiki")
    assert locations(store, MARKER) != []  # the fixture really wrote copies
    result = _forget(mid)
    assert result["deleted"] is True and result["method"] == "hard"
    assert locations(store, MARKER) == []
    assert locations(store, f"Jane Placeholder{RUN}") == []  # orphaned extracted entity
    # what was not this memory's survives
    assert locations(store, KEEP) != []
    assert locations(store, f"SharedConcept{RUN}") != []
    assert locations(store, f"parse_config{RUN}") != []  # a code symbol is kept


def test_hard_forget_removes_the_page_files_derived_from_the_memory(
    store, tmp_path, monkeypatch
):
    from mcp_server.handlers import forget

    monkeypatch.setattr(forget, "WIKI_ROOT", tmp_path / "wiki", raising=False)
    mid = _seed(store, tmp_path / "wiki")
    _forget(mid)
    for path in (tmp_path / "wiki").rglob("*"):
        if path.is_file():
            assert MARKER not in path.read_text(encoding="utf-8", errors="replace"), (
                path
            )


def test_sqlite_file_and_wal_hold_no_byte_of_the_text(tmp_path, monkeypatch):
    from mcp_server.handlers import forget

    store = SqliteMemoryStore(str(tmp_path / "memory.db"))
    monkeypatch.setattr(forget, "_store", store)
    monkeypatch.setattr(forget, "WIKI_ROOT", tmp_path / "wiki", raising=False)
    try:
        mid = _seed(store, tmp_path / "wiki")
        on_disk = b"".join(
            (tmp_path / n).read_bytes()
            for n in ("memory.db", "memory.db-wal")
            if (tmp_path / n).exists()
        )
        assert f"ERASE-ME-{RUN}".encode() in on_disk  # the fixture really wrote bytes
        _forget(mid)
        for name in ("memory.db", "memory.db-wal"):
            path = tmp_path / name
            if path.exists():
                assert f"ERASE-ME-{RUN}".encode() not in path.read_bytes(), name
    finally:
        store.close()


def test_soft_forget_keeps_the_content_by_design(store, tmp_path, monkeypatch):
    from mcp_server.handlers.forget import handler

    mid = store.insert_memory({"content": MARKER, "tags": [], "domain": DOMAIN})
    result = asyncio.run(handler({"memory_id": mid, "soft": True}))
    assert result["method"] == "soft"
    assert locations(store, MARKER) != []


def test_a_failing_step_erases_nothing_and_raises(store, tmp_path, monkeypatch):
    """No partial erasure: the old handler logged a failed claim deletion and
    went on deleting the row; now any failing statement rolls back all."""
    from mcp_server.handlers import forget
    from mcp_server.infrastructure import memory_erasure

    monkeypatch.setattr(forget, "WIKI_ROOT", tmp_path / "wiki", raising=False)
    mid = _seed(store, tmp_path / "wiki")
    broken = (
        *memory_erasure.DERIVED_ROWS,
        ("boom", "DELETE FROM no_such_table WHERE id = %s"),
    )
    monkeypatch.setattr(memory_erasure, "DERIVED_ROWS", broken)
    with pytest.raises(Exception, match="no_such_table"):
        _forget(mid)
    assert store.get_memory(mid) is not None
    assert ("memory_archives", "content") in [
        (t.split(".")[-1], c) for t, c in locations(store, MARKER)
    ]
