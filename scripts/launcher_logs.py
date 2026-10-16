"""Bound the size of the launcher's append-only logs — stdlib only.

Every detached job and the Setup run append to a log in plugin data. A job
that fails the same way on every start would grow it without limit, so a log
past ``LOG_CAP`` bytes is moved to ``<log>.1`` (replacing the previous one)
before the next append: at most two files of about ``LOG_CAP`` each.

source: ADR-1108
"""

from __future__ import annotations

import os
from pathlib import Path

LOG_CAP = 1024 * 1024  # source: ADR-1108 (a decision, not a measurement)


def rotate(log: Path) -> Path:
    """Make ``log``'s directory, rotate ``log`` when over the cap; return ``log``."""
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log.stat().st_size > LOG_CAP:
            os.replace(log, log.with_name(log.name + ".1"))
    except FileNotFoundError:
        pass
    return log
