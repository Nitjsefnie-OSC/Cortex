"""scripts/init_plugin.py: `claude --init-only`, its progress and a failing verdict.

A fake `claude` stands in for the host: it answers `plugin list --json` and, for
`--init-only`, does what the Setup hook does (append to the log, record the
outcome) and exits 0 whatever happened, as the real host does (measured).

source: ADR-1108
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from tests_py.scripts._host_tree import PLUGIN_ID, plugin_tree

REPO_ROOT = Path(__file__).resolve().parents[2]

_FAKE = """#!{python}
import json, os, sys
if sys.argv[1:] == ["plugin", "list", "--json"]:
    print(json.dumps([{{"id": "{plugin_id}", "installPath": os.environ["FAKE_ROOT"]}}]))
elif sys.argv[1:] == ["--init-only"]:
    if os.environ.get("FAKE_STATUS"):
        os.makedirs(os.path.dirname(os.environ["FAKE_LOG"]), exist_ok=True)
        with open(os.environ["FAKE_LOG"], "a") as log:
            log.write("[cortex-setup] installing the base dependency set\\n")
        os.makedirs(os.path.dirname(os.environ["FAKE_OUTCOME"]), exist_ok=True)
        with open(os.environ["FAKE_OUTCOME"], "w") as out:
            record = {{"status": os.environ["FAKE_STATUS"],
                       "error": os.environ.get("FAKE_ERROR", "")}}
            if os.environ.get("FAKE_GC_ERROR"):
                record["gc_error"] = os.environ["FAKE_GC_ERROR"]
            json.dump(record, out)
sys.exit(0)
"""


@pytest.fixture
def init(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import init_plugin
    import launcher_layout
    import launcher_setup

    root = plugin_tree(tmp_path / "root", "1.0.0")
    config = tmp_path / "config"
    data = init_plugin.data_dir(PLUGIN_ID, {"CLAUDE_CONFIG_DIR": str(config)})
    deps = launcher_layout.project_deps_dir(data, root)
    fake = tmp_path / "claude"
    fake.write_text(_FAKE.format(python=sys.executable, plugin_id=PLUGIN_ID))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {
        "CLAUDE_CONFIG_DIR": str(config),
        "FAKE_ROOT": str(root),
        "FAKE_LOG": str(launcher_setup.log_path(deps)),
        "FAKE_OUTCOME": str(launcher_setup.status_file(deps)),
    }
    return init_plugin, fake, env, deps


def test_the_data_directory_follows_the_hosts_naming(init):
    init_plugin, *_ = init
    assert init_plugin.data_dir(PLUGIN_ID, {"CLAUDE_CONFIG_DIR": "/c"}) == Path(
        "/c/plugins/data/hypermnesia-mcp-cortex-plugins"
    )


def test_a_ready_setup_exits_0_and_its_progress_is_shown(init, capsys):
    init_plugin, fake, env, _ = init
    env["FAKE_STATUS"] = "ready"
    assert init_plugin.init([str(fake)], PLUGIN_ID, env) == 0
    out = capsys.readouterr().out
    assert "installing the base dependency set" in out
    assert "Cortex is ready" in out


def test_a_ready_setup_whose_collection_failed_says_so(init, capsys):
    """The set is ready (exit 0), and the collection's failure is printed."""
    init_plugin, fake, env, _ = init
    env.update(FAKE_STATUS="ready", FAKE_GC_ERROR="PermissionError: sets/")
    assert init_plugin.init([str(fake)], PLUGIN_ID, env) == 0
    captured = capsys.readouterr()
    assert "Cortex is ready" in captured.out
    assert "PermissionError: sets/" in captured.err


def test_a_failed_setup_exits_1_with_its_cause_although_the_host_exits_0(init, capsys):
    init_plugin, fake, env, _ = init
    env.update(FAKE_STATUS="failed", FAKE_ERROR="the model did not download")
    assert init_plugin.init([str(fake)], PLUGIN_ID, env) == 1
    assert "the model did not download" in capsys.readouterr().err


def test_a_setup_hook_that_never_ran_is_not_ready(init, capsys):
    init_plugin, fake, env, _ = init  # the fake writes nothing: no FAKE_STATUS
    assert init_plugin.init([str(fake)], PLUGIN_ID, env) == 1
    assert "did not run" in capsys.readouterr().err


def test_a_plugin_that_is_not_installed_is_reported(init, capsys):
    init_plugin, fake, env, _ = init
    assert init_plugin.init([str(fake)], "other@market", env) == 2
    assert "not installed" in capsys.readouterr().out
