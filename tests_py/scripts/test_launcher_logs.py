"""scripts/launcher_logs.py: an append-only log stays under two caps.

source: ADR-1108
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_a_log_over_the_cap_is_rotated_once_and_a_small_one_is_kept(
    monkeypatch, tmp_path
):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import launcher_logs

    log = tmp_path / "data" / "x.log"
    assert launcher_logs.rotate(log) == log  # creates the directory
    log.write_bytes(b"a" * launcher_logs.LOG_CAP)
    launcher_logs.rotate(log)
    assert log.exists() and not (tmp_path / "data" / "x.log.1").exists()
    log.write_bytes(b"b" * (launcher_logs.LOG_CAP + 1))
    launcher_logs.rotate(log)
    assert not log.exists()
    assert (tmp_path / "data" / "x.log.1").stat().st_size == launcher_logs.LOG_CAP + 1
    log.write_bytes(b"c" * (launcher_logs.LOG_CAP + 1))
    launcher_logs.rotate(log)  # the older rotation is replaced, never accumulated
    assert sorted(p.name for p in log.parent.iterdir()) == ["x.log.1"]
