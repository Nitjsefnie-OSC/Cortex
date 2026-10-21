#!/usr/bin/env bash
# source: ci.yml mcp-host-config at 33aad87a and ADR-1084.
# Actual vendor CLI parsing/installation plus the candidate runtime command.
set -euo pipefail
cd "$(dirname "$0")/../.."
work="$(mktemp -d "${RUNNER_TEMP:?}/cortex-hosts.XXXXXX")"
trap 'rm -rf "$work"' EXIT
export CODEX_HOME="$work/codex"
mkdir -p "$CODEX_HOME"
npm ci --prefix tests_js/mcp-host-clis
uv build --wheel --out-dir "$work/wheel"
cli_bin="tests_js/mcp-host-clis/node_modules/.bin"
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  "$cli_bin/claude" plugin validate .
GEMINI_CLI_HOME="$work/gemini" \
  "$cli_bin/gemini" extensions validate .
"$cli_bin/codex" mcp list --json \
  -c 'mcp_servers={cortex={command="hypermnesia-mcp",startup_timeout_sec=30,tool_timeout_sec=600,env={CORTEX_MEMORY_STORE_BACKEND="sqlite"}}}' \
  | python -c 'import json,sys; servers={s["name"]:s for s in json.load(sys.stdin)}; c=servers["cortex"]; assert c["transport"]["type"]=="stdio"; assert c["transport"]["command"]=="hypermnesia-mcp"; assert c["transport"]["env"]["CORTEX_MEMORY_STORE_BACKEND"]=="sqlite"; assert c["startup_timeout_sec"]==30.0; assert c["tool_timeout_sec"]==600.0'
marketplace_ref="${GITHUB_HEAD_REF:-$GITHUB_REF_NAME}"
"$cli_bin/codex" plugin marketplace add cdeust/Cortex \
  --ref "$marketplace_ref" --json
"$cli_bin/codex" plugin list --available --json \
  | python -c 'import json,sys; d=json.load(sys.stdin); expected=json.load(open("package.json"))["version"]; ps=d.get("available",[])+d.get("installed",[]); p=[x for x in ps if x.get("pluginId")=="hypermnesia-mcp-codex@cortex-codex-plugins"]; assert len(p)==1, p; assert p[0]["version"]==expected'
"$cli_bin/codex" plugin add hypermnesia-mcp-codex@cortex-codex-plugins --json
"$cli_bin/codex" plugin list --json \
  | python -c 'import json,sys; d=json.load(sys.stdin); p=[x for x in d.get("installed",[]) if x.get("pluginId")=="hypermnesia-mcp-codex@cortex-codex-plugins"]; assert len(p)==1, p; assert p[0]["installed"] and p[0]["enabled"]'
"$cli_bin/codex" mcp list --json \
  | python -c 'import json,sys; ss=[s for s in json.load(sys.stdin) if s.get("name")=="cortex"]; assert len(ss)==1, ss; s=ss[0]; assert s["startup_timeout_sec"]==180.0, s; assert s["transport"]["command"]=="python3", s; assert s["transport"]["env"]["CORTEX_RUNTIME"]=="cowork", s'
mapfile -t plugin_command < <(
  # PLUGIN_ROOT is a literal Codex placeholder resolved by Python.
  # shellcheck disable=SC2016
  python -c 'import json; from pathlib import Path; root=str(Path("plugins/hypermnesia-mcp-codex").resolve()); s=json.load(open(Path(root)/".mcp.json"))["mcpServers"]["cortex"]; print(s["command"]); print(*(arg.replace("${PLUGIN_ROOT}",root) for arg in s["args"]), sep="\n")'
)
plugin_timeout="$(
  python -c 'import json; print(json.load(open("plugins/hypermnesia-mcp-codex/.mcp.json"))["mcpServers"]["cortex"]["startup_timeout_sec"])'
)"
plugin_runtime="$(
  python -c 'import json; print(json.load(open("plugins/hypermnesia-mcp-codex/.mcp.json"))["mcpServers"]["cortex"]["env"]["CORTEX_RUNTIME"])'
)"
candidate_wheel="$(find "$work/wheel" -name '*.whl' -print -quit)"
# source: docs/verification/codex-hooks-20261002.md
# Prepare the candidate explicitly; event startup never installs it.
export UV_FIND_LINKS="$work/wheel"
export UV_CACHE_DIR="$work/uv-cache"
export UV_TOOL_DIR="$work/uv-tools"
uv tool install --python "$(command -v python3)" --no-build \
  "hypermnesia-mcp[postgresql,sqlite] @ file://${candidate_wheel}"
# The verifier and manifest runtime inherit the same prepared paths.
uv run --no-project --with "$candidate_wheel" -- env \
  CORTEX_RUNTIME="$plugin_runtime" \
  UV_FIND_LINKS="$work/wheel" \
  PYTHONPATH="$GITHUB_WORKSPACE" \
  python scripts/verify_mcp_hosts.py \
    --timeout "$plugin_timeout" --clients codex-cli --profiles full \
    --command-includes-profile \
    --allow-bootstrap-network \
    --storage-selection auto \
    -- "${plugin_command[@]}"
