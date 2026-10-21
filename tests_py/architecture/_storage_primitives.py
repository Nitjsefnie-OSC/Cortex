"""AST detector for every primitive that can persist bytes from mcp_server/.

Shared by the storage-gate architecture tests (ADR-1110). A *primitive* is a
call that creates or writes a file, opens a database connection, or hands a
child process a file to write into. Each finding is
``(relative_path, line, kind)``.

source: ADR-1110"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

MCP_SERVER_ROOT = Path(__file__).resolve().parents[2] / "mcp_server"

_WRITE_MODE_CHARS = set("wax+")
_OS_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"}
_FILE_HANDLERS = {
    "FileHandler",
    "RotatingFileHandler",
    "TimedRotatingFileHandler",
    "WatchedFileHandler",
}
_DB_CONNECT = {"connect", "ConnectionPool"}
_CHILD_STD_OK = {"DEVNULL", "PIPE", "STDOUT", "None"}
_TEMPFILES = {"mkstemp", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
_COPIES = {"copy", "copy2", "copyfile", "copytree", "copyfileobj"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _owner(node: ast.Call) -> str:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return ""
    value = func.value
    if isinstance(value, ast.Subscript):  # psycopg.Connection[DictRow].connect
        value = value.value
    return _name(value)


def _mode(node: ast.Call, position: int) -> str | None:
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    if len(node.args) > position and isinstance(node.args[position], ast.Constant):
        return str(node.args[position].value)
    return None


def _open_kind(node: ast.Call) -> str | None:
    func_name = _name(node.func)
    if func_name not in {"open", "fdopen"}:
        return None
    if _owner(node) == "os" and func_name == "open":
        # read-only only when the flags are spelled out and none can write;
        # flags held in a variable count as writing
        flags = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        read_only = "O_RDONLY" in flags and not flags & _OS_WRITE_FLAGS
        return None if read_only else "os.open-write"
    is_method = isinstance(node.func, ast.Attribute) and _owner(node) != "os"
    mode = _mode(node, 0 if is_method else 1)
    if mode is not None and set(mode) & _WRITE_MODE_CHARS:
        return "open-write"
    return None


def _child_stdio_kind(node: ast.Call) -> str | None:
    if _name(node.func) not in {"Popen", "run", "call", "check_call", "check_output"}:
        return None
    for kw in node.keywords:
        if kw.arg in {"stdout", "stderr"}:
            target = _name(kw.value) or repr(getattr(kw.value, "value", None))
            if target not in _CHILD_STD_OK:
                return "child-stdio-file"
    return None


def classify(node: ast.Call) -> str | None:
    """Return the primitive kind of ``node``, or None when it writes nothing."""
    name = _name(node.func)
    if name in {"write_text", "write_bytes"}:
        return None if _owner(node) == "gated_files" else name
    if name in _FILE_HANDLERS:
        return "log-file-handler"
    if name in _TEMPFILES:
        return "tempfile"
    if name in _COPIES and _owner(node) == "shutil":
        return "shutil-copy"
    if name in _DB_CONNECT and (
        _owner(node) in {"sqlite3", "psycopg", "Connection"} or name == "ConnectionPool"
    ):
        return "db-connect"
    return _open_kind(node) or _child_stdio_kind(node)


def scan_file(path: Path) -> Iterator[tuple[str, int, str]]:
    rel = path.relative_to(MCP_SERVER_ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = classify(node)
            if kind is not None:
                yield rel, node.lineno, kind


def scan_tree() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(MCP_SERVER_ROOT.rglob("*.py")):
        found.extend(scan_file(path))
    return found
