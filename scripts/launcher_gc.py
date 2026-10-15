"""Remove the installed sets no installed plugin version references — stdlib only.

A set (``<data>/sets/<key>/`` and its ``<key>.*`` sidecars) is kept when its key
is one of: the running version's, an installed copy's, or the marketplace
clone's (pre-installed for the update that is about to apply). It is also kept
while a live MCP server has it registered (``mark_in_use``): that server
imports from it for the life of its session. When any reference cannot be read
nothing is removed: a set is never removed on a guess.

The flat ``deps/`` directory the plugin used before per-lock sets (and its
sidecars) is removed only when it is provably unreferenced: the running
version's own set is installed AND no copy of the plugin that still imports
from ``deps/`` exists, neither in the host's install records nor in its cache
(an old server cannot register itself as a user, so the only proof is that the
tree it runs from is gone). When the cache cannot be read nothing is removed.

Never run inside a hook: removing a set is seconds of deletion.

source: ADR-1108
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
from pathlib import Path

import launcher_deps_fs
import launcher_installed
import launcher_layout
import launcher_remote

# The file launcher_deps writes into a set when its base install succeeded.
BASE_STAMP = (
    ".cortex-deps-stamp-base.json"  # source: scripts/launcher_deps.py _stamp_path
)

# The module only the per-lock layout has (source: scripts/launcher_layout.py).
_LAYOUT_MODULE = "launcher_layout.py"

# What the flat layout (ADR-0747 to ADR-1104) left in plugin data.
_LEGACY = re.compile(
    r"^(?:deps(?:\.(?:tmp-\d+|install\.lock|base-job\.lock|semantic-job\.lock|uv-[\w.]+))?"
    r"|semantic-install\.json)$"
)


def _users_dir(deps_dir: str) -> Path:
    return Path(f"{deps_dir}.users")


def mark_in_use(deps_dir: str) -> None:
    """Record this process as a user of ``deps_dir`` until it exits.

    Postcondition: ``<deps>.users/<pid>`` exists while this process lives, so
    ``in_use`` is True for the set; an OS-level failure to record it is
    raised, not skipped (a set removed under a live server is worse).
    """
    marker = _users_dir(deps_dir) / str(os.getpid())
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    atexit.register(lambda: marker.unlink(missing_ok=True))


def in_use(deps_dir: str) -> bool:
    """True iff a live process registered itself as a user of ``deps_dir``."""
    directory = _users_dir(deps_dir)
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    live = False
    for name in names:
        if name.isdigit() and launcher_deps_fs.pid_alive(int(name)):
            live = True
        else:
            (directory / name).unlink(missing_ok=True)
    return live


def referenced_keys(running_root: str, data: str) -> set[str] | None:
    """Every key an installed copy, the running copy or the next copy locks.

    Postcondition: None when the host's records do not identify the plugin or
    a referenced tree has no readable uv.lock (the caller removes nothing).
    """
    installed = launcher_installed.find(running_root, data)
    return None if installed is None else _keys(installed, running_root, data)


def _keys(
    installed: launcher_installed.Installed, running_root: str, data: str
) -> set[str] | None:
    """The keys of the trees that own sets, and of a set pre-installed from the
    remote for an update no installed copy has reached; a flat-layout tree owns none."""
    trees = list(installed.trees)
    following = launcher_installed.next_tree(installed)
    if following is not None:
        trees.append(following)
    trees = [t for t in trees if (t / "scripts" / _LAYOUT_MODULE).is_file()]
    trees.append(Path(running_root))
    keys = {launcher_layout.set_key(tree) for tree in trees}
    if None in keys:
        return None
    versions = [v for v in map(launcher_installed.version_of, trees) if v]
    remote = launcher_remote.referenced_key(data, versions)
    return {k for k in keys if k} | ({remote} if remote else set())


def doomed(data: Path, keep: set[str]) -> list[Path]:
    """The set directories and sidecars of ``data`` whose key is not in ``keep``."""
    sets = data / launcher_layout.SETS
    try:
        names = sorted(os.listdir(sets))
    except OSError:
        return []
    by_key: dict[str, list[Path]] = {}
    for name in names:
        by_key.setdefault(name.split(".", 1)[0], []).append(sets / name)
    doomed: list[Path] = []
    for key, paths in by_key.items():
        if key not in keep and not in_use(str(sets / key)):
            doomed.extend(paths)
    return doomed


def _stamped(data: Path, key: str) -> bool:
    """True iff the set ``key`` carries the base stamp an install leaves last."""
    return (data / launcher_layout.SETS / key / BASE_STAMP).is_file()


def flat_layout_trees(installed: launcher_installed.Installed) -> list[Path] | None:
    """The plugin trees that still keep their dependencies in the flat ``deps/``.

    A tree is on the flat layout iff it has no ``scripts/launcher_layout.py``.
    Looks at every install path the host records and every version directory in
    the host's cache of this plugin (an updated-from copy lingers there while
    its server may still run). None when the cache directory exists but cannot
    be listed: the answer is then unknown, not "none".
    """
    name, _, marketplace = installed.plugin_id.partition("@")
    cache = installed.plugins_dir / "cache" / marketplace / name
    try:
        versions = [cache / entry for entry in sorted(os.listdir(cache))]
    except FileNotFoundError:
        versions = []
    except OSError:
        return None
    trees = {*installed.trees, *(v for v in versions if v.is_dir())}
    return sorted(t for t in trees if not (t / "scripts" / _LAYOUT_MODULE).is_file())


def collect(running_root: str, data: str) -> list[str]:
    """Remove unreferenced sets; the removed paths.

    Precondition: not called from a hook.
    Postcondition: every removed path belonged to a key outside
    ``referenced_keys`` with no live user; nothing is removed when the
    references are unknown. The flat legacy ``deps`` goes only once the running
    version's own set is installed to replace it and ``flat_layout_trees`` is
    empty (no copy that imports from it is installed or cached).
    """
    installed = launcher_installed.find(running_root, data)
    keep = None if installed is None else _keys(installed, running_root, data)
    if installed is None or keep is None:
        return []
    root = Path(data)
    paths = doomed(root, keep)
    running = launcher_layout.set_key(Path(running_root))
    if running and _stamped(root, running) and flat_layout_trees(installed) == []:
        paths += [p for p in root.iterdir() if _LEGACY.match(p.name)]
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    return [str(p) for p in paths]
