"""Private rotating worker diagnostics, independent of the launching hook pipe."""

from __future__ import annotations

import logging
import os
import stat
from logging.handlers import RotatingFileHandler as BaseRotatingFileHandler
from pathlib import Path

from mcp_server.shared.redaction_gate import RedactingFilter

# source: ADR-0488
LOG_BYTES = 196_000 * 30


def _check_log(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or (
        hasattr(os, "geteuid") and info.st_uid != os.geteuid()
    ):
        raise PermissionError("capture worker log must be an owner-controlled file")
    return info


class RotatingFileHandler(BaseRotatingFileHandler):
    def _open(self):
        path = Path(self.baseFilename)
        before = _check_log(path)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        stream = None
        try:
            current = _check_log(path)
            opened = os.fstat(descriptor)
            if current is None or (opened.st_dev, opened.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                raise PermissionError("capture worker log changed while opening")
            if before is not None and (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise PermissionError("capture worker log changed while opening")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            stream = os.fdopen(
                descriptor, self.mode, encoding=self.encoding, errors=self.errors
            )
            return stream
        finally:
            if stream is None:
                os.close(descriptor)


def configure(runtime: Path) -> None:
    path = runtime / "worker.log"
    for candidate in (path, path.with_suffix(".log.1")):
        _check_log(candidate)
    # source: ADR-0488
    handler = RotatingFileHandler(
        path, maxBytes=LOG_BYTES, backupCount=1, encoding="utf-8"
    )
    # source: ADR-1110 — every record is redacted before the handler writes
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
