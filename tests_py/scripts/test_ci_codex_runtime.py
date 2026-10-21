"""CI must prepare the wheel before driving the resolved Codex MCP command.

source: docs/verification/codex-hooks-20261002.md
"""

from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]


def _host_step() -> str:
    return (REPO / ".github/ci/codex-runtime.sh").read_text()


def test_native_host_step_is_valid_shell_and_resolves_plugin_command():
    step = _host_step()
    subprocess.run(["bash", "-n"], input=step, text=True, check=True)
    expression = next(
        code
        for code in re.findall(r"python -c '([^']*)'", step)
        if "arg.replace" in code
    )
    output = subprocess.check_output(
        [sys.executable, "-c", expression], cwd=REPO, text=True
    )
    command = output.splitlines()
    assert command == [
        "python3",
        str(REPO / "plugins/hypermnesia-mcp-codex/scripts/runtime.py"),
        "server",
    ]
    assert Path(command[1]).is_file()
    assert "${PLUGIN_ROOT}" not in output


def test_host_step_prepares_candidate_in_same_environment_as_verifier():
    step = _host_step()
    setup = step.index("uv tool install")
    verifier = step.index("python scripts/verify_mcp_hosts.py")
    assert setup < verifier
    for variable in ("UV_CACHE_DIR", "UV_TOOL_DIR", "UV_FIND_LINKS"):
        assert step.index(f"export {variable}=") < setup
    assert "--no-build" in step[setup:verifier]
    assert "hypermnesia-mcp[postgresql,sqlite] @ file://${candidate_wheel}" in step
    assert 's["transport"]["command"]=="python3"' in step


def test_adapter_runs_native_codex_checks_in_e2e():
    adapter = (REPO / ".github/ci/run.sh").read_text()
    e2e = adapter.split("  e2e)", 1)[1].split(";;", 1)[0]
    assert "bash .github/ci/codex-runtime.sh" in e2e
    step = _host_step()
    assert "npm ci --prefix tests_js/mcp-host-clis" in step
    assert '"$cli_bin/codex" plugin add ' in step
    assert '"$cli_bin/codex" mcp list --json' in step
