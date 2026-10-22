"""Private rotating worker diagnostics, independent of the launching hook pipe."""

from __future__ import annotations

import ctypes
import logging
import os
import stat
from logging.handlers import RotatingFileHandler as BaseRotatingFileHandler
from pathlib import Path

from mcp_server.shared.redaction_gate import RedactingFilter

# source: ADR-0488
LOG_BYTES = 196_000 * 30

# source: Microsoft CreateFileW: GENERIC_WRITE | FILE_READ_ATTRIBUTES
_LOG_ACCESS = 0x40000080
# source: Microsoft CreateFileW: FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_LOG_SHARE = 7
# source: https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
_OPEN_ALWAYS = 4
# source: same CreateFileW page, FILE_FLAG_OPEN_REPARSE_POINT
_OPEN_REPARSE_POINT = 0x00200000
# source: Microsoft GetFileInformationByHandleEx, FileAttributeTagInfo
_ATTRIBUTE_TAG_INFO = 9


class _AttributeTagInfo(ctypes.Structure):
    # source: Microsoft FILE_ATTRIBUTE_TAG_INFO (winbase.h)
    _fields_ = [("attributes", ctypes.c_uint32), ("tag", ctypes.c_uint32)]


def _windows_api() -> ctypes.CDLL:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("capture log requires a no-follow opener")
    dll = loader("kernel32", use_last_error=True)
    try:
        handle, dword = ctypes.c_void_p, ctypes.c_uint32
        dll.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            dword,
            dword,
            handle,
            dword,
            dword,
            handle,
        ]
        dll.CreateFileW.restype = handle
        dll.GetFileInformationByHandleEx.argtypes = [
            handle,
            ctypes.c_int,
            handle,
            dword,
        ]
        dll.GetFileInformationByHandleEx.restype = ctypes.c_int
        dll.CloseHandle.argtypes = [handle]
        dll.CloseHandle.restype = ctypes.c_int
    except AttributeError as exc:
        raise OSError("capture log requires a no-follow opener") from exc
    return dll


def _open_without_flag(path: Path) -> int:
    dll = _windows_api()
    import msvcrt  # noqa: PLC0415 — the WinDLL capability was checked above

    handle = dll.CreateFileW(
        str(path),
        _LOG_ACCESS,
        _LOG_SHARE,
        None,
        _OPEN_ALWAYS,
        _OPEN_REPARSE_POINT,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = None
    try:
        info = _AttributeTagInfo()
        if not dll.GetFileInformationByHandleEx(
            handle, _ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PermissionError("capture worker log must not be a reparse point")
        descriptor = msvcrt.open_osfhandle(handle, os.O_APPEND | os.O_NOINHERIT)
        return descriptor
    finally:
        if descriptor is None:
            dll.CloseHandle(handle)


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
        if hasattr(os, "O_NOFOLLOW"):
            descriptor = os.open(path, flags, 0o600)
        else:
            descriptor = _open_without_flag(path)
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
