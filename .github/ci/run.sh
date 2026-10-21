#!/usr/bin/env bash
# Source: .github/actions/test-suite, scripts/install_e2e and ADR-1114.
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$PWD/.venv/bin:$PATH"
if [ "${RUNNER_OS:-Linux}" = Windows ]; then
  export PATH="$PWD/.venv/Scripts:$PATH"
fi
export CI=true
export CORTEX_MEMORY_STORE_BACKEND="${CORTEX_MEMORY_STORE_BACKEND:-postgresql}"
# source: .github/actions/test-suite/action.yml offline test environment.
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 CORTEX_RERANKER_OFFLINE=1
case "${1:?phase required}" in
  setup)
    if [ "${2:-}" = environment ]; then
      if [ "${RUNNER_OS:-Linux}" = Windows ]; then
        uv sync --locked --no-default-groups --extra dev --extra sqlite
        HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c 'from sentence_transformers import SentenceTransformer; SentenceTransformer("all-MiniLM-L6-v2", device="cpu")'
      else
        uv sync --locked --only-group launcher-base
      fi
      exit 0
    fi
    uv sync --locked --no-default-groups --extra dev --extra postgresql --extra sqlite --extra codebase
    python .github/ci/require_pg.py
    HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c 'from sentence_transformers import SentenceTransformer; SentenceTransformer("all-MiniLM-L6-v2", device="cpu")'
    CORTEX_RERANKER_OFFLINE=0 python -c 'from mcp_server.doctor import ensure_reranker_ready; status = ensure_reranker_ready(); assert status.state == "loaded", status'
    python -c 'from mcp_server.core.ast_parser import AST_SUPPORTED; from tree_sitter_language_pack import prefetch; prefetch(sorted(AST_SUPPORTED))'
    ;;
  unit)
    python .github/ci/require_pg.py
    # Preserve the established coverage floor with one complete Python run.
    python -m pytest --tb=short -q --cov=mcp_server --cov-report=xml --cov-report=term-missing --cov-fail-under=82
    ;;
  regression)
    python -m unittest discover -s .github/shared-ci -p 'test_*.py'
    CORTEX_MEMORY_STORE_BACKEND=sqlite python -m pytest --tb=short -q tests_py/infrastructure/test_sqlite_backend.py
    ;;
  e2e)
    python .github/ci/require_pg.py
    python -m pytest --tb=short -q tests_py/integration/test_recall_e2e.py
    CORTEX_RUNTIME=cowork python scripts/verify_mcp_hosts.py --clients claude-code codex-cli --profiles lean --storage-selection auto -- hypermnesia-mcp
    bash .github/ci/codex-runtime.sh
    ;;
  environment)
    if [ "${RUNNER_OS:-Linux}" = Windows ]; then
      bash .github/ci/windows-environment.sh
      exit 0
    fi
    work="$(mktemp -d "${RUNNER_TEMP:?}/cortex-managed-pg.XXXXXX")"
    trap 'rm -rf "$work"' EXIT
    mkdir -p "$work/home"
    locked=()
    if [ "${RUNNER_OS:-Linux}" = macOS ]; then
      locked=(sandbox-exec -f scripts/locked_mac_env/no_homebrew.sb)
    fi
    "${locked[@]}" env -i HOME="$work/home" PATH=/usr/bin:/bin:/usr/sbin:/sbin TMPDIR="$work" LANG=en_US.UTF-8 \
      "$(command -v python)" -I scripts/locked_mac_env/managed_pg_e2e.py "$PWD" "$work"
    if [ "${RUNNER_OS:-Linux}" = macOS ]; then
      # source: ci.yml install-e2e macOS ARM and Rosetta legs, ADR-1104.
      /usr/bin/python3 scripts/install_e2e/run.py --marketplace "$PWD" --work "$work/install-arm" --python /usr/bin/python3 --settle-seconds 1500
      softwareupdate --install-rosetta --agree-to-license || arch -x86_64 /usr/bin/true
      /usr/bin/python3 scripts/install_e2e/run.py --marketplace "$PWD" --work "$work/install-x86" --python /usr/bin/python3 --settle-seconds 1500 --x86_64
    fi
    ;;
  installation)
    work="$(mktemp -d "${RUNNER_TEMP:?}/cortex-install.XXXXXX")"
    trap 'rm -rf "$work"' EXIT
    # source: ci.yml install-e2e uses the clean machine's Python and this bound.
    /usr/bin/python3 scripts/install_e2e/run.py --marketplace "$PWD" --work "$work/install" --python /usr/bin/python3 --settle-seconds 1500
    ;;
  release-version)
    python -c 'import tomllib; v=tomllib.load(open("pyproject.toml", "rb"))["project"]["version"]; print("version="+v); print("tag=v"+v)'
    ;;
  *) echo "Unknown phase: $1" >&2; exit 2 ;;
esac
