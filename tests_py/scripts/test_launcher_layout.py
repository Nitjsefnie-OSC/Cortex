"""scripts/launcher_layout.py: the key names a dependency set, not a release.

source: ADR-1108
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def layout(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_layout

    return launcher_layout


def _lock(tmp_path: Path, version: str, dep_hash: str = "aaa") -> Path:
    path = tmp_path / f"uv-{version}-{dep_hash}.lock"
    path.write_text(
        '[[package]]\nname = "hypermnesia-mcp"\n'
        f'version = "{version}"\nsource = {{ editable = "." }}\n\n'
        f'[[package]]\nname = "numpy"\nversion = "2.4.4"\nhash = "{dep_hash}"\n'
    )
    return path


def test_two_releases_with_one_dependency_set_share_a_key(layout, tmp_path):
    assert layout.set_key(_lock(tmp_path, "4.23.4")) == layout.set_key(
        _lock(tmp_path, "4.23.5")
    )


def test_a_changed_dependency_is_a_new_key(layout, tmp_path):
    assert layout.set_key(_lock(tmp_path, "4.23.5", "aaa")) != layout.set_key(
        _lock(tmp_path, "4.23.5", "bbb")
    )


def test_only_the_projects_own_version_line_is_ignored(layout, tmp_path):
    other = tmp_path / "other.lock"
    other.write_text(
        '[[package]]\nname = "numpy"\nversion = "1"\n\n'
        '[[package]]\nname = "hypermnesia-mcp"\nversion = "9"\n'
    )
    changed = tmp_path / "changed.lock"
    changed.write_text(other.read_text().replace('version = "1"', 'version = "2"'))
    assert layout.set_key(other) != layout.set_key(changed)


def test_the_repositorys_own_lock_has_a_key_and_the_key_has_the_declared_length(
    layout,
):
    key = layout.set_key(REPO_ROOT / "uv.lock")
    assert key and len(key) == layout.KEY_LENGTH


def test_an_unreadable_lock_has_no_key(layout, tmp_path):
    assert layout.set_key(tmp_path / "absent.lock") is None
    assert layout.project_deps_dir(tmp_path, tmp_path).endswith(
        f"sets/{layout.NO_LOCK}"
    )


def test_deps_dir_and_data_dir_are_inverse(layout, tmp_path):
    deps = layout.deps_dir(tmp_path, "abc")
    assert deps == str(tmp_path / "sets" / "abc")
    assert layout.data_dir(deps) == tmp_path
    assert layout.key_of(deps) == "abc"


def test_the_command_line_prints_the_projects_deps_dir(layout, tmp_path):
    printed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "launcher_layout.py"),
            str(tmp_path),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert printed == layout.project_deps_dir(tmp_path, REPO_ROOT)
