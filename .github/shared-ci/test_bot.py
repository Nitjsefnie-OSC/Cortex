"""Regression cases for the privileged PR review gate."""

import unittest
from bot import eligible, native_receipts, verify
from config import outputs


def pr():
    return {
        "state": "open",
        "draft": False,
        "labels": [],
        "head": {"sha": "reviewed", "repo": {"full_name": "owner/repo"}},
    }


def messages():
    transcript = []
    for skill in ("code-review", "security-review"):
        transcript.append(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Skill",
                            "id": skill,
                            "input": {"skill": skill},
                        }
                    ]
                }
            }
        )
        transcript.append(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": skill,
                            "content": "review executed",
                        }
                    ]
                }
            }
        )
        work = (
            [("Agent", {"description": "native reviewer"})] * 7
            if skill == "code-review"
            else [("Bash", {"command": "git diff --name-only origin/HEAD..."})]
        )
        for index, (name, args) in enumerate(work):
            identifier = f"{skill}-{index}"
            transcript.append(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": name,
                                "id": identifier,
                                "input": args,
                            }
                        ]
                    }
                }
            )
            transcript.append(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": identifier,
                                "content": "completed",
                            }
                        ]
                    }
                }
            )
    transcript.append({"type": "result", "is_error": False, "permission_denials": []})
    return transcript


def report():
    return {
        "head": "reviewed",
        "outcome": "ready",
        "code_review": "passed",
        "security_review": "passed",
    }


class ReviewGateTests(unittest.TestCase):
    def test_current_same_repo_pr(self):
        self.assertTrue(eligible(pr(), "owner/repo", "reviewed"))

    def test_stale_head_and_fork(self):
        self.assertFalse(eligible(pr(), "owner/repo", "stale"))
        self.assertFalse(eligible(pr(), "another/repo", "reviewed"))

    def test_drafts_closed_and_holds(self):
        for changes in (
            {"draft": True},
            {"state": "closed"},
            {"labels": [{"name": "do-not-merge"}]},
        ):
            self.assertFalse(eligible({**pr(), **changes}, "owner/repo", "reviewed"))

    def test_valid_native_receipts(self):
        verify(messages(), report(), "reviewed")

    def test_prose_claim_is_not_native_execution(self):
        with self.assertRaises(ValueError):
            verify(
                [{"type": "result", "result": "Both native reviews passed"}],
                report(),
                "reviewed",
            )

    def test_skill_loading_without_review_is_rejected(self):
        transcript = [
            entry
            for entry in messages()
            if not any(
                block.get("name") in {"Agent", "Bash"}
                for block in entry.get("message", {}).get("content", [])
            )
        ]
        with self.assertRaises(ValueError):
            verify(transcript, report(), "reviewed")

    def test_failed_native_tool_result(self):
        transcript = messages()
        transcript[1]["message"]["content"][0]["is_error"] = True
        with self.assertRaises(ValueError):
            verify(transcript, report(), "reviewed")

    def test_permission_denial(self):
        transcript = messages()
        transcript[-1]["permission_denials"] = [
            {"tool_name": "Bash", "command": "git fetch origin"}
        ]
        with self.assertRaises(ValueError):
            verify(transcript, report(), "reviewed")

    def test_review_blocked_changed_head_and_fixes(self):
        for changes in (
            {"security_review": "blocked"},
            {"head": "new"},
            {"outcome": "fixed"},
        ):
            with self.assertRaises(ValueError):
                verify(messages(), {**report(), **changes}, "reviewed")

    def test_missing_result(self):
        with self.assertRaises(ValueError):
            verify(messages()[:-1], report(), "reviewed")

    def test_namespaced_skill(self):
        transcript = messages()
        transcript[0]["message"]["content"][0]["input"]["skill"] = (
            "code-review:code-review"
        )
        self.assertEqual(
            native_receipts(transcript), {"code-review", "security-review"}
        )

    def test_multiline_runtime_rejected(self):
        with self.assertRaises(ValueError):
            outputs({"python_version": "3.12\ninjected=true"})


if __name__ == "__main__":
    unittest.main()
