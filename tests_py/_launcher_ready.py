"""Run the real ``scripts/launcher.py`` for a test, with its base set reported ready.

A hook installs nothing (ADR-1106): ``launcher.py <hook>`` exits 1 unless the
plugin's ``deps/`` carries a stamp for this uv.lock and Python. A test that
drives a hook through the launcher is about the hook, not about what the
machine running it has installed (the test interpreter already has the
packages), so it starts the launcher through this driver: the real
``launcher.py`` run as ``__main__``, with the one readiness question answered.

source: ADR-1106
"""

from __future__ import annotations

import sys
from pathlib import Path

READY_DRIVER = (
    "import runpy, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import launcher_deps\n"
    "launcher_deps.ensure_deps = launcher_deps.ensure_all_deps = lambda d: None\n"
    "launcher_deps.set_ready = lambda d, kind: True\n"
    "sys.argv = sys.argv[2:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def ready_launcher_argv(launcher: Path, *arguments: str) -> list[str]:
    """``python launcher.py <arguments>`` with the base set reported installed."""
    return [
        sys.executable,
        "-c",
        READY_DRIVER,
        str(launcher.parent),
        str(launcher),
        *arguments,
    ]
