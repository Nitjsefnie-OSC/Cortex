"""Retained aggregate-parser tests and the replacement shared CI contract.

The parser's fixtures cover its historical CI Green format. Live assertions
cover shared-tests.yml and the caller's release dependency instead.

Source: scripts/check_ci_gate_complete.py and ADR-1114.
"""

from __future__ import annotations

import importlib.util
import json
import unittest

import yaml
from pathlib import Path

# Dotted, path-derived module name: mutmut keys its trampolines on
# "scripts.check_ci_gate_complete.*" and a bare name makes every mutant look
# unreached (issue #293, same idiom as test_check_doc_claims.py).
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "scripts.check_ci_gate_complete", _SCRIPTS / "check_ci_gate_complete.py"
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _workflow(jobs: str) -> str:
    """Wrap job definitions in the minimal ci.yml envelope the parser reads."""
    return "name: CI\n\non:\n  push:\n    branches: [main]\n\njobs:\n" + jobs


_COMPLETE = _workflow(
    """  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .

  mcp-host-config:
    name: Validate MCP host configurations
    runs-on: ubuntu-latest
    if: github.event.pull_request.head.repo.fork == false
    steps:
      - run: true

  ci-green:
    name: CI Green
    if: always()
    runs-on: ubuntu-latest
    needs:
      - lint
      - mcp-host-config
    steps:
      - name: Require every job above to have succeeded
        env:
          NEEDS: ${{ toJSON(needs) }}
          ALLOWED_SKIPS: mcp-host-config
        run: true
"""
)


class CheckCompleteGate(unittest.TestCase):
    def test_complete_workflow_has_no_failures(self) -> None:
        self.assertEqual(gate.check(_COMPLETE), [])

    def test_parses_jobs_needs_and_allowed_skips(self) -> None:
        jobs, needs, allowed, exempt = gate.parse_workflow(_COMPLETE)
        self.assertEqual(set(jobs), {"lint", "mcp-host-config", "ci-green"})
        self.assertFalse(jobs["lint"])
        self.assertTrue(jobs["mcp-host-config"])
        self.assertEqual(needs, ["lint", "mcp-host-config"])
        self.assertEqual(allowed, ["mcp-host-config"])
        self.assertEqual(exempt, set())


class RefusesIncompleteGate(unittest.TestCase):
    def _only_failure(self, workflow: str) -> str:
        failures = gate.check(workflow)
        self.assertEqual(len(failures), 1, failures)
        return failures[0]

    def test_missing_gate_job_fails(self) -> None:
        workflow = _workflow(
            "  lint:\n    name: Lint\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: true\n"
        )
        self.assertIn(gate.GATE_JOB, self._only_failure(workflow))

    def test_job_absent_from_needs_fails(self) -> None:
        workflow = _COMPLETE.replace("      - lint\n", "")
        self.assertIn("'lint' is not in", self._only_failure(workflow))

    def test_needs_naming_a_nonexistent_job_fails(self) -> None:
        workflow = _COMPLETE.replace("      - lint\n", "      - lint\n      - ghost\n")
        self.assertIn("'ghost'", self._only_failure(workflow))

    def test_conditional_job_missing_from_allowed_skips_fails(self) -> None:
        workflow = _COMPLETE.replace("          ALLOWED_SKIPS: mcp-host-config\n", "")
        failure = self._only_failure(workflow)
        self.assertIn("mcp-host-config", failure)
        self.assertIn("ALLOWED_SKIPS", failure)

    def test_stale_allowed_skips_entry_fails(self) -> None:
        # `lint` carries no `if:`, so exempting it guards nothing and hides a
        # real skip if one ever appears.
        workflow = _COMPLETE.replace(
            "ALLOWED_SKIPS: mcp-host-config", "ALLOWED_SKIPS: mcp-host-config, lint"
        )
        self.assertIn("'lint'", self._only_failure(workflow))

    def test_gate_must_always_run(self) -> None:
        for condition in ("", "    if: success()\n"):
            workflow = _COMPLETE.replace("    if: always()\n", condition)
            self.assertIn("always()", self._only_failure(workflow))

    def test_duplicate_or_self_needs_fail(self) -> None:
        for job in ("lint", "ci-green"):
            workflow = _COMPLETE.replace(
                "      - lint\n", f"      - lint\n      - {job}\n"
            )
            self.assertIn("dependency", self._only_failure(workflow))


