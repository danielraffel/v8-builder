#!/usr/bin/env bash
# Create a durable issue + PR handoff for failed default-branch V8 build sweeps.
set -euo pipefail

GH_CMD="${GH_CMD:-gh}"
DRY_RUN="${DRY_RUN:-false}"
REPORT_DIR="${REPORT_DIR:-.github/codex-failures}"
LABEL="${LABEL:-auto-update-failed}"

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "report-build-failure: missing required env $name" >&2
    exit 2
  fi
}

require_env RELEASE_REPO
require_env FAILED_RUN_ID

FAILED_RUN_URL="${FAILED_RUN_URL:-https://github.com/${RELEASE_REPO}/actions/runs/${FAILED_RUN_ID}}"
FAILED_RUN_NAME="${FAILED_RUN_NAME:-Build V8}"
FAILED_RUN_CONCLUSION="${FAILED_RUN_CONCLUSION:-failure}"
FAILED_DISPLAY_TITLE="${FAILED_DISPLAY_TITLE:-}"
FAILED_HEAD_BRANCH="${FAILED_HEAD_BRANCH:-main}"
FAILED_HEAD_SHA="${FAILED_HEAD_SHA:-unknown}"

derive_target_id() {
  local candidate="${TARGET_ID:-${FAILED_DISPLAY_TITLE:-}}"
  if [ -z "$candidate" ] || [ "$candidate" = "$FAILED_RUN_NAME" ] || [ "$candidate" = "Build V8" ]; then
    candidate="${FAILED_RUN_NAME:-}"
  fi
  if [ -z "$candidate" ] || [ "$candidate" = "Build V8" ]; then
    candidate="run-${FAILED_RUN_ID}"
  fi
  printf '%s' "$candidate"
}

slugify() {
  python3 - "$1" <<'PY'
import re
import sys

slug = re.sub(r"[^0-9A-Za-z._-]+", "-", sys.argv[1].strip().lower())
slug = re.sub(r"-+", "-", slug).strip("-._")
print((slug or "target")[:72].rstrip("-._") or "target")
PY
}

TARGET_ID="$(derive_target_id)"
SAFE_TARGET="$(slugify "$TARGET_ID")"
REPORT_FILE="${REPORT_DIR}/${SAFE_TARGET}-${FAILED_RUN_ID}.md"
ISSUE_TITLE="V8 auto-update failed: ${TARGET_ID}"
BRANCH="codex/v8-auto-fix-${SAFE_TARGET}-${FAILED_RUN_ID}"
PR_TITLE="Fix V8 auto-update failure: ${TARGET_ID}"

echo "Fetching failed run jobs for ${RELEASE_REPO}#${FAILED_RUN_ID}"
JOBS_JSON="$("$GH_CMD" run view "$FAILED_RUN_ID" --repo "$RELEASE_REPO" --json jobs)"
FAILED_JOBS_MD="$(JOBS_JSON="$JOBS_JSON" python3 - <<'PY'
import json
import os
import sys

data = json.loads(os.environ["JOBS_JSON"])
jobs = data.get("jobs") or []
stale = (os.environ.get("FAILED_RUN_CONCLUSION") or "").startswith("stale")
bad_states = {
    "failure",
    "timed_out",
    "startup_failure",
    "action_required",
}
if stale:
    bad_states.add("queued")
bad = [j for j in jobs if ((j.get("conclusion") or j.get("status") or "").lower()
                           in bad_states)]
if not bad:
    bad = jobs
for job in bad:
    name = job.get("name") or "<unnamed>"
    conclusion = job.get("conclusion") or job.get("status") or "unknown"
    url = job.get("url") or ""
    line = f"- `{name}`: {conclusion}"
    if url:
        line += f" ({url})"
    print(line)
if not bad:
    print("- No job details returned by GitHub CLI.")
PY
)"
FAILED_JOB_COUNT="$(JOBS_JSON="$JOBS_JSON" python3 - <<'PY'
import json
import os

jobs = json.loads(os.environ["JOBS_JSON"]).get("jobs") or []
stale = (os.environ.get("FAILED_RUN_CONCLUSION") or "").startswith("stale")
bad_states = {
    "failure",
    "timed_out",
    "startup_failure",
    "action_required",
}
if stale:
    bad_states.add("queued")
bad = [j for j in jobs if ((j.get("conclusion") or j.get("status") or "").lower()
                           in bad_states)]
