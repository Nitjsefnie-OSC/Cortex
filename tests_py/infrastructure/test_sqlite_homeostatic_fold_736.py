"""The homeostatic fold runs on the SQLite backend (issue #736).

``_apply_fold`` issued ``LEAST``/``GREATEST``, which SQLite does not have, so
every fold cycle raised ``OperationalError`` on the default backend. The fold
now goes through ``fold_heat_base`` on each store, in that backend's dialect.

source: ADR-0363
"""

from __future__ import annotations

import pytest

from mcp_server.handlers.consolidation import homeostatic_apply
from mcp_server.infrastructure.sqlite_store import SqliteMemoryStore
from mcp_server.shared import write_class

_DOMAIN = "fold-domain"


@pytest.fixture
def store(tmp_path):
    s = SqliteMemoryStore(db_path=str(tmp_path / "fold.db"))
    yield s
    s.close()


def _insert(store, heat_base: float, **flags) -> int:
    mid = store.insert_memory(
        {
            "content": f"row {heat_base} {sorted(flags)}",
            "domain": _DOMAIN,
            "heat": heat_base,
            "heat_base": heat_base,
            "write_class": write_class.AUTO,
        }
    )
    for column in flags:
        store._conn.execute(f"UPDATE memories SET {column} = 1 WHERE id = ?", (mid,))
    store._conn.commit()
    return mid


def _heat(store, mid: int) -> float:
    row = store._conn.execute(
        "SELECT heat_base FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    return float(row["heat_base"])


def test_fold_scales_clamps_and_skips_exempt_rows(store):
    mid_scaled = _insert(store, 0.2)
    mid_high = _insert(store, 0.8)
    mid_low = _insert(store, 0.2)
    mid_protected = _insert(store, 0.3, is_protected=True)
    mid_stale = _insert(store, 0.3, is_stale=True)

    folded = homeostatic_apply._apply_fold(store, _DOMAIN, 2.5, write_class.AUTO)

    assert folded == 3
    assert _heat(store, mid_scaled) == pytest.approx(0.5)
    assert _heat(store, mid_high) == pytest.approx(1.0)  # 2.0 clamped to 1.0
    assert _heat(store, mid_low) == pytest.approx(0.5)
    assert _heat(store, mid_protected) == pytest.approx(0.3)
    assert _heat(store, mid_stale) == pytest.approx(0.3)
    assert store.get_homeostatic_factor(_DOMAIN, write_class.AUTO) == 1.0


def test_fold_clamps_to_zero_floor(store):
    mid = _insert(store, 0.4)
    homeostatic_apply._apply_fold(store, _DOMAIN, 0.0, write_class.AUTO)
    assert _heat(store, mid) == 0.0
