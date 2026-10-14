"""A killed install leaves ``<deps>.tmp-<pid>`` behind; the next install reclaims it.

Found 2026-10-14: 64 such directories in one plugin data directory, each holding
only uv's ``.lock`` (uv was cancelled by the host's hook timeout before it
wrote a package). Nothing removed them.

source: ADR-1106
"""

from __future__ import annotations

import importlib.util
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


def _scratch(tmp_path: Path) -> list[Path]:
    names = ("deps.tmp-4242", "deps.uv-0.11.3.tmp-4243", "deps.tmp-9")
    for name in names:
        (tmp_path / name).mkdir()
        (tmp_path / name / ".lock").write_text("")
    return [tmp_path / name for name in names]


def test_dead_pid_scratch_is_removed(fs_mod, tmp_path, monkeypatch):
    (tmp_path / "deps").mkdir()
    scratch = _scratch(tmp_path)
    monkeypatch.setattr(fs_mod, "pid_alive", lambda _pid: False)
    fs_mod.sweep_stale_scratch(str(tmp_path / "deps"))
    assert [path.exists() for path in scratch] == [False, False, False]
    assert (tmp_path / "deps").exists()


def test_scratch_of_a_live_install_is_left_alone(fs_mod, tmp_path, monkeypatch):
    scratch = _scratch(tmp_path)
    monkeypatch.setattr(fs_mod, "pid_alive", lambda pid: pid == 4242)
    fs_mod.sweep_stale_scratch(str(tmp_path / "deps"))
    assert [path.exists() for path in scratch] == [True, False, False]


def test_other_directories_are_never_touched(fs_mod, tmp_path, monkeypatch):
    for name in ("deps-extra.tmp-1", "other.tmp-1", "deps.tmp-x", "deps.bak-1"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(fs_mod, "pid_alive", lambda _pid: False)
    fs_mod.sweep_stale_scratch(str(tmp_path / "deps"))
    assert len(list(tmp_path.iterdir())) == 4


def test_a_missing_parent_is_a_no_op(fs_mod, tmp_path):
    fs_mod.sweep_stale_scratch(str(tmp_path / "gone" / "deps"))
