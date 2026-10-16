"""scripts/launcher_gc.py: a set goes when no installed version references it.

source: ADR-1108
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from tests_py.scripts._host_tree import MARKETPLACE, PLUGIN, Host, plugin_tree

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_gc
    import launcher_layout

    return launcher_gc, launcher_layout


def _key(layout, tree: Path) -> str:
    return layout.set_key(tree)


def test_referenced_sets_stay_and_the_rest_goes(mods, tmp_path):
    gc, layout = mods
    host = Host(tmp_path)  # running 1.0.0 locks numpy
    clone = host.add_clone("1.1.0", "scipy")  # the next version locks scipy
    running_key, next_key = _key(layout, host.running), _key(layout, clone)
    for key in (running_key, next_key, "stale0000000000a"):
        host.make_set(key)
    (host.data / "sets" / "stale0000000000a.install.lock").write_text("")
    (host.data / "deps").mkdir()
    removed = gc.collect(str(host.running), str(host.data))
    left = sorted(p.name for p in (host.data / "sets").iterdir())
    assert left == sorted([running_key, next_key])
    assert len(removed) == 3  # the directory, its sidecar and the legacy deps
    assert not (host.data / "deps").exists()


def test_a_set_with_a_live_user_is_kept(mods, tmp_path):
    gc, _ = mods
    host = Host(tmp_path)
    old = host.make_set("old0000000000000")
    (Path(f"{old}.users")).mkdir()
    (Path(f"{old}.users") / str(os.getpid())).write_text("")
    gc.collect(str(host.running), str(host.data))
    assert old.exists()


def test_a_dead_user_does_not_keep_a_set(mods, tmp_path):
    gc, _ = mods
    host = Host(tmp_path)
    old = host.make_set("old0000000000000")
    (Path(f"{old}.users")).mkdir()
    (Path(f"{old}.users") / "2147483646").write_text("")  # no such process
    gc.collect(str(host.running), str(host.data))
    assert not old.exists()


def test_the_flat_layout_stays_until_the_running_set_is_installed(mods, tmp_path):
    gc, layout = mods
    host = Host(tmp_path)
    (host.data / "deps").mkdir()
    host.make_set("old0000000000000")
    gc.collect(str(host.running), str(host.data))
    assert (host.data / "deps").exists()  # the running set is not stamped yet
    host.make_set(_key(layout, host.running), stamps=("base",))
    gc.collect(str(host.running), str(host.data))
    assert not (host.data / "deps").exists()


def _flat_tree(host, version="0.9.0"):
    """A cached copy of the release before per-lock sets: it imports from deps/."""
    return plugin_tree(
        host.plugins / "cache" / MARKETPLACE / PLUGIN / version,
        version,
        flat=True,
    )


def _ready_host(tmp_path, layout):
    host = Host(tmp_path)
    (host.data / "deps").mkdir()
    host.make_set(layout.set_key(host.running), stamps=("base",))
    return host


def test_the_flat_layout_stays_while_an_old_copy_is_cached(mods, tmp_path):
    """An old server cannot register as a user; its cached tree is the proof."""
    gc, layout = mods
    host = _ready_host(tmp_path, layout)
    _flat_tree(host)  # not in installed_plugins.json, still in the cache
    assert gc.collect(str(host.running), str(host.data)) == []
    assert (host.data / "deps").exists()


def test_the_flat_layout_stays_while_an_old_copy_is_installed(mods, tmp_path):
    gc, layout = mods
    host = _ready_host(tmp_path, layout)
    host.installed.append(
        plugin_tree(tmp_path / "elsewhere" / "0.8.0", "0.8.0", flat=True)
    )
    host.write_records()
    gc.collect(str(host.running), str(host.data))
    assert (host.data / "deps").exists()


def test_the_flat_layout_goes_once_the_old_copy_is_gone(mods, tmp_path):
    gc, layout = mods
    host = _ready_host(tmp_path, layout)
    old = _flat_tree(host)
    gc.collect(str(host.running), str(host.data))
    assert (host.data / "deps").exists()
    shutil.rmtree(old)
    gc.collect(str(host.running), str(host.data))
    assert not (host.data / "deps").exists()


def test_the_flat_layout_stays_when_the_cache_cannot_be_listed(mods, tmp_path):
    gc, layout = mods
    host = _ready_host(tmp_path, layout)
    cache = host.plugins / "cache" / MARKETPLACE / PLUGIN
    cache.chmod(0o111)  # traversable, not listable
    try:
        if os.access(cache, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        gc.collect(str(host.running), str(host.data))
    finally:
        cache.chmod(0o755)
    assert (host.data / "deps").exists()


def test_nothing_is_removed_when_the_host_records_are_unknown(mods, tmp_path, capsys):
    gc, _ = mods
    host = Host(tmp_path)
    old = host.make_set("old0000000000000")
    record = host.plugins / "installed_plugins.json"
    record.write_text("not json")
    assert gc.collect(str(host.running), str(host.data)) == []
    assert old.exists()
    assert f"could not read {record}" in capsys.readouterr().err


def test_nothing_is_removed_when_a_referenced_tree_has_no_lock(mods, tmp_path):
    gc, _ = mods
    host = Host(tmp_path)
    old = host.make_set("old0000000000000")
    (host.running / "uv.lock").unlink()
    assert gc.collect(str(host.running), str(host.data)) == []
    assert old.exists()


def test_mark_in_use_registers_this_process_until_it_exits(mods, tmp_path):
    gc, _ = mods
    deps = str(tmp_path / "sets" / "k")
    assert gc.in_use(deps) is False
    gc.mark_in_use(deps)
    assert gc.in_use(deps) is True


def _undeletable_set(host, key="old0000000000000"):
    """A set whose content this user cannot unlink: rmtree raises inside it."""
    old = host.make_set(key)
    locked = old / "pkg"
    locked.mkdir()
    (locked / "module.py").write_text("")
    locked.chmod(0o555)
    if os.access(locked, os.W_OK):
        locked.chmod(0o755)
        pytest.skip("permissions are not enforced for this user")
    return old, locked


def test_a_set_that_cannot_be_removed_is_reported_not_declared_removed(
    mods, tmp_path, capsys
):
    """rmtree(ignore_errors=True) used to report the set removed while it stayed."""
    gc, _ = mods
    host = Host(tmp_path)
    old, locked = _undeletable_set(host)
    try:
        removed = gc.collect(str(host.running), str(host.data))
        assert old.exists()
        assert str(old) not in removed
        assert str(old) in gc.failed_removals(str(host.data))
        assert str(old) in capsys.readouterr().err
    finally:
        locked.chmod(0o755)


def test_a_later_collection_that_removes_it_clears_the_record(mods, tmp_path):
    gc, _ = mods
    host = Host(tmp_path)
    old, locked = _undeletable_set(host)
    try:
        gc.collect(str(host.running), str(host.data))
    finally:
        locked.chmod(0o755)
    assert str(old) in gc.collect(str(host.running), str(host.data))
    assert not old.exists()
    assert gc.failed_removals(str(host.data)) == {}


def test_a_user_registry_that_cannot_be_listed_removes_nothing(mods, tmp_path):
    """Unknown users are not "no users": the set a live server imports from stays."""
    gc, _ = mods
    host = Host(tmp_path)
    old = host.make_set("old0000000000000")
    users = Path(f"{old}.users")
    users.mkdir()
    users.chmod(0o000)
    try:
        if os.access(users, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        with pytest.raises(PermissionError):
            gc.collect(str(host.running), str(host.data))
        assert old.exists()
    finally:
        users.chmod(0o755)


def test_a_sets_directory_that_cannot_be_listed_removes_nothing(mods, tmp_path):
    """An unlistable sets/ is not an empty one: the collection stops with the cause."""
    gc, _ = mods
    host = Host(tmp_path)
    old = host.make_set("old0000000000000")
    sets = host.data / "sets"
    sets.chmod(0o000)
    try:
        if os.access(sets, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        with pytest.raises(PermissionError):
            gc.collect(str(host.running), str(host.data))
        with pytest.raises(PermissionError):
            gc.has_new_work(str(host.data), {"anything"})
    finally:
        sets.chmod(0o755)
    assert old.exists()
