"""scripts/launcher_remote.py: the next release's set is found at the git remote.

A real local git remote stands in for GitHub: the marketplace clone is a real
clone of it, the next release is a real commit on its branch, and the fetch is a
real sparse, shallow, partial fetch. Nothing is mocked but the Setup run.

source: ADR-1108
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tests_py.scripts._host_tree import MARKETPLACE, PLUGIN, Host, plugin_tree

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _release(origin: Path, version: str, dep: str) -> None:
    """Commit a plugin tree of ``version`` locking ``dep`` on origin's branch."""
    plugin_tree(origin, version, dep)
    (origin / "mcp_server").mkdir(exist_ok=True)
    (origin / "mcp_server" / "__init__.py").write_text("")
    (origin / "wiki.md").write_text(version)  # outside the sparse paths
    (origin / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": MARKETPLACE, "plugins": [{"name": PLUGIN, "source": "./"}]})
    )
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", version)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_gc
    import launcher_installed
    import launcher_layout
    import launcher_maintain
    import launcher_remote

    host = Host(tmp_path)  # running 1.0.0
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "uploadpack.allowFilter", "true")
    _release(origin, "1.0.0", "numpy")
    clone = host.plugins / "marketplaces" / MARKETPLACE
    clone.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    host.clone = clone
    host.write_records()

    class World:
        pass

    w = World()
    w.host, w.origin = host, origin
    w.gc, w.layout, w.remote, w.maintain = (
        launcher_gc,
        launcher_layout,
        launcher_remote,
        launcher_maintain,
    )
    w.installed = launcher_installed.find(str(host.running), str(host.data))
    return w


def _prepare(w):
    return w.remote.prepare(w.installed, str(w.host.data), str(w.host.running))


def test_a_new_release_on_the_remote_is_fetched_sparsely_and_recorded(world):
    _release(world.origin, "1.1.0", "scipy")
    tree = _prepare(world)
    assert tree is not None and (tree / "uv.lock").is_file()
    assert not (tree / "wiki.md").exists()  # only the files that define a set
    state = world.remote.read_state(str(world.host.data))
    assert state["version"] == "1.1.0"
    assert state["key"] == world.layout.set_key(tree)
    assert state["sha"] == _git(world.origin, "rev-parse", "HEAD")


def test_nothing_is_fetched_while_the_clone_is_at_the_remote_head(world, monkeypatch):
    monkeypatch.setattr(
        world.remote, "fetch", lambda *a: pytest.fail("fetched with nothing new")
    )
    assert _prepare(world) is None
    assert not (world.host.data / world.remote.TREE).exists()


def test_the_same_remote_head_is_not_fetched_twice(world):
    _release(world.origin, "1.1.0", "scipy")
    assert _prepare(world) is not None
    world.remote.discard(str(world.host.data))
    assert _prepare(world) is None


def test_a_remote_head_with_the_running_version_installs_nothing(world):
    _release(world.origin, "1.0.0", "scipy")  # a commit, not a release
    assert _prepare(world) is None
    assert not (world.host.data / world.remote.TREE).exists()


def test_an_unreachable_remote_is_nothing_to_do(world):
    _git(world.host.clone, "remote", "set-url", "origin", "/nonexistent/remote")
    assert _prepare(world) is None
    assert world.remote.read_state(str(world.host.data)) == {}


def test_a_set_preinstalled_from_the_remote_survives_collect(world):
    """The pre-installed set is in no installed copy and not in the clone: only
    the record references it, and Setup collects right after it installs."""
    _release(world.origin, "1.1.0", "scipy")
    tree = _prepare(world)
    key = world.layout.set_key(tree)
    world.host.make_set(key)
    world.host.make_set(world.layout.set_key(world.host.running))
    world.host.make_set("stale0000000000a")
    world.gc.collect(str(world.host.running), str(world.host.data))
    left = sorted(p.name for p in (world.host.data / "sets").iterdir())
    assert left == sorted([key, world.layout.set_key(world.host.running)])


def test_the_record_is_spent_once_an_installed_copy_reaches_its_version(world):
    _release(world.origin, "1.1.0", "scipy")
    key = world.layout.set_key(_prepare(world))
    world.host.make_set(key)
    updated = plugin_tree(
        world.host.plugins / "cache" / MARKETPLACE / PLUGIN / "1.1.0", "1.1.0", "scipy"
    )
    world.host.installed.append(updated)
    world.host.write_records()
    world.gc.collect(str(updated), str(world.host.data))
    assert (world.host.data / "sets" / key).exists()  # the installed copy's own key
    world.host.installed.remove(updated)
    world.host.installed.append(
        plugin_tree(
            world.host.plugins / "cache" / MARKETPLACE / PLUGIN / "1.2.0",
            "1.2.0",
            "pandas",
        )
    )
    world.host.write_records()
    newest = world.host.installed[-1]
    world.gc.collect(str(newest), str(world.host.data))
    assert not (world.host.data / "sets" / key).exists()


def test_the_job_runs_setup_on_the_fetched_tree_then_removes_it(world, monkeypatch):
    _release(world.origin, "1.1.0", "scipy")
    ran: list = []

    def fake_setup(tree, running_root, data):
        ran.append((Path(tree).name, (Path(tree) / "uv.lock").is_file(), data))
        return 0

    monkeypatch.setattr(world.maintain, "_setup", fake_setup)
    monkeypatch.setenv(world.maintain.REMOTE_ENV, "1")
    code = world.maintain.run(str(world.host.running), str(world.host.data))
    assert code == 0
    assert ran == [(world.remote.TREE, True, str(world.host.data))]
    assert not (world.host.data / world.remote.TREE).exists()


def test_the_job_does_not_ask_the_remote_unless_a_session_started(world, monkeypatch):
    _release(world.origin, "1.1.0", "scipy")
    monkeypatch.setattr(
        world.maintain, "_setup", lambda *a: pytest.fail("no remote check")
    )
    monkeypatch.delenv(world.maintain.REMOTE_ENV, raising=False)
    assert world.maintain.run(str(world.host.running), str(world.host.data)) == 0
    assert world.remote.read_state(str(world.host.data)) == {}
