"""Where one uv.lock set lives in plugin data, and which key names it — stdlib only.

Plugin data (``CLAUDE_PLUGIN_DATA``) survives plugin updates, the plugin root
does not, so installed sets live in data, one directory per KEY::

    <data>/sets/<key>/              the packages and the stamps of one set
    <data>/sets/<key>.install.lock  and the other sidecars of that set
    <data>/python/  <data>/uv-<version>/  <data>/*.log   shared by every set

The key is the digest of everything that decides what a set contains: uv.lock
with the project's own ``version`` line left out (uv.lock records the plugin's
version, so every release changes the file's digest, while the dependency set
changed at 8 of 29 release transitions, v4.9.0 to v4.23.5, ``git show
<tag>:uv.lock``), the install selection (``launcher_sets.BASE`` and ``ML``, the
``uv export`` selectors) and the pinned CPython (``launcher_sets.MANAGED_PYTHON``).
Two plugin versions with the same key share one installed set; a new key is a
new directory, so installing the next version's set never touches the one a
running session imports from, even when a release changes only the selection.
The running interpreter's minor version is NOT in the key: the key must be the
same on the system Python 3.9 that starts a launch and on the managed 3.12 it
re-executes onto (``scripts/init_plugin.py`` reads its outcome file under the
former). A set installed for another minor is refused by its stamp instead
(``launcher_deps._stamp``).

source: ADR-1108
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

SETS = "sets"  # source: ADR-1108
LEGACY_DEPS = "deps"  # source: ADR-1108 (the one flat directory before sets/)
# source: ADR-1108 (the directory of a tree with no readable uv.lock or selection)
NO_LOCK = "no-uv-lock"
KEY_LENGTH = 16  # source: ADR-1108 (64 bits of sha256: 10^-19 collision odds per pair)

# The line uv writes for the project itself, which changes at every release.
_OWN_VERSION = re.compile(
    rb'(\[\[package\]\]\r?\nname = "hypermnesia-mcp"\r?\n)version = "[^"\r\n]*"\r?\n'
)


# The names of scripts/launcher_sets.py that decide what a set contains.
_SELECTION = ("BASE", "ML", "MANAGED_PYTHON")  # source: scripts/launcher_sets.py


def selection(tree: Path) -> dict | None:
    """The install selection ``tree`` declares, None when it cannot be read.

    Read from the source as data (``ast.literal_eval`` of the three assignments),
    so a plugin tree other than the running one (the marketplace clone) is
    keyed without importing or running any of its code.
    """
    try:
        module = ast.parse((tree / "scripts" / "launcher_sets.py").read_bytes())
        found = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _SELECTION
        }
    except (OSError, SyntaxError, ValueError):
        return None
    return found if set(found) == set(_SELECTION) else None


def set_key(tree: Path) -> str | None:
    """The key of the set the plugin tree ``tree`` installs, None when unreadable.

    Postcondition: equal for two trees that differ only in the project's own
    version; different when any locked package, hash or group, the install
    selection or the pinned CPython differs. None when uv.lock or the
    selection cannot be read (a caller then does nothing, it never guesses).
    """
    chosen = selection(tree)
    try:
        content = (tree / "uv.lock").read_bytes()
    except OSError:
        return None
    if chosen is None:
        return None
    digest = hashlib.sha256(_OWN_VERSION.sub(rb"\1", content))
    digest.update(b"\0" + json.dumps(chosen, sort_keys=True).encode())
    return digest.hexdigest()[:KEY_LENGTH]


def deps_dir(data: str | Path, key: str) -> str:
    """The directory of set ``key`` in plugin data ``data``."""
    return str(Path(data) / SETS / key)


def data_dir(deps: str | Path) -> Path:
    """The plugin data directory a set directory ``deps_dir`` lives in.

    The inverse of ``deps_dir``; every module that needs the data directory
    (logs, the managed CPython, the bootstrapped uv) asks here.
    """
    return Path(deps).parent.parent


def project_deps_dir(data: str | Path, project: str | Path) -> str:
    """The deps directory of the plugin tree ``project`` in plugin data ``data``."""
    key = set_key(Path(project)) or NO_LOCK
    return deps_dir(data, key)


def key_of(deps: str | Path) -> str:
    """The key a set directory ``deps_dir`` is named by."""
    return Path(deps).name


if (
    __name__ == "__main__"
):  # `launcher_layout.py DATA PROJECT` prints PROJECT's deps dir
    import sys

    print(project_deps_dir(sys.argv[1], sys.argv[2]))
