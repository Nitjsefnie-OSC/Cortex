"""Find the next release's dependency set at the git remote itself — stdlib only.

Claude Code applies a plugin update in the same command that refreshes the
marketplace clone (measured 2.1.300), so the clone cannot announce an update
before it is applied. The remote can: the marketplace clone follows one branch
of one repository, and that branch's head is what ``claude plugin update``
installs next. Once per session start, the detached maintain job compares the
remote's head with the clone's; when they differ it fetches just the files that
define a dependency set (partial, shallow, sparse), installs that set the way
Setup does, and records the key so the set is not collected as unreferenced.

All network and git calls live here and run only in the detached job, never in
a hook. Every failure (no git, offline, a moved branch, a tree without a
selection) is "nothing to do": the next session asks again, nothing is stamped
failed, nothing is retried in a loop.

source: ADR-1108
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import launcher_installed
import launcher_layout

STATE = "next-remote.json"  # source: ADR-1108 (in plugin data)
TREE = "next-tree"  # source: ADR-1108 (the scratch checkout, removed after Setup)
# What defines a set and runs its Setup: the lock, the project, the launcher,
# the code the model warm-up imports, and the plugin manifest (the version).
# source: scripts/launcher.py (_use_utf8_output imports mcp_server.hooks), ADR-1108
SPARSE = (
    "/pyproject.toml",
    "/uv.lock",
    "/scripts/",
    "/mcp_server/",
    "/.claude-plugin/",
)
_QUERY_SECONDS = 60  # source: ADR-1108 (one ls-remote; a hung network is "offline")
_FETCH_SECONDS = 600  # source: ADR-1108 (a partial, shallow fetch of a few MB)


@dataclass(frozen=True)
class Clone:
    """What the marketplace clone follows: its remote, its branch, its head."""

    url: str
    branch: str
    head: str


def _git(*args: str, cwd: Path | None = None, seconds: int = _QUERY_SECONDS) -> str:
    """Stdout of a git call, "" on any failure (no git, offline, timeout, error)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def clone_of(installed: launcher_installed.Installed) -> Clone | None:
    """The marketplace clone's remote, branch and head; None unless all three."""
    tree = launcher_installed.clone_dir(installed)
    if tree is None:
        return None
    url = _git("remote", "get-url", "origin", cwd=tree)
    branch = _git("symbolic-ref", "--short", "HEAD", cwd=tree)
    head = _git("rev-parse", "HEAD", cwd=tree)
    return Clone(url, branch, head) if url and branch and head else None


def remote_head(clone: Clone) -> str:
    """The sha the remote's branch points at now, "" when it cannot be asked."""
    line = _git("ls-remote", clone.url, f"refs/heads/{clone.branch}")
    sha = line.split("\t", 1)[0]
    return sha if len(sha) == len(clone.head) else ""


def fetch(clone: Clone, data: str) -> tuple[Path, str] | None:
    """Check out the files of ``SPARSE`` at the branch head: (tree, sha) or None.

    Postcondition: the scratch tree under ``data`` holds exactly the head of the
    branch as fetched; the returned sha is that head (it may be newer than the
    one ``remote_head`` saw: the key is read from what was fetched).
    """
    tree = Path(data) / TREE
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True)
    ref = f"refs/heads/{clone.branch}"
    # A step that fails leaves no FETCH_HEAD, which ends the function below.
    _git("init", "-q", cwd=tree)
    _git("remote", "add", "origin", clone.url, cwd=tree)
    _git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=tree)
    _git(
        "fetch",
        "-q",
        "--depth=1",
        "--filter=blob:none",
        "origin",
        ref,
        cwd=tree,
        seconds=_FETCH_SECONDS,
    )
    sha = _git("rev-parse", "FETCH_HEAD", cwd=tree)
    _git("checkout", "-q", "FETCH_HEAD", cwd=tree, seconds=_FETCH_SECONDS)
    if not sha or not (tree / "uv.lock").is_file():
        shutil.rmtree(tree, ignore_errors=True)
        return None
    return tree, sha


def discard(data: str) -> None:
    """Remove the scratch tree (the set it defined is installed or abandoned)."""
    shutil.rmtree(Path(data) / TREE, ignore_errors=True)


def _state_path(data: str) -> Path:
    return Path(data) / STATE


def read_state(data: str) -> dict:
    """The last remote head looked at: {"sha", "key", "version"} or {}."""
    try:
        state = json.loads(_state_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ok = isinstance(state, dict) and all(
        isinstance(state.get(k), str) for k in ("sha", "key", "version")
    )
    return state if ok else {}


def record(data: str, sha: str, key: str, version: str) -> None:
    """Remember the head and the set it defines BEFORE that set is installed:
    Setup collects unreferenced sets when it succeeds, and this record is what
    references the set it just installed."""
    _state_path(data).write_text(
        json.dumps({"sha": sha, "key": key, "version": version}), encoding="utf-8"
    )


def _numbers(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def superseded(version: str, installed_versions: list[str]) -> bool:
    """True iff an installed copy is at ``version`` or newer (the record is spent)."""
    wanted = _numbers(version)
    have = [_numbers(v) for v in installed_versions]
    return wanted is not None and any(h is not None and h >= wanted for h in have)


def referenced_key(data: str, installed_versions: list[str]) -> str | None:
    """The key the record keeps alive: its set is pre-installed for an update
    that no installed copy has reached yet. None when there is no such record."""
    state = read_state(data)
    if not state or superseded(state["version"], installed_versions):
        return None
    return state["key"]


def prepare(
    installed: launcher_installed.Installed, data: str, running_root: str
) -> Path | None:
    """The tree whose set to install now, from the remote, or None.

    Postcondition: a tree is returned only when the remote branch moved past the
    clone, declares a plugin version other than the running one, and its set is
    not installed; its key is recorded first (``record``). The caller removes
    the tree with ``discard``.
    """
    clone = clone_of(installed)
    if clone is None:
        return None
    head = remote_head(clone)
    if not head or head == clone.head or head == read_state(data).get("sha"):
        return None
    fetched = fetch(clone, data)
    if fetched is None:
        return None
    tree, sha = fetched
    key = launcher_layout.set_key(tree)
    version = launcher_installed.version_of(tree)
    if key is None or version is None:
        discard(data)
        return None
    record(data, sha, key, version)
    due = version != launcher_installed.version_of(
        Path(running_root)
    ) and not launcher_layout.both_stamped(data, key)
    if not due:
        discard(data)
    return tree if due else None