print(len(bad))
PY
)"

if [ "$FAILED_RUN_CONCLUSION" = "cancelled" ] && [ "$FAILED_JOB_COUNT" = "0" ]; then
  echo "Cancelled run ${FAILED_RUN_ID} has no failed jobs; skipping Codex handoff."
  exit 0
fi

CODEX_PROMPT="@codex fix the CI failures/stall for ${TARGET_ID}.

Inspect the failed or stalled GitHub Actions jobs and logs for run ${FAILED_RUN_ID}, make the smallest repo change that fixes the failure/stall, and push the fix to this PR branch. Do not publish manually, and do not manually create or update V8 releases; release publication must happen only through build-v8.yml after CI is green."

mkdir -p "$REPORT_DIR"
cat > "$REPORT_FILE" <<EOF
# V8 Auto-Update Failure: ${TARGET_ID}

- Repository: \`${RELEASE_REPO}\`
- Failed run: [${FAILED_RUN_ID}](${FAILED_RUN_URL})
- Workflow: \`${FAILED_RUN_NAME}\`
- Run conclusion: \`${FAILED_RUN_CONCLUSION}\`
- Display title: \`${FAILED_DISPLAY_TITLE:-<none>}\`
- Branch: \`${FAILED_HEAD_BRANCH}\`
- Head SHA: \`${FAILED_HEAD_SHA}\`

## Failed Jobs

${FAILED_JOBS_MD}

## Codex Handoff

${CODEX_PROMPT}
EOF

echo "Wrote ${REPORT_FILE}"

if [ "$DRY_RUN" = "1" ] || [ "$DRY_RUN" = "true" ]; then
  echo "DRY_RUN enabled; skipping label, issue, branch, PR, and comment creation."
  exit 0
fi

EXISTING_PR_URL="$("$GH_CMD" pr view "$BRANCH" --repo "$RELEASE_REPO" --json url --jq .url 2>/dev/null || true)"
if [ -n "$EXISTING_PR_URL" ]; then
  echo "Failure handoff PR already exists: ${EXISTING_PR_URL}"
  exit 0
fi

"$GH_CMD" label create "$LABEL" --repo "$RELEASE_REPO" \
  --description "Automated V8 update build failure" \
  --color B60205 >/dev/null 2>&1 || \
  "$GH_CMD" label edit "$LABEL" --repo "$RELEASE_REPO" \
    --description "Automated V8 update build failure" \
    --color B60205 >/dev/null

ISSUE_NUMBER="$("$GH_CMD" issue list --repo "$RELEASE_REPO" \
  --state open \
  --label "$LABEL" \
  --search "${SAFE_TARGET} in:title" \
  --json number \
  --jq '.[0].number // ""')"

if [ -n "$ISSUE_NUMBER" ]; then
  "$GH_CMD" issue comment "$ISSUE_NUMBER" --repo "$RELEASE_REPO" --body-file "$REPORT_FILE" >/dev/null
  echo "Updated issue #${ISSUE_NUMBER}"
else
  ISSUE_URL="$("$GH_CMD" issue create --repo "$RELEASE_REPO" \
    --title "$ISSUE_TITLE" \
    --body-file "$REPORT_FILE" \
    --label "$LABEL")"
  echo "Created issue ${ISSUE_URL}"
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git switch -c "$BRANCH"
git add "$REPORT_FILE"
git commit -m "Report V8 auto-update failure ${TARGET_ID}"
git push -u origin "$BRANCH"

PR_URL="$("$GH_CMD" pr view "$BRANCH" --repo "$RELEASE_REPO" --json url --jq .url 2>/dev/null || true)"
if [ -z "$PR_URL" ]; then
  PR_URL="$("$GH_CMD" pr create --repo "$RELEASE_REPO" \
    --base "$FAILED_HEAD_BRANCH" \
    --head "$BRANCH" \
    --title "$PR_TITLE" \
    --body-file "$REPORT_FILE")"
fi

COMMENT_FILE="$(mktemp)"
printf '%s\n' "$CODEX_PROMPT" > "$COMMENT_FILE"
"$GH_CMD" pr comment "$PR_URL" --repo "$RELEASE_REPO" --body-file "$COMMENT_FILE" >/dev/null
rm -f "$COMMENT_FILE"

echo "Created failure handoff PR ${PR_URL}"
