"""scripts/launcher_base_job.py: the one detached job that installs the base set.

A hook installs nothing; it starts this job (once, whatever the number of
hooks starting together) and exits 1 with the cause.

source: ADR-1108
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def job_mod(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "scripts.launcher_base_job", REPO_ROOT / "scripts" / "launcher_base_job.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def deps_dir(tmp_path: Path) -> str:
    return str(tmp_path / "data" / "sets" / "k1")


@pytest.fixture
def spawned(job_mod, monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(
        job_mod.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw))
    )
    return calls


def test_start_spawns_one_detached_job_logging_beside_the_deps(
    job_mod, deps_dir, spawned
):
    assert job_mod.start(deps_dir, "/plugin/scripts/launcher.py") is True
    ((argv, kwargs),) = spawned
    assert argv == [sys.executable, "/plugin/scripts/launcher.py", job_mod.JOB]
    assert kwargs["stdin"] is job_mod.subprocess.DEVNULL
    assert job_mod.job_log(deps_dir).parent == Path(deps_dir).parent.parent


def test_start_does_nothing_while_the_job_runs(job_mod, deps_dir, spawned):
    with job_mod.launcher_lock.held_if_free(job_mod.job_lock(deps_dir)) as mine:
        assert mine
        assert job_mod.start(deps_dir, "launcher.py") is False
    assert spawned == []


def test_refuse_exits_1_naming_the_cause_and_the_log(
    job_mod, deps_dir, spawned, capsys
):
    with pytest.raises(SystemExit) as stopped:
        job_mod.refuse(deps_dir, "launcher.py", "no psycopg")
    assert stopped.value.code == 1
    err = capsys.readouterr().err
    assert "Cortex not ready: no psycopg" in err
    assert str(job_mod.job_log(deps_dir)) in err
    assert len(spawned) == 1


def test_require_ready_returns_without_a_job_when_the_set_is_stamped(
    job_mod, deps_dir, spawned, monkeypatch
):
    monkeypatch.setattr(job_mod.launcher_deps, "set_ready", lambda d, k: k == "base")
    job_mod.require_ready(deps_dir, "launcher.py")
    assert spawned == []


@pytest.fixture
def ml_started(job_mod, monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(
        job_mod.launcher_ml, "start", lambda d, launcher: calls.append((d, launcher))
    )
    return calls


def test_run_installs_the_base_set_once_then_starts_semantic_search(
    job_mod, deps_dir, ml_started, monkeypatch
):
    order: list = []
    monkeypatch.setattr(
        job_mod.launcher_deps, "ensure_deps", lambda d: order.append("install")
    )
    monkeypatch.setattr(job_mod.launcher_deps, "set_ready", lambda d, k: True)
    assert job_mod.run(deps_dir, "launcher.py") == 0
    assert order == ["install"]
    assert ml_started == [(deps_dir, "launcher.py")]


def test_run_reports_failure_when_the_set_is_still_not_ready(
    job_mod, deps_dir, ml_started, monkeypatch
):
    monkeypatch.setattr(job_mod.launcher_deps, "ensure_deps", lambda d: None)
    monkeypatch.setattr(job_mod.launcher_deps, "set_ready", lambda d, k: False)
    assert job_mod.run(deps_dir, "launcher.py") == 1
    assert ml_started == []


def test_the_semantic_job_starts_after_the_job_lock_is_released(
    job_mod, deps_dir, monkeypatch
):
    held_during_start: list = []

    def start(d, launcher):
        with job_mod.launcher_lock.held_if_free(job_mod.job_lock(d)) as free:
            held_during_start.append(not free)

    monkeypatch.setattr(job_mod.launcher_ml, "start", start)
    monkeypatch.setattr(job_mod.launcher_deps, "ensure_deps", lambda d: None)
    monkeypatch.setattr(job_mod.launcher_deps, "set_ready", lambda d, k: True)
    job_mod.run(deps_dir, "launcher.py")
    assert held_during_start == [False]


def test_a_second_job_returns_without_installing(
    job_mod, deps_dir, ml_started, monkeypatch
):
    monkeypatch.setattr(
        job_mod.launcher_deps,
        "ensure_deps",
        lambda d: pytest.fail("a second job installed"),
    )
    with job_mod.launcher_lock.held_if_free(job_mod.job_lock(deps_dir)):
        assert job_mod.run(deps_dir, "launcher.py") == 0
    assert ml_started == []


def _refused(job_mod, deps_dir, module):
    with pytest.raises(SystemExit) as stopped:
        job_mod.refuse(deps_dir, "launcher.py", "cause", module)
    return stopped.value.code


def test_a_gate_lets_the_edit_through_and_says_the_gate_is_inactive(
    job_mod, deps_dir, spawned, capsys
):
    """Before Cortex is installed there is nothing to guard (owner, 2026-10-15):
    exit 0 and a user-visible systemMessage, on stdout, as the only stdout."""
    for module in ("mcp_server.hooks.decision_gate", "mcp_server.hooks.no_deps_gate"):
        assert _refused(job_mod, deps_dir, module) == 0, module
        out = capsys.readouterr()
        notice = json.loads(out.out)
        assert set(notice) == {"systemMessage"}
        assert "installing its dependencies" in notice["systemMessage"]
        assert "gate is inactive" in notice["systemMessage"]
        assert "Cortex not ready: cause" in out.err


def test_session_start_announces_the_install_to_the_user_and_the_model(
    job_mod, deps_dir, spawned, capsys
):
    assert _refused(job_mod, deps_dir, job_mod.SESSION_START) == 0
    notice = json.loads(capsys.readouterr().out)
    context = notice["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert "memory is available when it is done" in context["additionalContext"]
    assert "about 2 minutes" in notice["systemMessage"]


def test_a_failed_install_is_announced_as_failed_not_as_installing(
    job_mod, deps_dir, spawned, capsys
):
    job_mod._failure_stamp(deps_dir).parent.mkdir(parents=True)
    job_mod._failure_stamp(deps_dir).write_text('{"error": "offline"}')
    assert _refused(job_mod, deps_dir, job_mod.SESSION_START) == 0
    message = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "could not install" in message and "offline" in message


def test_every_other_hook_exits_1_without_stdout(job_mod, deps_dir, spawned, capsys):
    for module in ("mcp_server.hooks.auto_recall", ""):
        assert _refused(job_mod, deps_dir, module) == 1, module
    out = capsys.readouterr()
    assert out.out == ""
    assert "Cortex not ready: cause" in out.err


def test_a_failed_job_is_not_restarted_by_the_next_hook(
    job_mod, deps_dir, ml_started, spawned, monkeypatch, capsys
):
    monkeypatch.setattr(job_mod.launcher_deps, "ensure_deps", lambda d: None)
    monkeypatch.setattr(job_mod.launcher_deps, "set_ready", lambda d, k: False)
    assert job_mod.run(deps_dir, "launcher.py") == 1
    assert job_mod.last_failure(deps_dir)
    for _ in range(3):  # three more hook calls: none starts a job
        with pytest.raises(SystemExit):
            job_mod.refuse(deps_dir, "launcher.py", "cause")
    assert spawned == []
    assert "the last background install failed" in capsys.readouterr().err


def test_a_successful_install_clears_the_failure(
    job_mod, deps_dir, ml_started, monkeypatch
):
    monkeypatch.setattr(job_mod.launcher_deps, "ensure_deps", lambda d: None)
    monkeypatch.setattr(job_mod.launcher_deps, "set_ready", lambda d, k: False)
    job_mod.run(deps_dir, "launcher.py")
    monkeypatch.setattr(job_mod.launcher_deps, "set_ready", lambda d, k: True)
    assert job_mod.run(deps_dir, "launcher.py") == 0
    assert job_mod.last_failure(deps_dir) == ""


def test_start_does_nothing_while_a_setup_run_holds_the_set(job_mod, deps_dir, spawned):
    with job_mod.launcher_lock.held_if_free(job_mod.launcher_setup.job_lock(deps_dir)):
        assert job_mod.start(deps_dir, "launcher.py") is False
    assert spawned == []


def test_the_not_ready_message_names_only_ways_out_that_work_from_any_shell(
    job_mod, deps_dir, spawned, capsys
):
    """A printed `launcher.py --setup` run from a user shell has no
    CLAUDE_PLUGIN_DATA and installs into the plugin tree, where no hook looks:
    the message names `claude --init-only` and the offline way out instead."""
    with pytest.raises(SystemExit):
        job_mod.refuse(deps_dir, "/plugin/scripts/launcher.py", "no psycopg")
    err = capsys.readouterr().err
    assert "`claude --init-only`" in err
    assert f"`claude plugin disable {job_mod.PLUGIN_ID}`" in err
    assert "--setup" not in err
    assert "launcher.py" not in err


def test_the_plugin_id_is_the_manifests_plugin_at_marketplace(job_mod):
    import json

    manifest = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    market = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert job_mod.PLUGIN_ID == f"{manifest['name']}@{market['name']}"


def test_a_corrupt_failure_stamp_is_a_failure_not_none(
    job_mod, deps_dir, spawned, capsys
):
    """Read as "no failure", a corrupt stamp would start a job on every hook."""
    Path(deps_dir).parent.mkdir(parents=True)
    Path(f"{deps_dir}.base-job.failed").write_text("{not json", encoding="utf-8")
    assert "unreadable" in job_mod.last_failure(deps_dir)
    with pytest.raises(SystemExit):
        job_mod.refuse(deps_dir, "launcher.py", "cause")
    assert spawned == []
    assert "unreadable" in capsys.readouterr().err
