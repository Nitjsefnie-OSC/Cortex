"""The pinned PostgreSQL + pgvector archives: which this host runs, how installed.

Stdlib only. Both archives come from GitHub releases at pinned tags and are
kept only when their sha256 equals the digest recorded here; anything else
raises ``AssetError``. The PostgreSQL tree is extracted, pgvector's ``lib/``
and ``share/extension/`` are laid over it, and a stamp naming both digests is
written last, so a tree without the stamp is never used.

A host this table has no row for is ``UnsupportedError``: the plugin keeps the
SQLite store there (ADR-1110 records why per platform).

source: ADR-1110
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tarfile
import urllib.request
from pathlib import Path

PG_VERSION = "18.6.0"  # source: ADR-1110 (theseus-rs/postgresql-binaries release)
PGVECTOR_TAG = (
    "v0.18.320"  # source: ADR-1110 (nicoloboschi/pgvector_compiled, pgvector 0.8.5)
)
# source: ADR-1110 (release download URLs of both projects)
PG_URL = "https://github.com/theseus-rs/postgresql-binaries/releases/download/{version}/{name}"
VECTOR_URL = (
    "https://github.com/nicoloboschi/pgvector_compiled/releases/download/{tag}/{name}"
)
# Per Rust target triple, the PostgreSQL then the pgvector archive digest,
# measured 2026-10-17 and equal to the digests both releases publish.
SHA256 = {  # source: ADR-1110
    "aarch64-apple-darwin": (
        "a257bcdb8aa3301a13d6a5bcec48f8c9517045b7cbae71e50788b2615539e95b",
        "4c580ce63f2c7d462da9241b0d11f8f5038783de7a69a8aa3fe92d6996c75de6",
    ),
    "x86_64-apple-darwin": (
        "f8918fbe747e0d79bda58ad8f8afcf23ac384b847ff3af18856f0825ba54b031",
        "e95a3a1f45e334b2bcbb070961f7ed9a33d118f8f99b36f91946f5c80e4f635c",
    ),
    "x86_64-unknown-linux-gnu": (
        "bb3d09f876b2383e25a8c9ce09e32d185a03656a23d074f5195542b1b1b3ca61",
        "2461d601bbf2c8ab3c70965be87dff92e5778d1610f9b294fcf23e32bd8a603e",
    ),
    "aarch64-unknown-linux-gnu": (
        "5d2b5e8be9e96bfc3d7f22090057a836ec36d5d092542d8d3b93cc145cf4ee13",
        "c7aa638f11c1ccd219b4efc62de1fe5884b928f0088579f5d176f654c56832d6",
    ),
}
_MACHINES = {
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}  # source: ADR-1110
_CHUNK = 1 << 20  # source: ADR-1110 (1 MiB read size; any size gives the same digest)
STAMP = (
    ".cortex-installed.json"  # source: ADR-1110 (written last: the tree is complete)
)


class AssetError(RuntimeError):
    """The pinned archives could not be installed; the message says why."""


class UnsupportedError(AssetError):
    """This host has no pinned archive: the plugin keeps the SQLite store."""


def triple(system: str, machine: str, libc: str) -> str:
    """The target triple of ``platform.system()/machine()`` and the libc name."""
    arch = _MACHINES.get(machine.lower())
    if system == "Darwin" and arch:
        name = f"{arch}-apple-darwin"
    elif system == "Linux" and arch and libc == "glibc":
        name = f"{arch}-unknown-linux-gnu"
    elif system == "Windows":
        raise UnsupportedError(
            "native Windows: the server lifecycle uses POSIX flock, and the "
            "Windows archive does not bundle VCRUNTIME140.dll"
        )
    else:
        raise UnsupportedError(
            f"no pinned PostgreSQL archive for {system} {machine} {libc or 'no libc'}"
        )
    if name not in SHA256:
        raise UnsupportedError(f"no pinned PostgreSQL archive for {name}")
    return name


def host_triple() -> str:
    """The triple of the interpreter running this code (Rosetta reports x86_64)."""
    return triple(platform.system(), platform.machine(), platform.libc_ver()[0])


def tree(dist: Path, target: str) -> Path:
    return dist / f"postgresql-{PG_VERSION}-{target}"


def is_installed(dist: Path, target: str) -> bool:
    """True iff the tree for ``target`` carries the stamp of both pinned digests."""
    stamp = tree(dist, target) / STAMP
    try:
        recorded = json.loads(stamp.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    return recorded == {"postgresql": SHA256[target][0], "pgvector": SHA256[target][1]}


def _download(url: str, target: Path, expected: str) -> None:
    sha = hashlib.sha256()
    try:
        with urllib.request.urlopen(url) as response, target.open("wb") as out:  # noqa: S310 — fixed https URL built from the pinned table
            for block in iter(lambda: response.read(_CHUNK), b""):
                sha.update(block)
                out.write(block)
    except OSError as exc:
        raise AssetError(f"could not download {url}: {exc}") from exc
    if sha.hexdigest() != expected:
        raise AssetError(
            f"{url} has sha256 {sha.hexdigest()}, expected {expected}; refusing it"
        )


def _inside(base: Path, member: tarfile.TarInfo) -> bool:
    """The member, and a link's target, stay under ``base``."""
    resolved = (base / member.name).resolve()
    if not resolved.is_relative_to(base.resolve()):
        return False
    if member.issym() or member.islnk():
        anchor = resolved.parent if member.issym() else base
        return (anchor / member.linkname).resolve().is_relative_to(base.resolve())
    return member.isfile() or member.isdir() or member.issym() or member.islnk()


