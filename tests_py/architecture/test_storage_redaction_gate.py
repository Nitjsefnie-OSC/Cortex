"""ADR-1110: no write path into storage bypasses the redaction gate.

Mechanical proof, by AST over every module of ``mcp_server/``:

1. every raw storage primitive (file creation or write, log file handler,
   temporary file, database connection, child process handed a file as
   stdout/stderr) sits in an audited site below, with the reason it cannot
   carry an unredacted secret; a new primitive anywhere fails the test;
2. database connections that can write are gated by construction
   (``factory=GatedConnection`` / ``cursor_factory=GatedCursor``); those
   listed as read-only or schema-only are checked to contain no SQL write
   verb in any string literal;
3. every log file handler gets a ``RedactingFilter`` in the same function.

Runtime behaviour of each gate is pinned in
``tests_py/shared/test_redaction_gate_paths.py``.
"""

from __future__ import annotations

import ast
import collections
import re

from tests_py.architecture._storage_primitives import MCP_SERVER_ROOT, scan_tree

# (path under mcp_server/, primitive kind) -> (count, reason)
_AUDITED = {
    ("shared/gated_files.py", "open-write"): (2, "the gate itself"),
    ("shared/gated_files.py", "write_text"): (
        1,
        "write_json calls the gated write_text",
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
    ("infrastructure/sqlite_connection_registry.py", "db-connect"): (
        1,
        "factory=GatedConnection",
    ),
    ("infrastructure/pg_store_schema.py", "db-connect"): (
        3,
        "cursor_factory=GatedCursor at connect and in both pools",
    ),
    ("doctor.py", "db-connect"): (2, "read-only probe"),
    ("doctor_mcp.py", "db-connect"): (2, "read-only probe"),
    ("doctor_pgvector.py", "db-connect"): (1, "read-only probe"),
    ("hooks/agent_briefing_query.py", "db-connect"): (1, "read-only query"),
    ("hooks/auto_recall.py", "db-connect"): (1, "read-only query"),
    ("hooks/session_start_db.py", "db-connect"): (1, "read-only query"),
    ("hooks/session_start_sources.py", "db-connect"): (
        1,
        "reads a foreign claude-mem database",
    ),
    ("migrate.py", "db-connect"): (1, "schema probe; DDL only, binds no content"),
}

_GATED_DB_MODULES = {
    "infrastructure/sqlite_connection_registry.py": ("factory", "GatedConnection"),
    "infrastructure/pg_store_schema.py": ("cursor_factory", "GatedCursor"),
}

_SQL_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|COPY\s)")


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


def _db_calls(path: str) -> list[ast.Call]:
    tree = ast.parse((MCP_SERVER_ROOT / path).read_text(encoding="utf-8"))
    hits = {line for p, line, kind in scan_tree() if p == path and kind == "db-connect"}
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.lineno in hits]


def _passes_gate(call: ast.Call, keyword: str, gate: str) -> bool:
    for kw in call.keywords:
        if kw.arg == keyword and isinstance(kw.value, ast.Name):
            return kw.value.id == gate
        if kw.arg == "kwargs" and isinstance(kw.value, ast.Dict):
            for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == keyword
                    and isinstance(value, ast.Name)
                ):
                    return value.id == gate
    return False


def test_writable_database_connections_are_gated_by_construction():
    for path, (keyword, gate) in _GATED_DB_MODULES.items():
        calls = _db_calls(path)
        assert calls, path
        for call in calls:
            assert _passes_gate(call, keyword, gate), f"{path}:{call.lineno}"


def test_ungated_database_connections_bind_no_write_statement():
    ungated = {p for (p, k) in _AUDITED if k == "db-connect"} - set(_GATED_DB_MODULES)
    for path in sorted(ungated):
        tree = ast.parse((MCP_SERVER_ROOT / path).read_text(encoding="utf-8"))
        literals = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        writes = [s for s in literals if _SQL_WRITE.search(s.upper())]
        assert not writes, f"{path} is audited read-only but writes: {writes}"


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
