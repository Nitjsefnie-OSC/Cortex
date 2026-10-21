"""Owner-only storage for Cortex's personal data.

Everything Cortex keeps about the user lives under a few roots: the
methodology directory (database, logs, telemetry, wiki, artifacts, profiles),
the capture worker's runtime directory, ``brain-index.json`` and
``~/.cache/cortex`` (upstream MCP logs, the viz registry). On POSIX a file
under them is created ``0600`` and a directory ``0700`` (``gated_files``, the
rotating log, the SQLite file, whose WAL and shared-memory files take its
mode); on Windows the roots carry an ACL granting only the current user, which
files and directories inherit.

Installs made before this existed hold ``0644``/``0755`` (umask) files:
:func:`harden_private_storage` tightens them once per install (a stamp file
records it), and the roots themselves on every call.

source: ADR-1112"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from mcp_server.shared.platform import cache_dir

PRIVATE_FILE_MODE = 0o600  # source: ADR-1112 (owner read/write)
PRIVATE_DIR_MODE = 0o700  # source: ADR-1112 (owner only)
# source: ADR-1112 — bump to re-run the one-time tightening on every install
_STAMP_NAME = ".private-modes-v1"


def claude_root() -> Path:
    override = os.environ.get("CORTEX_CLAUDE_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".claude"


def cortex_cache_dir() -> Path:
    """``CORTEX_CACHE_DIR``, else ``<XDG cache or ~/.cache>/cortex``: session
    registry, groomer coordination, upstream MCP logs."""
    override = os.environ.get("CORTEX_CACHE_DIR", "").strip()
    return Path(override).expanduser() if override else cache_dir() / "cortex"


def private_roots() -> tuple[Path, ...]:
    """Directories whose whole content is Cortex personal data."""
    root = claude_root()
    return (root / "methodology", root / ".capture-worker", cortex_cache_dir())


def private_files() -> tuple[Path, ...]:
    return (claude_root() / "brain-index.json",)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def is_private(path: str | Path) -> bool:
    """True when ``path`` is, or is inside, Cortex personal-data storage."""
    target = _absolute(Path(path))
    if target in {_absolute(p) for p in private_files()}:
        return True
    return any(
        target == r or r in target.parents for r in map(_absolute, private_roots())
    )


def _owning_root(path: Path) -> Path | None:
    target = _absolute(path)
    for root in map(_absolute, private_roots()):
        if target == root or root in target.parents:
            return root
    return None


def ensure_private_dir(path: str | Path) -> Path:
    """Create ``path`` (and parents); every level from its private root down
    is owner-only. Outside the private roots this is a plain ``mkdir -p``."""
    directory = _absolute(Path(path))
    directory.mkdir(parents=True, exist_ok=True)
    root = _owning_root(directory)
    if root is None or sys.platform == "win32":
        return directory
    for level in (directory, *directory.parents):
        if stat.S_IMODE(level.stat().st_mode) != PRIVATE_DIR_MODE:
            os.chmod(level, PRIVATE_DIR_MODE)
        if level == root:
            break
    return directory


def create_private_file(path: str | Path) -> None:
    """Create ``path`` empty and ``0600`` if absent (before a library opens it)."""
    target = Path(path)
    ensure_private_dir(target.parent)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(target, flags, PRIVATE_FILE_MODE))


def _tighten(path: Path, info: os.stat_result) -> bool:
    mode = stat.S_IMODE(info.st_mode)
    wanted = PRIVATE_DIR_MODE if stat.S_ISDIR(info.st_mode) else mode & 0o700
    if mode == wanted:
        return False
    os.chmod(path, wanted)
    return True


def tighten_tree(root: Path) -> int:
    """Strip group/other bits from every directory and regular file under
    ``root`` (symlinks are neither followed nor changed); return the count."""
    changed = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (".", *dirnames, *filenames):
            entry = Path(dirpath) if name == "." else Path(dirpath, name)
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                changed += _tighten(entry, info)
    return changed


Runner = Callable[[Sequence[str]], object]


def restrict_windows_acl(path: Path, run: Runner | None = None) -> None:
    """Grant only the current user full control, inherited by the tree."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    principal = f"{domain}\\{user}" if domain else user
    command = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{principal}:(OI)(CI)F",
    ]
    if run is None:
        subprocess.run(command, check=True, capture_output=True)  # noqa: S603 — fixed system tool
    else:
        run(command)


def harden_private_storage(
    *, platform: str = sys.platform, run: Runner | None = None
) -> int:
    """Make every existing private root owner-only; return entries changed.

    The roots are tightened on every call (cheap); their content once per
    install, recorded by a stamp in the methodology directory."""
    roots = [r for r in private_roots() if r.is_dir()]
    if platform == "win32":
        for root in roots:
            restrict_windows_acl(root, run)
        return len(roots)
    changed = 0
    for root in roots:
        changed += _tighten(root, root.lstat())
    for file in private_files():
        if file.is_file() and not file.is_symlink():
            changed += _tighten(file, file.lstat())
    stamp = private_roots()[0] / _STAMP_NAME
    if roots and not stamp.exists():
        changed += sum(tighten_tree(root) for root in roots)
        create_private_file(stamp)
    return changed


def harden_at_startup() -> None:
    """Entry-point form: a path that cannot be tightened (another owner, a
    read-only mount) is reported on stderr with its cause; startup goes on."""
    try:
        harden_private_storage()
    except OSError as exc:
        print(
            f"[cortex] could not make personal data owner-only: {exc} "
            f"(path: {exc.filename})",
            file=sys.stderr,
        )