def _extract(archive: Path, into: Path) -> Path:
    """Extract ``archive`` (one top-level directory) under ``into``; return it."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        unsafe = [m.name for m in members if not _inside(into, m)]
        if unsafe:
            raise AssetError(
                f"{archive.name} has members outside its tree: {unsafe[:3]}"
            )
        tar.extractall(into, members=members, **_FILTER)  # noqa: S202 — every member checked by _inside above
    tops = {Path(m.name).parts[0] for m in members}
    if len(tops) != 1:
        raise AssetError(
            f"{archive.name} has {len(tops)} top-level entries, expected 1"
        )
    return into / tops.pop()


# The data filter where this Python has it (3.10.12+, 3.11.4+, 3.12+); the
# members were already checked, so older 3.10/3.11 extract the same tree.
_FILTER = (
    {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
)  # source: PEP 706


def _overlay(vector: Path, pg: Path) -> None:
    """pgvector's library and extension files into the PostgreSQL tree."""
    for sub in ("lib", "share/extension"):
        for item in (vector / sub).iterdir():
            if item.is_file():
                shutil.copy2(item, pg / sub / item.name)


def install(dist: Path, target: str) -> Path:
    """The installed tree for ``target``; downloads and verifies it when absent.

    Precondition: the caller holds the managed root's install lock.
    """
    final = tree(dist, target)
    if is_installed(dist, target):
        return final
    scratch = dist / f".install-{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    try:
        pg_sha, vector_sha = SHA256[target]
        pg_name, vector_name = (
            f"postgresql-{PG_VERSION}-{target}.tar.gz",
            f"pgvector-{target}-pg18.tar.gz",
        )
        _download(
            PG_URL.format(version=PG_VERSION, name=pg_name), scratch / pg_name, pg_sha
        )
        _download(
            VECTOR_URL.format(tag=PGVECTOR_TAG, name=vector_name),
            scratch / vector_name,
            vector_sha,
        )
        pg = _extract(scratch / pg_name, scratch / "pg")
        _overlay(_extract(scratch / vector_name, scratch / "vector"), pg)
        (pg / STAMP).write_text(
            json.dumps({"postgresql": pg_sha, "pgvector": vector_sha}), encoding="utf-8"
        )
        shutil.rmtree(final, ignore_errors=True)  # an unstamped, partial tree
        os.replace(pg, final)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return final
