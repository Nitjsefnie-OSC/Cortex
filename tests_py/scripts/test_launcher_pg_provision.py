"""Setup's managed step records a selection only after the server proved itself.

PR #748's review (2026-10-16, blocker 1): pg0's supervisor wrote state.json
before starting the server, and the selection read "state.json exists" as
"managed", so one failed start (a Mac without Homebrew's OpenSSL) made the
failure permanent. Here a failure leaves no marker and no ``ready`` record:
the store stays SQLite and the next launch is not locked to a dead server.

The archives are faked (a tree whose ``initdb``/``pg_ctl`` are shell scripts);
the supervisor, the locks and the marker are the real ones.

source: ADR-1110
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX only (ADR-1110)")
_INITDB_OK = """#!/bin/sh
for a in "$@"; do
  case "$a" in -D) next=1;; *) [ "$next" = 1 ] && d="$a"; next=;; esac
done
mkdir -p "$d" && echo 18 > "$d/PG_VERSION" && : > "$d/postgresql.conf"
"""  # source: test fake of initdb (creates the files Setup checks)


def _tree(root: Path, *, initdb: str, pg_ctl: str) -> Path:
    tree = root / "fake-tree"
    (tree / "bin").mkdir(parents=True)
    for name, body in (("initdb", initdb), ("pg_ctl", pg_ctl), ("postgres", "")):
        (tree / "bin" / name).write_text(body)
        (tree / "bin" / name).chmod(0o755)
    return tree


@pytest.fixture
def setup_step(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_pg_assets
    import launcher_pg_provision
    from mcp_server.infrastructure import backend_marker

    marker = tmp_path / "methodology" / "backend.json"
    monkeypatch.setattr(backend_marker, "BACKEND_MARKER_PATH", marker)
    monkeypatch.setattr(
        launcher_pg_assets, "host_triple", lambda: "aarch64-apple-darwin"
    )
    for name in ("CORTEX_MEMORY_DB_PATH", "CORTEX_MEMORY_SQLITE_FALLBACK_PATH",
                 "DATABASE_URL", "CLAUDE_PLUGIN_OPTION_DATABASE_URL",
                 "CORTEX_MEMORY_STORE_BACKEND", "CORTEX_BACKEND",
                 "CORTEX_MEMORY_DATABASE_URL"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    said: list[str] = []

    def run(tree: Path) -> str:
        monkeypatch.setattr(launcher_pg_assets, "install", lambda dist, target: tree)
        return launcher_pg_provision.setup_step(str(tmp_path / "data"), said.append)

    return run, marker, tmp_path / "data" / "pg", said


def test_a_server_that_does_not_start_selects_nothing(setup_step, tmp_path):
    run, marker, root, said = setup_step
    tree = _tree(
        tmp_path,
        initdb=_INITDB_OK,
        pg_ctl="#!/bin/sh\necho 'dyld: Library not loaded' >&2\nexit 1\n",
    )
    assert run(tree) == ""  # an eligible store: SQLite stays the fallback
    assert not marker.exists()
    assert json.loads((root / "provisioned.json").read_text())["state"] == "failed"
    assert json.loads((root / "status.json").read_text())["state"] == "failed"
    assert "Library not loaded" in said[-1] and "stay in SQLite" in said[-1]


def test_a_failed_initdb_leaves_no_cluster_and_no_selection(setup_step, tmp_path):
    run, marker, root, said = setup_step
    tree = _tree(
        tmp_path, initdb="#!/bin/sh\necho 'initdb: error' >&2\nexit 1\n", pg_ctl=""
    )
    assert run(tree) == ""
    assert not marker.exists()
    assert not (root / "cluster").exists()
    assert "initdb exited 1" in said[-1]


def test_a_store_already_managed_fails_setup_when_its_server_does_not_start(
    setup_step, tmp_path
):
    run, marker, root, _said = setup_step
    from mcp_server.infrastructure.backend_marker import write_backend_marker

    write_backend_marker("managed", "t", marker, root=str(root))
    tree = _tree(tmp_path, initdb=_INITDB_OK, pg_ctl="#!/bin/sh\nexit 1\n")
    assert "did not start" in run(tree)
    assert json.loads(marker.read_text())["backend"] == "managed"  # untouched


def test_an_unsupported_host_keeps_sqlite_and_writes_nothing(setup_step, monkeypatch):
    run, marker, root, said = setup_step
    import launcher_pg_assets

    def unsupported():
        raise launcher_pg_assets.UnsupportedError("native Windows")

    monkeypatch.setattr(launcher_pg_assets, "host_triple", unsupported)
    assert run(Path("/nowhere")) == ""
    assert not marker.exists() and not root.exists()
    assert "native Windows" in said[-1]


def test_after_a_failed_setup_the_store_stays_eligible_for_the_next_one(
    setup_step, tmp_path
):
    run, marker, _root, _said = setup_step
    tree = _tree(tmp_path, initdb=_INITDB_OK, pg_ctl="#!/bin/sh\nexit 1\n")
    run(tree)
    import launcher_backend
    import launcher_pg_select

    assert launcher_pg_select.why_not_eligible(os.environ, None, marker) == ""
    assert launcher_backend.decide(marker.parent, marker.name)[0] == "sqlite"


def test_a_launch_that_chose_sqlite_during_provisioning_is_not_overwritten(
    setup_step, tmp_path, monkeypatch
):
    run, marker, _root, said = setup_step
    import launcher_backend
    import launcher_pg_provision
    from mcp_server.infrastructure.backend_marker import write_backend_marker

    store = tmp_path / "memory.db"
    monkeypatch.setenv("CORTEX_MEMORY_DB_PATH", str(store))

    def first_launch_meanwhile(paths):
        write_backend_marker("sqlite", launcher_backend.WRITTEN_BY, marker)
        store.write_text("memories")

    monkeypatch.setattr(launcher_pg_provision, "provision", first_launch_meanwhile)
    assert run(Path("/unused")) == ""
    assert json.loads(marker.read_text())["backend"] == "sqlite"
    assert "started but not selected" in said[-1]
