#!/usr/bin/env bash
# source: .github/workflows/ci.yml test-windows at 33aad87a, ADR-1091/1094/1096/1102/1108.
# Keep the existing native Windows probes inside the environment category.
set -euo pipefail
export CORTEX_MEMORY_STORE_BACKEND=sqlite
python -c 'import mcp_server.shared.platform, mcp_server.infrastructure.pipeline_install_lock, mcp_server.infrastructure.pipeline_discovery, mcp_server.core.staleness, mcp_server.doctor, mcp_server.doctor_mcp'
work="$(mktemp -d "${RUNNER_TEMP:?}/cortex-windows-install.XXXXXX")"
trap 'rm -rf "$work"' EXIT
export CLAUDE_PLUGIN_ROOT="$PWD" CLAUDE_PLUGIN_DATA="$work/data"
(
  # Existing cold-install test: the installer must bootstrap its pinned uv.
  while uv_bin="$(command -v uv)"; do
    uv_dir="$(dirname "$uv_bin")"
    stripped="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$uv_dir" | paste -sd: -)"
    [ "$stripped" != "$PATH" ] || { echo "cannot drop $uv_dir from PATH" >&2; exit 1; }
    PATH="$stripped"
  done
  HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 CORTEX_RERANKER_OFFLINE=0 bash scripts/install-plugin.sh
)
python -c 'import glob, os; data = os.environ["CLAUDE_PLUGIN_DATA"]; found = [f for directory in ("bin", "Scripts") for f in glob.glob(os.path.join(data, "uv-*", directory, "uv*"))]; assert found, "installer did not bootstrap uv"; print(found)'
python scripts/verify_sqlite_extra_parity.py "$CLAUDE_PLUGIN_DATA"
python -m pytest --tb=short -q \
  tests_py/shared/test_platform.py \
  tests_py/infrastructure/test_pipeline_discovery.py \
  tests_py/infrastructure/test_pipeline_install_lock.py \
  tests_py/core/test_staleness.py \
  tests_py/infrastructure/test_sqlite_backend.py \
  tests_py/scripts/test_setup_py_backend_skip.py \
  tests_py/scripts/test_launcher_site.py \
  tests_py/scripts/test_launcher_site_paths.py \
  tests_py/infrastructure/test_semantic_fallback_169.py \
  tests_py/test_doctor.py \
  tests_py/handlers/consolidation/test_wiki_maintenance.py \
  tests_py/handlers/consolidation/test_wiki_backlog_pass.py \
  tests_py/hooks/test_capture_unsupported_platform.py \
  tests_py/hooks/test_capture_drain.py \
  tests_py/infrastructure/test_capture_spool.py \
  tests_py/shared/test_win32_process.py \
  tests_py/shared/test_win32_process_logic.py \
  tests_py/shared/test_process_liveness.py \
  tests_py/infrastructure/test_process_ancestry.py \
  tests_py/infrastructure/test_session_registry_windows.py \
  tests_py/shared/test_detached_spawn.py \
  tests_py/shared/test_win32_job.py \
  tests_py/hooks/test_codex_windows_job.py
