"""Starting the store broker as a detached child, through the plugin launcher.

The broker loads the store's native dependencies, so it starts like every
other child that needs ``deps/``: through ``scripts/launcher.py`` when the
plugin ships it (issue #667), detached from the client that launched it
(``detached_spawn``, ADR-1102), with the bound listener and the lease
inherited on Unix (the capture worker's spawn, ADR-0486). Its output goes to
its own private log, so stdout and stderr are closed here.

source: ADR-1112 decision 1
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mcp_server.hooks.launcher_command import child_command
from mcp_server.shared import detached_spawn


def launch_broker(
    module: str, arguments: list[str], environment: dict[str, str], fds: tuple[int, ...]
) -> None:
    """Start ``module`` detached; ``fds`` are inherited (POSIX only)."""
    options = detached_spawn.options()
    if fds:
        options = {**options, "pass_fds": fds}
    subprocess.Popen(
        child_command(module, *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, **environment},
        **options,
    )
