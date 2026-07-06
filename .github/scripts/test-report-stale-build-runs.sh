#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

OLD_CREATED="$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat().replace("+00:00", "Z"))
PY
)"
FRESH_CREATED="$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"))
PY
)"

FAKE_GH="$TMP/gh"
cat > "$FAKE_GH" <<SH
#!/usr/bin/env bash
set -euo pipefail

if [ "\$1" = "run" ] && [ "\$2" = "list" ]; then
  case " \$* " in
    *" --status queued "*)
      cat <<'JSON'
[
  {
    "databaseId": 33333,
    "displayTitle": "chromium-lkgr-stale",
    "headBranch": "main",
    "headSha": "abc123",
    "url": "https://github.example/runs/33333",
    "createdAt": "$OLD_CREATED"
  },
  {
    "databaseId": 44444,
    "displayTitle": "chromium-lkgr-fresh",
    "headBranch": "main",
    "headSha": "def456",
    "url": "https://github.example/runs/44444",
    "createdAt": "$FRESH_CREATED"
  }
]
JSON
      exit 0
      ;;
    *" --status in_progress "*)
      echo '[]'
      exit 0
      ;;
  esac
fi

if [ "\$1" = "run" ] && [ "\$2" = "view" ] && [ "\$3" = "33333" ]; then
  cat <<JSON
{
  "jobs": [
    {
      "name": "build-v8 (ubuntu-24.04, linux, arm64)",
      "status": "queued",
      "conclusion": "",
      "startedAt": "$OLD_CREATED",
      "url": "https://github.example/jobs/33333"
    },
    {
      "name": "build-v8 (macos-15, mac, arm64)",
      "status": "completed",
      "conclusion": "success",
      "startedAt": "$OLD_CREATED",
      "url": "https://github.example/jobs/33334"
    }
  ]
}
JSON
  exit 0
fi

if [ "\$1" = "run" ] && [ "\$2" = "view" ] && [ "\$3" = "44444" ]; then
  cat <<JSON
{
  "jobs": [
    {
      "name": "build-v8 (ubuntu-24.04, linux, arm64)",
      "status": "queued",
      "conclusion": "",
      "startedAt": "$FRESH_CREATED",
      "url": "https://github.example/jobs/44444"
    }
  ]
}
JSON
  exit 0
fi

echo "unexpected gh invocation: \$*" >&2
exit 2
SH
chmod +x "$FAKE_GH"

bash -n "$REPO_ROOT/.github/scripts/report-stale-build-runs.sh"

REPORT_DIR="$TMP/reports" \
DRY_RUN=1 \
GH_CMD="$FAKE_GH" \
RELEASE_REPO="danielraffel/v8-builder" \
DEFAULT_BRANCH="main" \
STALE_MINUTES=45 \
  bash "$REPO_ROOT/.github/scripts/report-stale-build-runs.sh"

REPORT="$TMP/reports/chromium-lkgr-stale-33333.md"
test -f "$REPORT"
test ! -e "$TMP/reports/chromium-lkgr-fresh-44444.md"

grep -Fq "Run conclusion: \`stale_queued\`" "$REPORT"
grep -Fq "build-v8 (ubuntu-24.04, linux, arm64)" "$REPORT"
grep -Fq "@codex fix the CI failures/stall for chromium-lkgr-stale" "$REPORT"

echo "report-stale-build-runs dry-run test passed"
