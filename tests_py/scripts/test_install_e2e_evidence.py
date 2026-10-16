"""scripts/install_e2e/evidence.py reads a SessionStart hook that exits non-zero.

A hook that refuses (ADR-1108) finishes with ``error: status code 1`` and only a
stderr section; the check that session 1 "names its cause" needs that stderr.
The lines below are the host's own (claude 2.1.300, install_e2e run
2026-10-14), shortened.

source: ADR-1108
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = (
    "2026-10-14T21:26:40.287Z [DEBUG] Hooks: SessionStart:startup [bash -c 'x'] "
    "(plugin hypermnesia-mcp@cortex-plugins) "
)


def _evidence():
    folder = str(REPO_ROOT / "scripts" / "install_e2e")
    sys.path.insert(0, folder)
    try:
        spec = importlib.util.spec_from_file_location(
            "install_e2e_evidence", Path(folder) / "evidence.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(folder)


def _log(tmp_path: Path, status: str, payload: str) -> Path:
    log = tmp_path / "session.log"
    log.write_text(
        f"{_HOOK}finished with status {status} (586ms)\n"
        f"2026-10-14T21:26:40.287Z [DEBUG] Hook output\n"
        f'2026-10-14T21:26:40.287Z [ERROR] "Hook SessionStart:startup '
        f'(SessionStart) {payload}"\n',
        encoding="utf-8",
    )
    return log


def test_a_refusing_hook_exposes_its_stderr(tmp_path):
    log = _log(
        tmp_path,
        "1",
        r"error: status code 1\nstderr:\n[cortex-launcher] Cortex not ready: x "
        "(log: /d/deps-install.log)",
    )
    outcome = _evidence().session_start_outcome(log)
    assert outcome["status"] == 1 and outcome["ms"] == 586
    assert outcome["stdout"] == ""
    assert "Cortex not ready: x" in outcome["stderr"]
    assert "deps-install.log" in outcome["stderr"]


def test_a_successful_hook_still_splits_stdout_from_stderr(tmp_path):
    log = _log(
        tmp_path,
        "0",
        r"success:\nstdout:\n{\"systemMessage\": \"Cortex ready: s\"}\nstderr:\nnote",
    )
    outcome = _evidence().session_start_outcome(log)
    assert outcome["status"] == 0
    assert "Cortex ready" in outcome["stdout"]
    assert outcome["stderr"] == "note"
