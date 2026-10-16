"""Which uv.lock set each launcher path installs — stdlib only, no versions.

Versions live in uv.lock alone; these are only the ``uv export`` selectors,
the CPython the sets are installed for, and the import names checked before a
success stamp is written. ``BASE``, ``ML`` and ``MANAGED_PYTHON`` are part of
a set's key (``launcher_layout.selection``): keep them plain literals.

source: ADR-1092"""

from __future__ import annotations

# The newest 3.12 build uv 0.11.3 (launcher_uv.UV_VERSION) can download; 3.12
# has a wheel for every package of uv.lock's launcher sets.
# source: `uv python list --only-downloads` with uv 0.11.3, 2026-10-14
MANAGED_PYTHON = "3.12.13"

# The launcher's two groups (source: ADR-1092).
BASE = ("--only-group", "launcher-base")
ML = ("--only-group", "launcher-ml")

# What scripts/setup.sh and scripts/setup.py install (source: ADR-1092);
# the sqlite extra is ADR-1089's (source: ADR-1089).
INSTALLER = (
    "--extra",
    "postgresql",
    "--extra",
    "sqlite",
    "--extra",
    "codebase",
    "--extra",
    "benchmarks",
)

# Imports that must resolve before a stamp is written (source: ADR-1092).
BASE_IMPORTS = (
    "mcp",
    "pydantic",
    "pydantic_settings",
    "numpy",
    "psycopg",
    "psycopg_pool",
    "pgvector",
    "sqlite_vec",
)
ML_IMPORTS = ("sentence_transformers", "flashrank")
