"""Shared CI must fail before PostgreSQL tests silently select SQLite.

Source: tests_py/conftest.py backend selection and ADR-1114.
"""

import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[2]


def test_connection_failure_is_fatal(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test.invalid/ci")

    def refuse(_url):
        raise ConnectionError("test database unavailable")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=refuse))
    with pytest.raises(ConnectionError, match="test database unavailable"):
        runpy.run_path(str(REPO / ".github/ci/require_pg.py"))


@pytest.mark.parametrize(
    "extensions", [set(), {"vector"}, {"pg_trgm"}, {"vector", "pg_trgm"}]
)
def test_required_extensions_control_readiness(monkeypatch, extensions):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test.invalid/ci")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query):
            return [(name,) for name in extensions]

    monkeypatch.setitem(
        sys.modules, "psycopg", SimpleNamespace(connect=lambda _url: Connection())
    )
    path = str(REPO / ".github/ci/require_pg.py")
    if extensions == {"vector", "pg_trgm"}:
        runpy.run_path(path)
    else:
        with pytest.raises(RuntimeError, match="Missing PostgreSQL extensions"):
            runpy.run_path(path)


@pytest.mark.parametrize("phase", ["unit", "e2e"])
def test_adapter_stops_before_tests_when_database_probe_fails(tmp_path, phase):
    adapter = tmp_path / ".github/ci/run.sh"
    adapter.parent.mkdir(parents=True)
    adapter.write_text((REPO / ".github/ci/run.sh").read_text())
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CI_PROBE_LOG"\nexit 23\n')
    python.chmod(0o755)
    log = tmp_path / "commands.log"
    result = subprocess.run(
        ["bash", str(adapter), phase],
        env={**os.environ, "RUNNER_OS": "Linux", "CI_PROBE_LOG": str(log)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    assert log.read_text().splitlines() == [".github/ci/require_pg.py"]
