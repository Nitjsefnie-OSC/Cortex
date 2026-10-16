"""A killed install leaves ``<deps>.tmp-<pid>`` behind; the next install reclaims it.

Found 2026-10-14: 64 such directories in one plugin data directory, each holding
only uv's ``.lock`` (uv was cancelled by the host's hook timeout before it
wrote a package). Nothing removed them.

source: ADR-1108
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fs_mod(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "scripts.launcher_deps_fs", REPO_ROOT / "scripts" / "launcher_deps_fs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    (data / "sets" / "k1").mkdir(parents=True)
    return data, data / "sets" / "k1"


def _scratch(data: Path) -> list[Path]:
    paths = [
        data / "sets" / "k1.tmp-4242",
        data / "sets" / "k1.tmp-9",
        data / "uv-0.11.3.tmp-4243",
    ]
    for path in paths:
        path.mkdir()
        (path / ".lock").write_text("")
    return paths


def test_dead_pid_scratch_is_removed(fs_mod, tmp_path, monkeypatch):
    data, deps = _layout(tmp_path)
    scratch = _scratch(data)
    monkeypatch.setattr(fs_mod, "pid_alive", lambda _pid: False)
    fs_mod.sweep_stale_scratch(str(deps))
    assert [path.exists() for path in scratch] == [False, False, False]
    assert deps.exists()


def test_scratch_of_a_live_install_is_left_alone(fs_mod, tmp_path, monkeypatch):
    data, deps = _layout(tmp_path)
    scratch = _scratch(data)
    monkeypatch.setattr(fs_mod, "pid_alive", lambda pid: pid == 4242)
    fs_mod.sweep_stale_scratch(str(deps))
    assert [path.exists() for path in scratch] == [True, False, False]


def test_other_directories_are_never_touched(fs_mod, tmp_path, monkeypatch):
    data, deps = _layout(tmp_path)
    for name in ("k10.tmp-1", "other.tmp-1", "k1.tmp-x", "k1.bak-1"):
        (data / "sets" / name).mkdir()
    (data / "python.tmp-1").mkdir()
    monkeypatch.setattr(fs_mod, "pid_alive", lambda _pid: False)
    fs_mod.sweep_stale_scratch(str(deps))
    assert len(list((data / "sets").iterdir())) == 5
    assert (data / "python.tmp-1").exists()


def test_a_missing_parent_is_a_no_op(fs_mod, tmp_path):
    fs_mod.sweep_stale_scratch(str(tmp_path / "gone" / "sets" / "k1"))


def _unlistable(directory: Path) -> None:
    directory.chmod(0o000)
    if os.access(directory, os.R_OK):
        directory.chmod(0o755)
        pytest.skip("permissions are not enforced for this user")


def test_a_parent_that_cannot_be_listed_is_named_with_its_cause(
    fs_mod, tmp_path, capsys
):
    """The scratch stays, and the sweep says which directory and why."""
    data, deps = _layout(tmp_path)
    sets = data / "sets"
    _unlistable(sets)
    try:
        fs_mod.sweep_stale_scratch(str(deps))
    finally:
        sets.chmod(0o755)
    err = capsys.readouterr().err
    assert f"could not list {sets}" in err
    assert "Permission denied" in err


def test_scratch_that_cannot_be_removed_is_named_with_its_cause(
    fs_mod, tmp_path, monkeypatch, capsys
):
    """rmtree(ignore_errors=True) would leave it without a word."""
    data, deps = _layout(tmp_path)
    scratch = data / "sets" / "k1.tmp-9"
    locked = scratch / "pkg"
    locked.mkdir(parents=True)
    (locked / "module.py").write_text("")
    locked.chmod(0o555)
    if os.access(locked, os.W_OK):
        locked.chmod(0o755)
        pytest.skip("permissions are not enforced for this user")
    monkeypatch.setattr(fs_mod, "pid_alive", lambda _pid: False)
    try:
        fs_mod.sweep_stale_scratch(str(deps))
    finally:
        locked.chmod(0o755)
    assert scratch.exists()
    err = capsys.readouterr().err
    assert f"could not remove dead install scratch {scratch}" in err
    assert "Permission denied" in err
