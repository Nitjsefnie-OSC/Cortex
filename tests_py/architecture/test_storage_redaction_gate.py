"""ADR-1110: no write path into storage bypasses the redaction gate.

Mechanical proof, by AST over every module of ``mcp_server/``:

1. every raw storage primitive (file creation or write, log file handler,
   temporary file, database connection, child process handed a file as
   stdout/stderr) sits in an audited site below, with the reason it cannot
   carry an unredacted secret; a new primitive anywhere fails the test;
2. database connections are constructed in one place,
   ``infrastructure/db_connect.py``, which only builds gated classes and
   refuses another factory; no module swaps a connection's cursor class, so a
   connection handed across a module boundary is gated wherever it goes;
3. every log file handler gets a ``RedactingFilter`` in the same function.

Runtime behaviour of each gate is pinned in
``tests_py/shared/test_redaction_gate_paths.py``.
"""

from __future__ import annotations

import ast
import collections
import sqlite3

import pytest

from tests_py.architecture._storage_primitives import MCP_SERVER_ROOT, scan_tree

# (path under mcp_server/, primitive kind) -> (count, reason)
_AUDITED = {
    ("shared/gated_files.py", "open-write"): (2, "the gate itself"),
    ("shared/gated_files.py", "write_text"): (
        1,
        "write_json calls the gated write_text",
    ),
    ("shared/gated_files.py", "os.open-write"): (1, "the gate's own temp file"),
    ("shared/private_paths.py", "os.open-write"): (
        1,
        "creates an empty 0600 file before SQLite or a log opens it",
    ),
    ("shared/log_rotation.py", "open-write"): (1, "yields only a RedactingStream"),
    ("infrastructure/log_relay.py", "child-stdio-file"): (
        1,
        "child stdout is the relay's stdin pipe; the relay writes via the gate",
    ),
    ("hooks/capture_worker_logging.py", "os.open-write"): (
        1,
        "pre-creates the worker log 0600, writes nothing",
    ),
    ("hooks/capture_worker_logging.py", "log-file-handler"): (
        1,
        "RedactingFilter attached (checked below)",
    ),
    ("shared/log_file_lock.py", "os.open-write"): (1, "empty lock file"),
    ("infrastructure/hook_counter_lock.py", "os.open-write"): (1, "empty lock file"),
    ("infrastructure/pipeline_graph_ttl.py", "os.open-write"): (1, "empty lock file"),
    ("infrastructure/pipeline_install_lock.py", "os.open-write"): (1, "empty lock"),
    ("infrastructure/capture_spool.py", "os.open-write"): (1, "empty lock file"),
    ("infrastructure/groomer_coordinator_io.py", "os.open-write"): (1, "lock file"),
    ("infrastructure/capture_socket.py", "os.open-write"): (1, "lease lock file"),
    ("infrastructure/pipeline_install_release.py", "write_bytes"): (
        1,
        "release tarball, sha256-verified binary, no user content",
    ),
    ("infrastructure/pipeline_install_rust.py", "tempfile"): (
        1,
        "download staging for a pinned toolchain, no user content",
    ),
    ("infrastructure/db_connect.py", "db-connect"): (
        3,
        "the one connection factory: sqlite3, psycopg and the pool, all gated",
    ),
}


_CURSOR_CLASS_ATTRS = {"cursor_factory", "server_cursor_factory"}
_CURSOR_CLASSES = {"Cursor", "ClientCursor", "ServerCursor", "RawCursor"}


def _name_of(node: ast.AST) -> str:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def test_every_storage_primitive_is_an_audited_site():
    found = collections.Counter((path, kind) for path, _line, kind in scan_tree())
    expected = {key: count for key, (count, _r) in _AUDITED.items() if count}
    unexpected = {k: v for k, v in found.items() if expected.get(k) != v}
    missing = {k: v for k, v in expected.items() if found.get(k) != v}
    assert not unexpected and not missing, (
        "storage primitive outside the redaction gate (ADR-1110): route the "
        "write through mcp_server.shared.gated_files, or audit it here with a "
        f"reason.\nfound but not audited: {unexpected}\naudited but not "
        f"found: {missing}"
    )


def test_connections_come_only_from_the_gated_factory():
    """B1 of the PR #775 review: a hook opened its own psycopg connection,
    allowlisted as read-only, and passed it to a receipt writer. There is no
    ungated connection type left: db_connect is the only constructor, and it
    cannot be asked for another class."""
    from mcp_server.infrastructure import db_connect
    from mcp_server.infrastructure.sqlite_gated_connection import GatedConnection

    conn = db_connect.sqlite_connect(":memory:")
    try:
        assert isinstance(conn, GatedConnection)
    finally:
        conn.close()
    with pytest.raises(TypeError, match="always gated"):
        db_connect.sqlite_connect(":memory:", factory=sqlite3.Connection)
    with pytest.raises(TypeError, match="always gated"):
        db_connect.pg_connect("postgresql://unused", cursor_factory=object)


def test_no_module_swaps_the_cursor_class_of_a_connection():
    allowed = {"infrastructure/pg_gated_cursor.py", "infrastructure/db_connect.py"}
    offenders = []
    for path in sorted(MCP_SERVER_ROOT.rglob("*.py")):
        rel = path.relative_to(MCP_SERVER_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else []
            swaps = any(
                isinstance(t, ast.Attribute) and t.attr in _CURSOR_CLASS_ATTRS
                for t in targets
            )
            builds = (
                isinstance(node, ast.Call) and _name_of(node.func) in _CURSOR_CLASSES
            )
            if (swaps or builds) and rel not in allowed:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders


def test_every_log_file_handler_is_filtered_in_the_same_function():
    handler_files = {p for p, _l, kind in scan_tree() if kind == "log-file-handler"}
    for path in handler_files:
        tree = ast.parse((MCP_SERVER_ROOT / path).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            if "RotatingFileHandler" in names | attrs or "FileHandler" in names:
                assert "RedactingFilter" in names, f"{path}:{fn.name}"


def test_scanner_detects_each_primitive_kind():
    """The proof is only as good as the detector: pin each kind on a sample."""
    from tests_py.architecture._storage_primitives import classify

    samples = {
        "p.write_text(x)": "write_text",
        "open(p, 'a')": "open-write",
        "os.open(p, os.O_CREAT | os.O_WRONLY)": "os.open-write",
        "os.open(p, flags, 0o600)": "os.open-write",
        "logging.FileHandler(p)": "log-file-handler",
        "tempfile.mkstemp()": "tempfile",
        "sqlite3.connect(p)": "db-connect",
        "psycopg.Connection[R].connect(u)": "db-connect",
        "subprocess.Popen(c, stdout=log)": "child-stdio-file",
        "shutil.copyfile(a, b)": "shutil-copy",
    }
    for source, kind in samples.items():
        call = ast.parse(source).body[0].value  # type: ignore[attr-defined]
        assert classify(call) == kind, source
    for harmless in (
        "open(p)",
        "gated_files.write_text(p, t)",
        "os.open(p, os.O_RDONLY)",
    ):
        call = ast.parse(harmless).body[0].value  # type: ignore[attr-defined]
        assert classify(call) is None, harmless