_WITH_EXEMPT_JOB = _workflow(
    """  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .

  mcp-host-config:
    name: Validate MCP host configurations
    runs-on: ubuntu-latest
    if: github.event.pull_request.head.repo.fork == false
    steps:
      - run: true

  release-gate:
    name: Tag a release
    needs: [ci-green]
    # ci-gate-exempt: runs only on push to main, after the PR merged —
    # there is nothing left for it to gate.
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - run: true

  ci-green:
    name: CI Green
    if: always()
    runs-on: ubuntu-latest
    needs:
      - lint
      - mcp-host-config
    steps:
      - name: Require every job above to have succeeded
        env:
          NEEDS: ${{ toJSON(needs) }}
          ALLOWED_SKIPS: mcp-host-config
        run: true
"""
)


class GateExemptJobs(unittest.TestCase):
    def test_exempt_job_outside_needs_has_no_failures(self) -> None:
        self.assertEqual(gate.check(_WITH_EXEMPT_JOB), [])

    def test_parses_exempt_set(self) -> None:
        jobs, needs, allowed, exempt = gate.parse_workflow(_WITH_EXEMPT_JOB)
        self.assertEqual(exempt, {"release-gate"})
        self.assertNotIn("release-gate", needs)
        self.assertTrue(jobs["release-gate"])

    def test_exempt_job_without_if_fails(self) -> None:
        workflow = _WITH_EXEMPT_JOB.replace(
            "    if: github.event_name == 'push' && github.ref == 'refs/heads/main'\n",
            "",
        )
        failures = gate.check(workflow)
        self.assertTrue(
            any("release-gate" in f and "no job-level" in f for f in failures),
            failures,
        )

    def test_unexempt_unneeded_job_still_fails(self) -> None:
        # Sanity: removing the marker (with no needs entry either) reverts to
        # the original "runs outside the gate" failure — the marker is a
        # deliberate escape hatch, not a parser blind spot.
        workflow = _WITH_EXEMPT_JOB.replace(
            "    # ci-gate-exempt: runs only on push to main, after the PR merged —\n"
            "    # there is nothing left for it to gate.\n",
            "",
        )
        failures = gate.check(workflow)
        unmarked = [f for f in failures if "release-gate" in f and "carries no" in f]
        self.assertTrue(unmarked, failures)


class SharedWorkflowContract(unittest.TestCase):
    """The five required categories cannot be skipped or made advisory.

    Source: ADR-1114 and .github/workflows/shared-tests.yml.
    """

    def workflow(self):
        path = _SCRIPTS.parent / ".github/workflows/shared-tests.yml"
        return yaml.safe_load(path.read_text())

    def test_primary_runner_executes_every_category_without_failure_override(self):
        job = self.workflow()["jobs"]["tests"]
        self.assertNotIn("if", job)
        self.assertNotIn("continue-on-error", job)
        phases = ["unit", "regression", "e2e", "environment", "installation"]
        for phase in phases:
            commands = [
                step
                for step in job["steps"]
                if step.get("run") == f"bash .github/ci/run.sh {phase}"
            ]
            self.assertEqual(len(commands), 1, phase)
            if phase == "environment":
                self.assertNotIn("if", commands[0])
            else:
                condition = commands[0].get("if", "")
                condition = condition.removeprefix("${{").removesuffix("}}").strip()
                self.assertEqual(condition, "matrix.runner == 'ubuntu-latest'")
            self.assertNotIn("continue-on-error", commands[0])

    def test_platforms_start_in_one_matrix_without_a_serial_dependency(self):
        jobs = self.workflow()["jobs"]
        self.assertEqual(set(jobs), {"tests"})
        job = jobs["tests"]
        self.assertNotIn("needs", job)
        self.assertNotIn("continue-on-error", job)
        self.assertEqual(job["runs-on"], "${{ matrix.runner }}")
        self.assertEqual(
            job["strategy"]["matrix"]["runner"], "${{ fromJSON(inputs.runners) }}"
        )

    def test_caller_waits_for_the_entire_reusable_workflow(self):
        path = _SCRIPTS.parent / ".github/workflows/ci.yml"
        jobs = yaml.safe_load(path.read_text())["jobs"]
        self.assertEqual(jobs["tests"]["uses"], "./.github/workflows/shared-tests.yml")
        self.assertEqual(jobs["release-tag"]["needs"], ["tests"])
        self.assertNotIn("continue-on-error", jobs["tests"])
        self.assertNotIn("if", jobs["tests"])
        runners = json.loads(jobs["tests"]["with"]["runners"])
        self.assertEqual(runners.count("ubuntu-latest"), 1)
        config = json.loads((_SCRIPTS.parent / ".github/ci/config.json").read_text())
        self.assertEqual(set(runners), {"ubuntu-latest", *config["environment_runners"]})


if __name__ == "__main__":
    unittest.main()
