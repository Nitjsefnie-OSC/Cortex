# Shared CI

The eight repositories use the same workflows hosted in Cortex. External callers
pin both the reusable workflow and `engine-ref` to the same commit. Each repository
owns `.github/ci/config.json` and `.github/ci/run.sh` with these commands:

- `setup`: locked dependencies; `setup environment` prepares compatibility probes.
- `unit`, `regression`, `e2e`, `environment`, `installation`: actual test categories.
- `release-version`: prints `tag=vX.Y.Z`, optionally preceded by `version=X.Y.Z`.

The primary Linux job shares one environment across the five categories. Declared
compatibility platforms run their environment probes concurrently. Missing
prerequisites fail; an empty suite must not be represented as successful coverage.
Release tags require every matrix job to pass on main. Existing tags are immutable.
Publisher dispatch resumes when a tag was created but dispatch was interrupted.

## Claude bot

The caller uses `workflow_run` after CI, so Dependabot does not require a duplicate
OAuth secret. The trusted default-branch engine selects a current open same-repo
PR whose author has write access or is Dependabot/Claude. Drafts and hold labels
are excluded; the agent also reads explicit discussion holds.

The checkout contains full history and `origin/HEAD`. Native code/security review
tools and the repository test adapter are allowed in headless mode. Runtime setup
is shared; dependency installation is deferred until Claude needs to test fixes.
Pushes use the Claude App token and a helper bound to the original PR head. The
helper refuses default/base branches and force pushes. A new push gets a new CI
run, then a fresh review.

`Claude bot` stays pending while the action runs. The finalizer requires native
Skill calls, successful review-agent work, a security diff, no permission denials,
a ready report, and successful CI on the unchanged head. The native code-review
work contract follows the upstream command's three preparation agents and four
reviewers. A live action fixture must confirm this contract before auto-merge is
enabled; a successful unit test does not prove native execution.

Set repository variable `CLAUDE_AUTOMERGE=true` only after `Claude bot` and the
actual test-matrix contexts are required checks. The App enables protected
auto-merge while the review status remains pending; the finalizer unlocks it only
after receipt validation. No admin bypass is used.

## Validation and sources

Run `python -m unittest discover -s .github/shared-ci -p 'test_*.py'`, actionlint,
and the CI contract tests under `tests_py/scripts`. The bootstrap migration must
also execute the real hosted suites and both native reviews before activation.

- [Reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
- [GitHub workflow trigger behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
- [Claude GitHub Actions](https://code.claude.com/docs/en/github-actions)
- [Pinned Claude action](https://github.com/anthropics/claude-code-action/tree/a5a423f75c251dcbff8a74e99f83b2cb03c72bfa)
- [Native code-review command](https://github.com/anthropics/claude-code/blob/main/plugins/code-review/commands/code-review.md)

Actual publishers retain their existing workflow identities, artifact verification
and provenance. Exploratory fuzzing, badges, posture reports and duplicate review
workflows are removed from automatic CI under the owner's seven-category scope.
