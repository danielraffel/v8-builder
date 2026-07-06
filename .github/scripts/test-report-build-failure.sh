#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAKE_GH="$TMP/gh"
cat > "$FAKE_GH" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

if [ "$1" = "run" ] && [ "$2" = "view" ] && [ "$3" = "12345" ]; then
  cat <<'JSON'
{
  "jobs": [
    {
      "name": "build-v8 (linux, x64)",
      "conclusion": "success",
      "url": "https://github.example/jobs/1"
    },
    {
      "name": "build-v8 (mac, arm64)",
      "conclusion": "failure",
      "url": "https://github.example/jobs/2"
    },
    {
      "name": "validate-all",
      "conclusion": "skipped",
      "url": "https://github.example/jobs/3"
    }
  ]
}
JSON
  exit 0
fi

if [ "$1" = "run" ] && [ "$2" = "view" ] && [ "$3" = "22222" ]; then
  cat <<'JSON'
{
  "jobs": [
    {
      "name": "build-v8 (linux, x64)",
      "conclusion": "cancelled",
      "url": "https://github.example/jobs/4"
    }
  ]
}
JSON
  exit 0
fi

if [ "$1" = "run" ] && [ "$2" = "view" ] && [ "$3" = "33333" ]; then
  cat <<'JSON'
{
  "jobs": [
    {
      "name": "build-v8 (linux, arm64)",
      "status": "queued",
      "conclusion": "",
      "url": "https://github.example/jobs/5"
    }
  ]
}
JSON
  exit 0
fi

if [ "$1" = "pr" ] && [ "$2" = "view" ] && [ "$3" = "codex/v8-auto-fix-chromium-lkgr-existing-33333" ]; then
  echo 'https://github.example/pull/99'
  exit 0
fi

echo "unexpected gh invocation: $*" >&2
exit 2
SH
chmod +x "$FAKE_GH"

bash -n "$REPO_ROOT/.github/scripts/report-build-failure.sh"

REPORT_DIR="$TMP/reports" \
DRY_RUN=1 \
GH_CMD="$FAKE_GH" \
RELEASE_REPO="danielraffel/v8-builder" \
FAILED_RUN_ID="12345" \
FAILED_RUN_URL="https://github.com/danielraffel/v8-builder/actions/runs/12345" \
FAILED_RUN_NAME="Build V8" \
FAILED_RUN_CONCLUSION="cancelled" \
FAILED_DISPLAY_TITLE="chromium-lkgr-abcdef123456" \
FAILED_HEAD_BRANCH="main" \
FAILED_HEAD_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" \
  bash "$REPO_ROOT/.github/scripts/report-build-failure.sh"

REPORT="$TMP/reports/chromium-lkgr-abcdef123456-12345.md"
test -f "$REPORT"

grep -Fq "V8 Auto-Update Failure: chromium-lkgr-abcdef123456" "$REPORT"
grep -Fq "build-v8 (mac, arm64)" "$REPORT"
grep -Fq "Run conclusion: \`cancelled\`" "$REPORT"
grep -Fq "https://github.com/danielraffel/v8-builder/actions/runs/12345" "$REPORT"
grep -Fq "@codex fix the CI failures/stall for chromium-lkgr-abcdef123456" "$REPORT"
grep -Fq "Do not publish manually" "$REPORT"

REPORT_DIR="$TMP/cancelled-reports" \
DRY_RUN=1 \
GH_CMD="$FAKE_GH" \
RELEASE_REPO="danielraffel/v8-builder" \
FAILED_RUN_ID="22222" \
FAILED_RUN_NAME="Build V8" \
FAILED_RUN_CONCLUSION="cancelled" \
FAILED_DISPLAY_TITLE="chromium-lkgr-cancelled" \
  bash "$REPO_ROOT/.github/scripts/report-build-failure.sh"

test ! -e "$TMP/cancelled-reports/chromium-lkgr-cancelled-22222.md"

REPORT_DIR="$TMP/existing-pr-reports" \
DRY_RUN=0 \
GH_CMD="$FAKE_GH" \
RELEASE_REPO="danielraffel/v8-builder" \
FAILED_RUN_ID="33333" \
FAILED_RUN_NAME="Build V8" \
FAILED_RUN_CONCLUSION="stale_queued" \
FAILED_DISPLAY_TITLE="chromium-lkgr-existing" \
  bash "$REPO_ROOT/.github/scripts/report-build-failure.sh"

test -f "$TMP/existing-pr-reports/chromium-lkgr-existing-33333.md"

echo "report-build-failure dry-run test passed"
