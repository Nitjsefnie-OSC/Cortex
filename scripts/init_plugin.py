#!/usr/bin/env python3
"""Install Cortex's runtime now, show the progress, and fail loudly — stdlib only.

    claude plugin marketplace add cdeust/Cortex
    claude plugin install hypermnesia-mcp
    python3 <plugin>/scripts/init_plugin.py          # or: claude --init-only

``claude --init-only`` runs the plugin's ``Setup`` hook (``scripts/launcher.py
--setup``): the base set, the semantic-search set and the embedding model are
installed before the first session. Claude Code discards that hook's output and
exits 0 whatever the hook did (measured: 2.1.300, also under a pty), so this
wrapper runs the same command, shows what the hook logged while it runs, and
then reads the outcome the hook recorded: exit 0 only when it says ``ready``.

Where the files are: the install path from ``claude plugin list --json``, and the
plugin data directory ``<config>/plugins/data/<id>`` with every character of
the plugin id outside ``[A-Za-z0-9_-]`` replaced by ``-`` (the host's naming,
measured: ``hypermnesia-mcp@cortex-plugins`` is ``hypermnesia-mcp-cortex-plugins``).

source: docs/adr/ADR-1108-dependencies-install-at-setup-and-live-in-per-lock-sets.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launcher_layout  # noqa: E402 — sibling module, resolvable once scripts/ is on sys.path
import launcher_setup  # noqa: E402

PLUGIN_ID = "hypermnesia-mcp@cortex-plugins"
NOT_INSTALLED = 2  # source: ADR-1108 (exit 1 is "installed, not ready")
_POLL_SECONDS = 0.5  # source: ADR-1108 (log tail refresh; not a deadline)


def data_dir(plugin_id: str, env: dict[str, str]) -> Path:
    """The host's plugin data directory for ``plugin_id``."""
    config = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(config) / "plugins" / "data" / re.sub(r"[^A-Za-z0-9_-]", "-", plugin_id)


def install_root(claude: list[str], plugin_id: str, env: dict[str, str]) -> Path | None:
    """Where the host runs the plugin from, None when it is not installed."""
    listed = subprocess.run(
        [*claude, "plugin", "list", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    for plugin in json.loads(listed.stdout or "[]"):
        if plugin.get("id") == plugin_id:
            return Path(plugin.get("readFromFolder") or plugin["installPath"])
    return None


def _follow(process: subprocess.Popen, log: Path, start: int) -> None:
    """Print what ``log`` gains after offset ``start`` until ``process`` exits."""
    offset = start
    while True:
        done = process.poll() is not None
        try:
            with log.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read()
        except OSError:
            chunk = b""
        if chunk:
            sys.stdout.write(chunk.decode("utf-8", "replace"))
            sys.stdout.flush()
            offset += len(chunk)
        if done:
            return
        time.sleep(_POLL_SECONDS)


def init(claude: list[str], plugin_id: str, env: dict[str, str]) -> int:
    """Run ``claude --init-only`` and verify its Setup hook's outcome.

    Postcondition: 0 iff the Setup hook recorded ``ready`` for this plugin's
    set (a ``gc_error`` it recorded is printed on stderr); 2 when the plugin
    is not installed in Claude Code; otherwise 1 with the cause printed.
    """
    root = install_root(claude, plugin_id, env)
    if root is None:
        print(f"{plugin_id} is not installed: run `claude plugin install` first")
        return NOT_INSTALLED
    deps = launcher_layout.project_deps_dir(data_dir(plugin_id, env), root)
    log = launcher_setup.log_path(deps)
    start = log.stat().st_size if log.exists() else 0
    process = subprocess.Popen(
        [*claude, "--init-only"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    _follow(process, log, start)
    result = launcher_setup.outcome(deps)
    if result.get("status") == "ready":
        print(f"Cortex is ready ({deps})")
        if result.get("gc_error"):  # the set is ready; earlier sets stayed
            print(
                f"Cortex kept earlier sets: {result['gc_error']} (log: {log})",
                file=sys.stderr,
            )
        return 0
    why = result.get("error") or "the Setup hook did not run or did not finish"
    print(f"Cortex is NOT ready: {why} (log: {log})", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin", default=PLUGIN_ID)
    parser.add_argument("--claude", default="claude")
    arguments = parser.parse_args()
    return init([arguments.claude], arguments.plugin, dict(os.environ))


if __name__ == "__main__":
    sys.exit(main())
