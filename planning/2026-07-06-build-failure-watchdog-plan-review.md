# Build Failure Watchdog Plan Review

Date: 2026-07-06
Repo: `danielraffel/v8-builder`

## Goal

Add a default-branch-only GitHub Actions watchdog for failed V8 auto-update builds. The
weekly release policy remains unchanged: `release-watch.yml` compares the Chromium LKGR
Skia/V8/Dawn tuple to the latest published release and dispatches `build-v8.yml` only when
that tuple changed. The watchdog handles the failure path after a dispatched build fails.

## Plan

1. Give `build-v8.yml` a stable run identity. Add optional `release_target_id` and use it
   as the workflow `run-name`, falling back to `v8_revision`, `v8_version`, or
   `synced-tip`.
2. Have `release-watch.yml` pass `release_target_id` as `chromium-lkgr-<v8-sha12>` for
   LKGR tuple builds and `v8-<tag>` for forced legacy tag builds.
3. Add `build-failure-watchdog.yml` on `workflow_run` for `Build V8`, gated to failed
   default-branch runs only. It checks out the current default branch, not the failing SHA.
4. Add `report-build-failure.sh` to fetch failed jobs, write a Markdown diagnostic report,
   create/update the `auto-update-failed` issue path, create a diagnostic branch and PR,
   and post a constrained `@codex` repair prompt.
5. Add a dry-run shell test with a mocked `gh run view` response. The test must prove the
   report includes target identity, failed job names, run URL, the `@codex` prompt, and the
   no-manual-publish constraint.
6. Wire the shell test into `tools/run_tests.sh` so local regression tests cover the handoff
   reporter.

## Review Notes

- Security boundary: no `pull_request_target`, no arbitrary branch execution, no checkout
  of the failed SHA. The watchdog only reacts to `Build V8` failures from the default
  branch and writes a report/PR.
- Token behavior: use `CODEX_TRIGGER_TOKEN` when configured so bot comments can trigger
  Codex; otherwise fall back to the workflow token. The fallback may open the PR but may
  not trigger downstream comment automation in every GitHub configuration.
- Target identity: use both explicit `release_target_id` and `run-name`. The explicit
  input makes the release watcher intentional; `run-name` makes the identity visible to
  `workflow_run`.
- Non-retroactive behavior: GitHub `workflow_run` will not fire for builds that failed
  before this workflow lands. Old failures still need manual triage or a fresh dispatch.

## Claude Alignment

Attempted to run Claude bridge mode for a second-opinion plan review:

```text
claude -p --output-format json ...
```

The local Claude CLI returned `Not logged in - Please run /login`, so live Claude alignment
was blocked on local auth. The implementation keeps the requested review shape by carrying
the same questions into the local adversarial review: target identity source, token
permissions, default-branch-only execution, dry-run test coverage, and release-safety
constraints.

## Validation Contract

- `tools/run_tests.sh`
- `bash -n .github/scripts/report-build-failure.sh .github/scripts/test-report-build-failure.sh`
- YAML parse for the touched workflows
- `actionlint` when available
- `git diff --check`
- Dry-run reporter against a real Actions run using `GH_CMD=ghapp`, `DRY_RUN=1`, and a
  temporary report directory.

After merge, dispatch a fresh `Build V8` workflow from `main` with the current Chromium
LKGR lock, `platforms=all`, and `skip_release=false`.

## Implementation Sweep

- The watchdog is scoped to failed default-branch `Build V8` runs only.
- The reporter dry-run mode performs no label, issue, branch, PR, or comment mutations.
- The handoff prompt explicitly tells Codex to repair the PR branch and not publish
  manually.
- `ghapp run view --json jobs` was verified in dry-run mode against existing run
  `28785203193`.
