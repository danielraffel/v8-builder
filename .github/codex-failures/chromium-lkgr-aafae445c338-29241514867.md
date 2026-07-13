# V8 Auto-Update Failure: chromium-lkgr-aafae445c338

- Repository: `danielraffel/v8-builder`
- Failed run: [29241514867](https://github.com/danielraffel/v8-builder/actions/runs/29241514867)
- Workflow: `chromium-lkgr-aafae445c338`
- Run conclusion: `stale_queued`
- Display title: `chromium-lkgr-aafae445c338`
- Branch: `main`
- Head SHA: `154de739d67cd63aea37717eed8da5cc18ca977e`

## Failed Jobs

- `portable-linux-x64`: failure (https://github.com/danielraffel/v8-builder/actions/runs/29241514867/job/86788431132)
- `build-v8 (macos-15, mac, x86_64, mac-x86_64, mac -archs x86_64)`: queued (https://github.com/danielraffel/v8-builder/actions/runs/29241514867/job/86788451267)
- `build-v8 (macos-15, ios, arm64, ios-simulator-arm64, ios -archs arm64 --ios-env simulator)`: queued (https://github.com/danielraffel/v8-builder/actions/runs/29241514867/job/86788451419)

## Codex Handoff

@codex fix the CI failures/stall for chromium-lkgr-aafae445c338.

Inspect the failed or stalled GitHub Actions jobs and logs for run 29241514867, make the smallest repo change that fixes the failure/stall, and push the fix to this PR branch. Do not publish manually, and do not manually create or update V8 releases; release publication must happen only through build-v8.yml after CI is green.
