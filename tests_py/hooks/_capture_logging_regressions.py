"""Open-time capture log controls, also collected by the capture CI file."""

from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp_server.hooks import capture_worker_logging as logs


def _os_without(*names):
    return SimpleNamespace(
        **{name: value for name, value in vars(os).items() if name not in names}
    )


@contextmanager
def _host_fallback():
    flag = getattr(os, "O_NOFOLLOW", 0)
    if flag:

        def open_file(path):
            return os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | flag, 0o600)

        with mock.patch.object(logs, "_open_without_flag", side_effect=open_file):
            yield
    else:
        yield


def _real_open(api):
    fallback = logs._open_without_flag

    def open_file(path, flags, mode):
        if hasattr(api, "O_NOFOLLOW"):
            return os.open(path, flags, mode)
        return fallback(path)

    return open_file


@contextmanager
def _intercept_open(api, redirect):
    with (
        mock.patch.object(api, "open", side_effect=redirect),
        mock.patch.object(
            logs,
            "_open_without_flag",
            side_effect=partial(redirect, flags=0, mode=0o600),
        ),
    ):
        yield


class TestCaptureLoggingOpenRaces(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def _open_error(self, path, api):
        try:
            with mock.patch.object(logs, "os", api):
                handler = logs.RotatingFileHandler(path, encoding="utf-8")
        except (OSError, AttributeError) as exc:
            return exc
        handler.close()
        return None

    def test_transient_symlink_restored_to_same_inode_is_refused(self):
        path, target = self.root / "worker.log", self.root / "target"
        path.write_text("untouched")
        target.hardlink_to(path)
        api = _os_without("O_NOFOLLOW", "geteuid", "fchmod")
        real_open = os.open

        def redirect(path, flags, mode):
            Path(path).unlink()
            Path(path).symlink_to(target)
            descriptor = real_open(path, flags, mode)
            Path(path).unlink()
            Path(path).hardlink_to(target)
            return descriptor

        with (
            mock.patch.object(ctypes, "WinDLL", None, create=True),
            mock.patch.object(api, "open", side_effect=redirect),
        ):
            error = self._open_error(path, api)
        self.assertIsInstance(error, OSError)
        self.assertEqual(target.read_text(), "untouched")

    def test_missing_log_never_creates_a_dangling_symlink_target(self):
        path, target = self.root / "worker.log", self.root / "missing"
        api = _os_without("O_NOFOLLOW", "geteuid", "fchmod")
        real_open = os.open

        def redirect(path, flags, mode):
            Path(path).symlink_to(target)
            return real_open(path, flags, mode)

        with (
            mock.patch.object(ctypes, "WinDLL", None, create=True),
            mock.patch.object(api, "open", side_effect=redirect),
        ):
            error = self._open_error(path, api)
        self.assertFalse(target.exists())
        self.assertIsInstance(error, OSError)

    def test_unavailable_nofollow_refuses_regular_files_too(self):
        path = self.root / "worker.log"
        path.write_text("untouched")
        api = _os_without("O_NOFOLLOW", "geteuid", "fchmod")
        with (
            mock.patch.object(ctypes, "WinDLL", None, create=True),
            mock.patch.object(
                api, "open", side_effect=AssertionError("following open")
            ),
        ):
            error = self._open_error(path, api)
        self.assertIsInstance(error, OSError)
        self.assertEqual(path.read_text(), "untouched")

    def _assert_replacement_refused(self, before_present):
        path, target = self.root / "worker.log", self.root / "target"
        if before_present:
            path.write_text("original")
        target.write_text("untouched")
        api = _os_without("O_NOFOLLOW", "geteuid", "fchmod")
        opened = []
        with _host_fallback():
            real_open = _real_open(api)

            def redirect(path, flags, mode):
                if before_present:
                    Path(path).unlink()
                    Path(path).hardlink_to(target)
                descriptor = real_open(path, flags, mode)
                opened.append(descriptor)
                if not before_present:
                    Path(path).unlink()
                    Path(path).hardlink_to(target)
                return descriptor

            with _intercept_open(api, redirect):
                error = self._open_error(path, api)
        self.assertTrue(opened)
        self.assertIsInstance(error, OSError)
        self.assertEqual(target.read_text(), "untouched")
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_preopen_identity_refuses_a_regular_file_replacement(self):
        self._assert_replacement_refused(before_present=True)

    def test_current_path_identity_refuses_a_replacement_after_open(self):
        self._assert_replacement_refused(before_present=False)

    @unittest.skipUnless(getattr(ctypes, "WinDLL", None), "native Win32 API required")
    def test_windows_transient_reparse_handle_is_refused(self):
        path, target = self.root / "worker.log", self.root / "target"
        path.write_text("untouched")
        target.hardlink_to(path)
        dll = logs._windows_api()
        real_create = dll.CreateFileW

        def redirect(*args):
            self.assertTrue(args[5] & logs._OPEN_REPARSE_POINT)
            Path(args[0]).unlink()
            Path(args[0]).symlink_to(target)
            handle = real_create(*args)
            Path(args[0]).unlink()
            Path(args[0]).hardlink_to(target)
            return handle

        with (
            mock.patch.object(logs, "_windows_api", return_value=dll),
            mock.patch.object(dll, "CreateFileW", side_effect=redirect),
        ):
            error = self._open_error(
                path, _os_without("O_NOFOLLOW", "geteuid", "fchmod")
            )
        self.assertIsInstance(error, OSError)
        self.assertEqual(target.read_text(), "untouched")

    @unittest.skipUnless(getattr(ctypes, "WinDLL", None), "native Win32 API required")
    def test_windows_dangling_reparse_handle_never_creates_target(self):
        path, target = self.root / "worker.log", self.root / "missing"
        dll = logs._windows_api()
        real_create = dll.CreateFileW

        def redirect(*args):
            self.assertTrue(args[5] & logs._OPEN_REPARSE_POINT)
            Path(args[0]).symlink_to(target)
            return real_create(*args)

        with (
            mock.patch.object(logs, "_windows_api", return_value=dll),
            mock.patch.object(dll, "CreateFileW", side_effect=redirect),
        ):
            error = self._open_error(
                path, _os_without("O_NOFOLLOW", "geteuid", "fchmod")
            )
        self.assertFalse(target.exists())
        self.assertIsInstance(error, OSError)
