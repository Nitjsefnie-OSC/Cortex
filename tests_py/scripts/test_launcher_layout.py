"""scripts/launcher_layout.py: the key names a dependency set, not a release.

source: ADR-1108
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tests_py.scripts._host_tree import write_selection

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def layout(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_layout

    return launcher_layout


def _tree(
    tmp_path: Path,
    name: str,
    version: str = "4.23.5",
    dep_hash: str = "aaa",
    python: str = "3.12.13",
    base: str = "launcher-base",
) -> Path:
    """A plugin tree: its uv.lock and the selection it declares."""
    tree = tmp_path / name
    tree.mkdir()
    (tree / "uv.lock").write_text(
        '[[package]]\nname = "hypermnesia-mcp"\n'
        f'version = "{version}"\nsource = {{ editable = "." }}\n\n'
        f'[[package]]\nname = "numpy"\nversion = "2.4.4"\nhash = "{dep_hash}"\n'
    )
    write_selection(tree, python=python, base=base)
    return tree


def test_two_releases_with_one_dependency_set_share_a_key(layout, tmp_path):
    assert layout.set_key(_tree(tmp_path, "a", "4.23.4")) == layout.set_key(
        _tree(tmp_path, "b", "4.23.5")
    )


def test_a_changed_dependency_is_a_new_key(layout, tmp_path):
    assert layout.set_key(_tree(tmp_path, "a", dep_hash="aaa")) != layout.set_key(
        _tree(tmp_path, "b", dep_hash="bbb")
    )


def test_a_changed_install_selection_with_one_lock_is_a_new_key(layout, tmp_path):
    """A release that installs another group from the same uv.lock must not
    reuse (and then rewrite in place) the set a running session imports from."""
    assert layout.set_key(_tree(tmp_path, "a")) != layout.set_key(
        _tree(tmp_path, "b", base="launcher-base-v2")
    )


def test_a_changed_pinned_python_with_one_lock_is_a_new_key(layout, tmp_path):
    assert layout.set_key(_tree(tmp_path, "a")) != layout.set_key(
        _tree(tmp_path, "b", python="3.13.1")
    )


def test_the_key_does_not_depend_on_the_running_interpreter(
    layout, tmp_path, monkeypatch
):
    """System 3.9 starts a launch and re-executes onto the managed 3.12: one key."""
    tree = _tree(tmp_path, "a")
    before = layout.set_key(tree)
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))
    assert layout.set_key(tree) == before


def test_only_the_projects_own_version_line_is_ignored(layout, tmp_path):
    other = _tree(tmp_path, "other")
    (other / "uv.lock").write_text(
        '[[package]]\nname = "numpy"\nversion = "1"\n\n'
        '[[package]]\nname = "hypermnesia-mcp"\nversion = "9"\n'
    )
    changed = _tree(tmp_path, "changed")
    (changed / "uv.lock").write_text(
        (other / "uv.lock").read_text().replace('version = "1"', 'version = "2"')
    )
    assert layout.set_key(other) != layout.set_key(changed)


def test_the_repositorys_own_tree_has_a_key_and_the_key_has_the_declared_length(
    layout,
):
    key = layout.set_key(REPO_ROOT)
    assert key and len(key) == layout.KEY_LENGTH


def test_the_selection_is_read_without_running_the_tree(layout, tmp_path):
    tree = _tree(tmp_path, "a")
    (tree / "scripts" / "launcher_sets.py").write_text(
        "import os\nos._exit(7)\n"
        'BASE = ("x",)\nML = ("y",)\nMANAGED_PYTHON = "3.12.1"\n'
    )
    assert layout.selection(tree) == {
        "BASE": ("x",),
        "ML": ("y",),
        "MANAGED_PYTHON": "3.12.1",
    }


def test_a_tree_without_a_readable_lock_or_selection_has_no_key(layout, tmp_path):
    assert layout.set_key(tmp_path / "absent") is None
    no_selection = _tree(tmp_path, "a")
    (no_selection / "scripts" / "launcher_sets.py").write_text("BASE = ()\n")
    assert layout.set_key(no_selection) is None
    assert layout.project_deps_dir(tmp_path, tmp_path / "absent").endswith(
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
