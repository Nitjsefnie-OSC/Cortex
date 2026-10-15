"""scripts/launcher_maintain.py: the next version's set is installed ahead.

The seams are the host's records on disk and ``subprocess``; no wall clock.

source: ADR-1108
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests_py.scripts._host_tree import Host

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_layout
    import launcher_maintain

    return launcher_maintain, launcher_layout


@pytest.fixture
def spawned(mods, monkeypatch) -> list:
    maintain, _ = mods
    calls: list = []
    monkeypatch.setattr(
        maintain.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw))
    )
    return calls


def _deps(layout, host: Host) -> str:
    return layout.project_deps_dir(host.data, host.running)


def test_a_newer_version_with_a_new_set_is_planned(mods, tmp_path):
    maintain, _ = mods
    host = Host(tmp_path)
    clone = host.add_clone("1.1.0", "scipy")
    plan = maintain.plan(str(host.running), str(host.data))
    assert plan.install == clone


def test_the_job_is_started_by_a_hook_and_installs_nothing_itself(
    mods, tmp_path, spawned
):
    maintain, layout = mods
    host = Host(tmp_path)
    host.add_clone("1.1.0", "scipy")
    started = maintain.start_if_needed(
        str(host.running), _deps(layout, host), "launcher.py"
    )
    assert started is True
    ((argv, kwargs),) = spawned
    assert argv == [sys.executable, "launcher.py", maintain.JOB]
    assert kwargs["env"]["CORTEX_RUNNING_ROOT"] == str(host.running)
    assert kwargs["start_new_session"] is True


def test_the_job_runs_the_clones_own_launcher_setup_for_its_data(
    mods, tmp_path, monkeypatch
):
    maintain, layout = mods
    host = Host(tmp_path)
    clone = host.add_clone("1.1.0", "scipy")
    runs: list = []

    def fake_run(argv, **kwargs):
        runs.append((argv, kwargs["env"]))
        # what the clone's Setup run leaves: both stamps under ITS key
        host.make_set(layout.set_key(clone))

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(maintain.subprocess, "run", fake_run)
    assert maintain.run(str(host.running), str(host.data)) == 0
    ((argv, env),) = runs
    assert argv == [sys.executable, str(clone / "scripts" / "launcher.py"), "--setup"]
    assert env["CLAUDE_PLUGIN_ROOT"] == str(clone)
    assert env["CLAUDE_PLUGIN_DATA"] == str(host.data)
    assert env["CORTEX_RUNNING_ROOT"] == str(host.running)
    # the set is stamped under the clone's digest: nothing is left to install
    assert maintain.plan(str(host.running), str(host.data)).install is None


def test_a_stamped_next_set_starts_no_job(mods, tmp_path, spawned):
    maintain, layout = mods
    host = Host(tmp_path)
    clone = host.add_clone("1.1.0", "scipy")
    host.make_set(layout.set_key(clone))
    host.make_set(layout.set_key(host.running))
    assert (
        maintain.start_if_needed(str(host.running), _deps(layout, host), "l.py")
        is False
    )
    assert spawned == []


def test_the_same_version_or_the_same_set_needs_nothing(mods, tmp_path, spawned):
    maintain, layout = mods
    host = Host(tmp_path)
    host.add_clone("1.0.0", "scipy")  # same version: not an update
    assert maintain.plan(str(host.running), str(host.data)).install is None
    host.add_clone("1.1.0", "numpy")  # new version, same dependency set
    host.make_set(layout.set_key(host.running))
    assert maintain.plan(str(host.running), str(host.data)).empty()


def test_a_clone_whose_plugin_lives_elsewhere_is_not_a_source(mods, tmp_path):
    maintain, _ = mods
    host = Host(tmp_path)
    clone = host.add_clone("1.1.0", "scipy")
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        '{"plugins": [{"name": "hypermnesia-mcp", "source": {"source": "github"}}]}'
    )
    assert maintain.plan(str(host.running), str(host.data)).install is None


def test_an_unknown_install_is_no_work(mods, tmp_path, spawned):
    maintain, layout = mods
    host = Host(tmp_path)
    host.add_clone("1.1.0", "scipy")
    stranger = tmp_path / "elsewhere"
    stranger.mkdir()
    assert maintain.plan(str(stranger), str(host.data)).empty()
    assert spawned == []


def test_a_second_job_returns_at_once(mods, tmp_path, monkeypatch):
    maintain, _ = mods
    host = Host(tmp_path)
    host.add_clone("1.1.0", "scipy")
    monkeypatch.setattr(
        maintain.subprocess, "run", lambda *a, **k: pytest.fail("a second job ran")
    )
    with maintain.launcher_lock.held_if_free(maintain.job_lock(str(host.data))):
        assert maintain.run(str(host.running), str(host.data)) == 0


def test_with_nothing_to_install_the_job_collects_old_sets(mods, tmp_path):
    maintain, _ = mods
    host = Host(tmp_path)
    old = host.make_set("old0000000000000")
    assert maintain.run(str(host.running), str(host.data)) == 0
    assert not old.exists()
