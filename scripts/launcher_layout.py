"""Where one uv.lock set lives in plugin data, and which key names it — stdlib only.

Plugin data (``CLAUDE_PLUGIN_DATA``) survives plugin updates, the plugin root
does not, so installed sets live in data, one directory per KEY::

    <data>/sets/<key>/              the packages and the stamps of one set
    <data>/sets/<key>.install.lock  and the other sidecars of that set
    <data>/python/  <data>/uv-<version>/  <data>/*.log   shared by every set

The key is the digest of uv.lock with the project's own ``version`` line left
out: uv.lock records the plugin's version, so every release changes the file's
digest, while the dependency set changed at 8 of 29 release transitions
(v4.9.0 to v4.23.5, ``git show <tag>:uv.lock``). Two plugin versions with the
same key share one installed set; a new key is a new directory, so installing
the next version's set never touches the one a running session imports from.

source: ADR-1108
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SETS = "sets"  # source: ADR-1108
LEGACY_DEPS = "deps"  # source: ADR-1108 (the one flat directory before sets/)
NO_LOCK = "no-uv-lock"  # source: ADR-1108 (the directory of a tree with no uv.lock)
KEY_LENGTH = 16  # source: ADR-1108 (64 bits of sha256: 10^-19 collision odds per pair)

# The line uv writes for the project itself, which changes at every release.
_OWN_VERSION = re.compile(
    rb'(\[\[package\]\]\r?\nname = "hypermnesia-mcp"\r?\n)version = "[^"\r\n]*"\r?\n'
)


def set_key(lock: Path) -> str | None:
    """The key of the set ``lock`` (a uv.lock) locks, None when it is unreadable.

    Postcondition: equal for two locks that differ only in the project's own
    version; different when any locked package, hash or group differs.
    """
    try:
        content = lock.read_bytes()
    except OSError:
        return None
    content = _OWN_VERSION.sub(rb"\1", content)
    return hashlib.sha256(content).hexdigest()[:KEY_LENGTH]


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
    key = set_key(Path(project) / "uv.lock") or NO_LOCK
    return deps_dir(data, key)


def key_of(deps: str | Path) -> str:
    """The key a set directory ``deps_dir`` is named by."""
    return Path(deps).name


if (
    __name__ == "__main__"
):  # `launcher_layout.py DATA PROJECT` prints PROJECT's deps dir
    import sys

    print(project_deps_dir(sys.argv[1], sys.argv[2]))
