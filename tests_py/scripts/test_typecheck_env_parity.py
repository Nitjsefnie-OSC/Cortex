"""Retain reproducible local pyright setup after retiring its CI job.

Source: issue #253 and ADR-1092. The documented command still uses uv.lock
and runs the tool from that environment. CI no longer requires pyright.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CONTRIBUTING = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
CLAUDE_MD = (REPO / "CLAUDE.md").read_text(encoding="utf-8")

# The fenced block under the type-gate heading — anchored on the heading and
# not on `uv sync`, because CONTRIBUTING documents a second sync (dev setup)
# whose extras are deliberately different.
_TYPECHECK_SECTION = "### Reproducing the pyright gate locally"
_FENCED_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def _typecheck_block() -> str:
    """The commands CONTRIBUTING gives for reproducing the gate, one line."""
    start = CONTRIBUTING.find(_TYPECHECK_SECTION)
    assert start != -1, f"CONTRIBUTING.md lost the {_TYPECHECK_SECTION!r} section"
    match = _FENCED_BLOCK.search(CONTRIBUTING, start)
    assert match is not None, "no ```bash block under the type-gate heading"
    block = match.group(1).replace("\\\n", " ")
    assert "uv sync" in block, "the type-gate block no longer resolves from uv.lock"
    return block


def _flag_values(command: str, flag: str) -> set[str]:
    return set(re.findall(rf"--{flag}\s+([A-Za-z0-9._-]+)", command))


class DocumentedTypecheckEnvTest(unittest.TestCase):
    """Local type checks retain their documented dependency selection."""

    def test_documented_command_selects_the_typecheck_group(self) -> None:
        self.assertEqual(_flag_values(_typecheck_block(), "group"), {"typecheck"})

    def test_default_groups_are_excluded_like_ci(self) -> None:
        """CI's sync passes --no-default-groups; a local sync that does not
        installs packages CI never had."""
        self.assertIn("--no-default-groups", _typecheck_block())
        action = (REPO / ".github/actions/uv-locked-install/action.yml").read_text()
        self.assertIn(
            "--locked --inexact --no-install-project --no-default-groups", action
        )

    def test_pyright_is_run_from_that_environment(self) -> None:
        self.assertIn(".venv/bin/python -m pyright mcp_server/", _typecheck_block())

    def test_range_resolution_is_documented_as_forbidden(self) -> None:
        """The failure mode has to be named where the command is, or the next
        contributor re-derives the env from pyproject and repeats #253."""
        self.assertIn("#253", CONTRIBUTING)
        self.assertIn("#253", CLAUDE_MD)


if __name__ == "__main__":
    unittest.main()
