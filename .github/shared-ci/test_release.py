"""Release recovery contracts using mocked Git/GitHub process boundaries."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


def load_module():
    spec = importlib.util.spec_from_file_location(
        "shared_release", Path(__file__).with_name("release.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_module()
REPOSITORY = "fixture/project"
HEAD = "a" * 40
TAG = "v1.2.3"


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.tag_head = HEAD
        self.runs = []
        self.env = patch.dict(
            os.environ,
            {
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_SHA": HEAD,
                "GITHUB_REPOSITORY": REPOSITORY,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        processes = patch.object(
            release.subprocess, "check_output", side_effect=self.command
        )
        processes.start()
        self.addCleanup(processes.stop)
        exists = patch.object(
            release.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        )
        self.exists = exists.start()
        self.addCleanup(exists.stop)
        config = patch.object(
            release.Path,
            "read_text",
            return_value=json.dumps({"release_workflows": ["release.yml"]}),
        )
        self.config = config.start()
        self.addCleanup(config.stop)

    def command(self, args, **kwargs):
        self.assertTrue(kwargs["text"])
        command = tuple(args)
        self.calls.append(command)
        if command == ("bash", ".github/ci/run.sh", "release-version"):
            return f"tag={TAG}\n"
        if command == ("git", "rev-parse", "HEAD"):
            return HEAD
        if command == ("git", "rev-parse", f"refs/tags/{TAG}^{{commit}}"):
            return self.tag_head
        if command[:3] == ("gh", "run", "list"):
            return json.dumps(self.runs)
        if command[:2] in {("gh", "api"), ("gh", "workflow"), ("gh", "run")}:
            return ""
        raise AssertionError(f"Unexpected process: {command}")

    def dispatch(self):
        return (
            "gh",
            "workflow",
            "run",
            "release.yml",
            "--repo",
            REPOSITORY,
            "--ref",
            TAG,
        )

    def test_existing_tag_same_sha_resumes_missing_dispatch(self):
        release.release()
        self.assertIn(self.dispatch(), self.calls)
        self.assertFalse(any(command[:2] == ("gh", "api") for command in self.calls))
        self.exists.assert_called_once_with(
            ["git", "rev-parse", "--verify", f"refs/tags/{TAG}"],
            capture_output=True,
            check=False,
        )

    def test_previous_version_tag_is_neither_overwritten_nor_dispatched(self):
        self.tag_head = "b" * 40
        release.release()
        self.assertFalse(any(command[0] == "gh" for command in self.calls))
        self.config.assert_not_called()

    def test_creation_uses_tested_sha_then_dispatches_on_tag(self):
        self.exists.return_value = subprocess.CompletedProcess([], 1)
        release.release()
        creation = (
            "gh",
            "api",
            f"repos/{REPOSITORY}/git/refs",
            "--method",
            "POST",
            "-f",
            f"ref=refs/tags/{TAG}",
            "-f",
            f"sha={HEAD}",
        )
        self.assertIn(creation, self.calls)
        self.assertLess(self.calls.index(creation), self.calls.index(self.dispatch()))

    def test_failed_publisher_reruns_failed_jobs_without_duplicate_dispatch(self):
        self.runs = [
            {
                "databaseId": 7,
                "headBranch": TAG,
                "status": "completed",
                "conclusion": "failure",
            }
        ]
        release.release()
        self.assertIn(
            ("gh", "run", "rerun", "7", "--repo", REPOSITORY, "--failed"),
            self.calls,
        )
        self.assertNotIn(self.dispatch(), self.calls)

    def test_successful_or_running_publisher_is_not_duplicated(self):
        for status, conclusion in [("completed", "success"), ("in_progress", "")]:
            with self.subTest(status=status):
                self.calls.clear()
                self.runs = [
                    {
                        "databaseId": 7,
                        "headBranch": TAG,
                        "status": status,
                        "conclusion": conclusion,
                    }
                ]
                release.release()
                self.assertFalse(
                    any(
                        command[:3]
                        in {("gh", "workflow", "run"), ("gh", "run", "rerun")}
                        for command in self.calls
                    )
                )

    def test_other_tag_run_does_not_hide_missing_dispatch(self):
        self.runs = [
            {
                "databaseId": 7,
                "headBranch": "v0.0.1",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        release.release()
        self.assertIn(self.dispatch(), self.calls)

    def test_non_main_ref_cannot_create_tag(self):
        os.environ["GITHUB_REF"] = "refs/heads/topic"
        with self.assertRaises(ValueError):
            release.release()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
