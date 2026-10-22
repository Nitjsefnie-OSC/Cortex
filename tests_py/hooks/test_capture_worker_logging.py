"""Detached diagnostics stay bounded and refuse redirected log paths."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp_server.hooks import capture_worker_logging as logs


def _os_without(*names):
    return SimpleNamespace(
        **{name: value for name, value in vars(os).items() if name not in names}
    )


class TestCaptureWorkerLogging(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.logger = logging.getLogger()
        self.handlers = list(self.logger.handlers)
        self.level = self.logger.level

    def tearDown(self):
        for handler in list(self.logger.handlers):
            if handler not in self.handlers:
                self.logger.removeHandler(handler)
                handler.close()
        self.logger.setLevel(self.level)
        self.temporary.cleanup()

    def test_rotation_retains_only_current_and_previous_segment(self):
        with mock.patch.object(logs, "LOG_BYTES", 64):
            logs.configure(self.root)
        for number in range(4):
            self.logger.error("fixture segment %s %s", number, "x" * 40)
        paths = list(self.root.iterdir())
        self.assertEqual({path.name for path in paths}, {"worker.log", "worker.log.1"})
        self.assertIn("segment 3", (self.root / "worker.log").read_text())
        self.assertIn("segment 2", (self.root / "worker.log.1").read_text())

    def test_symlink_log_is_refused_without_touching_target(self):
        target = self.root / "kept"
        target.write_text("untouched")
        (self.root / "worker.log").symlink_to(target)
        with self.assertRaises(PermissionError):
            logs.configure(self.root)
        self.assertEqual(target.read_text(), "untouched")

    def _configuration_error(self, runtime, api):
        try:
            with mock.patch.object(logs, "os", api):
                logs.configure(runtime)
        except (OSError, AttributeError) as exc:
            return exc
        return None

    def test_regular_logs_create_and_reopen_without_posix_capabilities(self):
        api = _os_without("O_NOFOLLOW", "geteuid", "fchmod")
        self.assertIsNone(self._configuration_error(self.root, api))
        self.logger.error("first diagnostic")
        self.assertIsNone(self._configuration_error(self.root, api))
        self.logger.error("second diagnostic")
        text = (self.root / "worker.log").read_text()
        self.assertIn("first diagnostic", text)
        self.assertIn("second diagnostic", text)

    def test_symlink_segments_are_refused_with_and_without_nofollow(self):
        for missing in ((), ("O_NOFOLLOW", "geteuid", "fchmod")):
            for name in ("worker.log", "worker.log.1"):
                for dangling in (False, True):
                    with self.subTest(missing=missing, name=name, dangling=dangling):
                        with tempfile.TemporaryDirectory(dir=self.root) as directory:
                            runtime = Path(directory)
                            target = runtime / "target"
                            if not dangling:
                                target.write_text("untouched")
                            (runtime / name).symlink_to(target)
                            error = self._configuration_error(
                                runtime, _os_without(*missing)
                            )
                            self.assertIsInstance(error, PermissionError)
                            if dangling:
                                self.assertFalse(target.exists())
                            else:
                                self.assertEqual(target.read_text(), "untouched")

    def test_symlink_substituted_during_open_is_refused_on_both_paths(self):
        real_open = os.open
        for missing in ((), ("O_NOFOLLOW", "geteuid", "fchmod")):
            for same_file in (False, True):
                with (
                    self.subTest(missing=missing, same_file=same_file),
                    tempfile.TemporaryDirectory(dir=self.root) as directory,
                ):
                    runtime = Path(directory)
                    (runtime / "worker.log").write_text("untouched")
                    target = runtime / "target"
                    if same_file:
                        target.hardlink_to(runtime / "worker.log")
                    else:
                        target.write_text("untouched")
                    api = _os_without(*missing)
                    opened = []

                    def redirect(
                        path, flags, mode, *, target=target, api=api, opened=opened
                    ):
                        if hasattr(api, "O_NOFOLLOW"):
                            self.assertTrue(flags & api.O_NOFOLLOW)
                        Path(path).unlink()
                        Path(path).symlink_to(target)
                        descriptor = real_open(path, flags, mode)
                        opened.append(descriptor)
                        return descriptor

                    with mock.patch.object(api, "open", side_effect=redirect):
                        error = self._configuration_error(runtime, api)
                    self.assertIsInstance(error, OSError)
                    self.assertEqual(target.read_text(), "untouched")
                    for descriptor in opened:
                        with self.assertRaises(OSError):
                            os.fstat(descriptor)
