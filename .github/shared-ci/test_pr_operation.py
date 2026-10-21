"""PR identity and protected-operation contracts at the process boundary."""

import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


def load_module():
    spec = importlib.util.spec_from_file_location(
        "shared_pr_operation", Path(__file__).with_name("pr_operation.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


operations = load_module()
REPOSITORY = "fixture/project"
HEAD = "a" * 40


class PrOperationTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.local_head = HEAD
        self.default_branch = "main"
        self.pr = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": HEAD,
                "ref": "topic",
                "repo": {"full_name": REPOSITORY, "default_branch": "main"},
            },
            "base": {
                "ref": "main",
                "repo": {"full_name": REPOSITORY, "default_branch": "main"},
            },
        }
        env = patch.dict(
            os.environ,
            {
                "PR_NUMBER": "42",
                "GITHUB_REPOSITORY": REPOSITORY,
                "REVIEW_HEAD": HEAD,
                "ENABLE_AUTOMERGE": "false",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        processes = patch.object(
            operations.subprocess, "check_output", side_effect=self.command
        )
        processes.start()
        self.addCleanup(processes.stop)

    def command(self, args, **kwargs):
        self.assertTrue(kwargs["text"])
        command = tuple(args)
        self.calls.append(command)
        if command == ("gh", "api", f"repos/{REPOSITORY}/pulls/42"):
            return json.dumps(self.pr)
        if command == ("gh", "api", f"repos/{REPOSITORY}"):
            return json.dumps({"default_branch": self.default_branch})
        if command == ("git", "rev-parse", "HEAD"):
            return self.local_head
        if command[:2] in {("git", "merge-base"), ("git", "push")}:
            return ""
        if command[:3] == ("gh", "pr", "merge"):
            return ""
        raise AssertionError(f"Unexpected process: {command}")

    def assert_no_mutation(self):
        self.assertFalse(
            any(
                command[:2] == ("git", "push") or command[:3] == ("gh", "pr", "merge")
                for command in self.calls
            )
        )

    def test_stale_review_head_rejects_push(self):
        self.pr["head"]["sha"] = "b" * 40
        with self.assertRaises(ValueError):
            operations.operate("push")
        self.assert_no_mutation()

    def test_fork_draft_and_closed_prs_reject_push(self):
        for kind in ["fork", "draft", "closed"]:
            with self.subTest(kind=kind):
                self.pr["head"]["repo"]["full_name"] = (
                    "outsider/project" if kind == "fork" else REPOSITORY
                )
                self.pr["draft"] = kind == "draft"
                self.pr["state"] = "closed" if kind == "closed" else "open"
                with self.assertRaises(ValueError):
                    operations.operate("push")
                self.assert_no_mutation()

    def test_base_branch_is_never_push_destination(self):
        self.pr["head"]["ref"] = self.pr["base"]["ref"]
        with self.assertRaises(ValueError):
            operations.operate("push")
        self.assert_no_mutation()

    def test_main_cannot_be_pushed_even_when_pr_targets_release_branch(self):
        self.pr["head"]["ref"] = "main"
        self.pr["base"]["ref"] = "release"
        with self.assertRaises(ValueError):
            operations.operate("push")
        self.assert_no_mutation()

    def test_non_main_default_branch_cannot_be_pushed(self):
        self.default_branch = "trunk"
        self.pr["base"]["repo"]["default_branch"] = "trunk"
        self.pr["head"]["repo"]["default_branch"] = "trunk"
        self.pr["head"]["ref"] = "trunk"
        self.pr["base"]["ref"] = "release"
        with self.assertRaises(ValueError):
            operations.operate("push")
        self.assert_no_mutation()

    def test_push_is_fast_forward_to_reviewed_branch_without_force_flags(self):
        operations.operate("push")
        ancestry = ("git", "merge-base", "--is-ancestor", HEAD, "HEAD")
        push = ("git", "push", "origin", "HEAD:refs/heads/topic")
        self.assertIn(ancestry, self.calls)
        self.assertIn(push, self.calls)
        self.assertLess(self.calls.index(ancestry), self.calls.index(push))
        self.assertFalse(any("force" in arg or arg == "--admin" for arg in push))

    def test_force_operation_cannot_be_injected(self):
        with self.assertRaises(ValueError):
            operations.operate("push --force")
        self.assert_no_mutation()

    def test_bootstrap_disables_auto_merge(self):
        with self.assertRaises(ValueError):
            operations.operate("auto-merge")
        self.assert_no_mutation()

    def test_local_fixes_require_another_review_before_auto_merge(self):
        os.environ["ENABLE_AUTOMERGE"] = "true"
        self.local_head = "b" * 40
        with self.assertRaises(ValueError):
            operations.operate("auto-merge")
        self.assert_no_mutation()

    def test_enabled_auto_merge_binds_exact_head_without_admin_bypass(self):
        os.environ["ENABLE_AUTOMERGE"] = "true"
        operations.operate("auto-merge")
        self.assertIn(
            (
                "gh",
                "pr",
                "merge",
                "42",
                "--repo",
                REPOSITORY,
                "--auto",
                "--squash",
                "--match-head-commit",
                HEAD,
            ),
            self.calls,
        )
        self.assertFalse(any("--admin" in command for command in self.calls))


if __name__ == "__main__":
    unittest.main()
