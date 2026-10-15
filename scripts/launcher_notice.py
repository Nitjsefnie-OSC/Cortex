"""What Cortex tells the user while its dependencies install — stdlib only.

One sentence, said by the hooks that announce the install (``launcher_base_job``)
and by the first-launch MCP stand-in (``launcher_mcp_wire``), so a user who sees
it in two places reads the same words and the same expectation.

source: ADR-1108
"""

from __future__ import annotations

# How long the install takes.
# source: measured 2026-10-14/15 (ADR-1104; fleet/deps-out-of-hook/real, throwaway
# config): the base set 28-109 s with uv and Python, everything with the model 307 s
ETA = "about 2 minutes, about 5 with semantic search"

INSTALLING = (
    f"Cortex is installing its dependencies ({ETA}); "
    "memory is available when it is done"
)
