"""A host's plugin records on disk: installed copies, a marketplace clone, plugin data.

Builds what Claude Code 2.1.300 writes (measured on a real install): the
``<config>/plugins/{installed_plugins,known_marketplaces}.json`` records, the
marketplace clone with ``.claude-plugin/marketplace.json``, one plugin tree per
version (``uv.lock``, ``.claude-plugin/plugin.json``) and the data directory.

source: ADR-1108
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN = "hypermnesia-mcp"
MARKETPLACE = "cortex-plugins"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
_LOCK = (
    '[[package]]\nname = "hypermnesia-mcp"\nversion = "{version}"\n\n'
    '[[package]]\nname = "{dep}"\n'
)


_SELECTION = (
    'BASE = ("--only-group", "launcher-base")\n'
    'ML = ("--only-group", "launcher-ml")\n'
    'MANAGED_PYTHON = "{python}"\n'
)


def write_selection(path: Path, python: str = "3.12.13", base: str = "launcher-base"):
    """The ``scripts/launcher_sets.py`` a tree declares its install selection in."""
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    text = _SELECTION.format(python=python).replace("launcher-base", base)
    (path / "scripts" / "launcher_sets.py").write_text(text)


def plugin_tree(
    path: Path, version: str, dep: str = "numpy", flat: bool = False
) -> Path:
    """A plugin tree whose uv.lock locks ``dep`` (and the project's own version).

    ``flat``: a pre-per-lock-sets release, which has no ``launcher_layout.py``.
    """
    (path / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": PLUGIN, "version": version})
    )
    (path / "uv.lock").write_text(_LOCK.format(version=version, dep=dep))
    (path / "scripts").mkdir(exist_ok=True)
    (path / "scripts" / "launcher.py").write_text("")
    (path / "scripts" / "launcher_setup.py").write_text("")
    write_selection(path)
    if not flat:
        (path / "scripts" / "launcher_layout.py").write_text("")
    return path


class Host:
    """``config/plugins`` records for one installed copy and an optional clone."""

    def __init__(self, root: Path, version: str = "1.0.0", dep: str = "numpy") -> None:
        self.config = root / "config"
        self.plugins = self.config / "plugins"
        self.data = self.plugins / "data" / PLUGIN_ID.replace("@", "-")
        self.data.mkdir(parents=True)
        self.running = plugin_tree(
            self.plugins / "cache" / MARKETPLACE / PLUGIN / version, version, dep
        )
        self.clone: Path | None = None
        self.installed = [self.running]
        self.write_records()

    def add_clone(self, version: str, dep: str) -> Path:
        """The marketplace clone, whose plugin entry is the clone itself ("./")."""
        self.clone = plugin_tree(
            self.plugins / "marketplaces" / MARKETPLACE, version, dep
        )
        (self.clone / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {"name": MARKETPLACE, "plugins": [{"name": PLUGIN, "source": "./"}]}
            )
        )
        self.write_records()
        return self.clone

    def write_records(self) -> None:
        (self.plugins / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        PLUGIN_ID: [
                            {"scope": "user", "installPath": str(p), "version": "x"}
                            for p in self.installed
                        ]
                    },
                }
            )
        )
        known = {}
        if self.clone is not None:
            known[MARKETPLACE] = {"installLocation": str(self.clone)}
        (self.plugins / "known_marketplaces.json").write_text(json.dumps(known))

    def make_set(self, key: str, stamps: tuple[str, ...] = ("base", "ml")) -> Path:
        """An installed set directory with the stamps an install leaves."""
        directory = self.data / "sets" / key
        directory.mkdir(parents=True, exist_ok=True)
        for kind in stamps:
            (directory / f".cortex-deps-stamp-{kind}.json").write_text("{}")
        return directory
