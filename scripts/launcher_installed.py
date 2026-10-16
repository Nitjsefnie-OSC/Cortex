"""Which copies of this plugin the host installed, and where the next is — stdlib only.

The host records every installed plugin in ``<config>/plugins/installed_plugins.json``
and keeps each marketplace's local clone where ``known_marketplaces.json`` says.
Plugin data is ``<config>/plugins/data/<id>`` (measured: Claude Code 2.1.300), so
the host's records are found from ``CLAUDE_PLUGIN_DATA`` alone. Nothing here
guesses: a record that is missing, unreadable or does not match yields ``None``,
and the caller then does nothing (no set is removed, none is pre-installed); an
unreadable one is named on stderr.

The marketplace clone is the SOURCE OF THE NEXT uv.lock: one function,
``next_tree``, is the only place that decides where it comes from.

source: ADR-1108
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_PLUGIN_MANIFEST = Path(".claude-plugin") / "plugin.json"
_MARKETPLACE_MANIFEST = Path(".claude-plugin") / "marketplace.json"


@dataclass(frozen=True)
class Installed:
    """The host's record of this plugin: its id and every installed copy."""

    plugin_id: str  # "<name>@<marketplace>"
    plugins_dir: Path
    trees: tuple[Path, ...]


def _read(path: Path) -> dict | None:
    """The JSON object in ``path``; None when it is missing or not an object.

    A file that exists but cannot be read or parsed is also None (the caller
    then removes and pre-installs nothing), and is named on stderr with the
    cause, which reaches the maintain or Setup log.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"[cortex-launcher] could not read {path}: {exc}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def find(running_root: str, data: str) -> Installed | None:
    """The host's record of the plugin whose installed copy is ``running_root``.

    Postcondition: None unless ``data`` is ``<config>/plugins/data/<id>``, the
    host's ``installed_plugins.json`` is readable, and one plugin id lists
    ``running_root`` as an install path; then every install path of that id.
    """
    data_path = Path(data)
    if data_path.parent.name != "data" or data_path.parent.parent.name != "plugins":
        return None
    plugins_dir = data_path.parent.parent
    plugins = (_read(plugins_dir / "installed_plugins.json") or {}).get("plugins")
    if not isinstance(plugins, dict):
        return None
    for plugin_id, entries in plugins.items():
        if not isinstance(entries, list):
            continue
        paths = [e.get("installPath") for e in entries if isinstance(e, dict)]
        if any(isinstance(p, str) and _same(p, running_root) for p in paths):
            trees = tuple(Path(p) for p in paths if isinstance(p, str))
            return Installed(plugin_id, plugins_dir, trees)
    return None


def version_of(tree: Path) -> str | None:
    """The plugin version a tree's manifest declares."""
    version = (_read(tree / _PLUGIN_MANIFEST) or {}).get("version")
    return version if isinstance(version, str) else None


def clone_dir(installed: Installed) -> Path | None:
    """The marketplace's local clone of this plugin's marketplace, None if absent."""
    marketplace = installed.plugin_id.partition("@")[2]
    known = (_read(installed.plugins_dir / "known_marketplaces.json") or {}).get(
        marketplace
    )
    location = known.get("installLocation") if isinstance(known, dict) else None
    return Path(location) if isinstance(location, str) else None


def next_tree(installed: Installed) -> Path | None:
    """The plugin tree the marketplace would install next: its local clone.

    Postcondition: the directory the marketplace entry of this plugin names
    inside the marketplace's clone, or None when the marketplace is not
    cloned locally, lists the plugin from somewhere other than a relative
    path inside the clone, or the clone is unreadable. This is the one seam
    that says where the next uv.lock comes from.
    """
    name = installed.plugin_id.partition("@")[0]
    clone = clone_dir(installed)
    if clone is None:
        return None
    plugins = (_read(clone / _MARKETPLACE_MANIFEST) or {}).get("plugins")
    for entry in plugins if isinstance(plugins, list) else []:
        if isinstance(entry, dict) and entry.get("name") == name:
            source = entry.get("source")
            if isinstance(source, str) and source.startswith("./"):
                return (clone / source).resolve()
            return None
    return None
