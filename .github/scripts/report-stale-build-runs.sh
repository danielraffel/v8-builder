#!/usr/bin/env bash
# Report Build V8 runs that are stuck with queued jobs for too long.
set -euo pipefail

GH_CMD="${GH_CMD:-gh}"
RELEASE_REPO="${RELEASE_REPO:-}"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
WORKFLOW_FILE="${WORKFLOW_FILE:-build-v8.yml}"
STALE_MINUTES="${STALE_MINUTES:-90}"

if [ -z "$RELEASE_REPO" ]; then
  echo "report-stale-build-runs: missing RELEASE_REPO" >&2
  exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for status in queued in_progress; do
  "$GH_CMD" run list \
    --repo "$RELEASE_REPO" \
    --workflow "$WORKFLOW_FILE" \
    --branch "$DEFAULT_BRANCH" \
    --status "$status" \
    --limit 20 \
    --json databaseId,displayTitle,headBranch,headSha,url,createdAt \
    > "$TMP/runs-$status.json"
done

python3 - "$TMP/runs-queued.json" "$TMP/runs-in_progress.json" <<'PY' > "$TMP/runs.tsv"
import json
import sys

seen = set()
for path in sys.argv[1:]:
    for run in json.load(open(path, encoding="utf-8")):
        run_id = str(run.get("databaseId") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        fields = [
            run_id,
            run.get("displayTitle") or f"run-{run_id}",
            run.get("url") or "",
            run.get("headBranch") or "main",
            run.get("headSha") or "unknown",
            run.get("createdAt") or "",
        ]
        print("\t".join(field.replace("\t", " ") for field in fields))
PY

reported=0
while IFS=$'\t' read -r run_id display_title run_url head_branch head_sha created_at; do
  [ -n "$run_id" ] || continue
  jobs_json="$("$GH_CMD" run view "$run_id" --repo "$RELEASE_REPO" --json jobs)"
  stale_count="$(
    JOBS_JSON="$jobs_json" \
    CREATED_AT="$created_at" \
    STALE_MINUTES="$STALE_MINUTES" \
    python3 - <<'PY'
from datetime import datetime, timezone
import json
import os


def parse_time(value):
    if not value or value.startswith("0001-"):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


data = json.loads(os.environ["JOBS_JSON"])
created_at = parse_time(os.environ.get("CREATED_AT"))
cutoff = int(os.environ["STALE_MINUTES"])
now = datetime.now(timezone.utc)
count = 0
for job in data.get("jobs") or []:
    if (job.get("status") or "").lower() != "queued":
        continue
    started = parse_time(job.get("startedAt")) or created_at
    if started is None:
        continue
    age_minutes = (now - started).total_seconds() / 60
    if age_minutes >= cutoff:
        count += 1
print(count)
PY
  )"
  if [ "${stale_count:-0}" -le 0 ]; then
    continue
  fi
  echo "Reporting stale queued Build V8 run ${run_id} (${stale_count} stale queued job(s))"
  TARGET_ID="$display_title" \
  FAILED_RUN_ID="$run_id" \
  FAILED_RUN_URL="$run_url" \
  FAILED_RUN_NAME="$display_title" \
  FAILED_RUN_CONCLUSION="stale_queued" \
  FAILED_DISPLAY_TITLE="$display_title" \
  FAILED_HEAD_BRANCH="$head_branch" \
  FAILED_HEAD_SHA="$head_sha" \
    bash "$(dirname "$0")/report-build-failure.sh"
  reported=$((reported + 1))
done < "$TMP/runs.tsv"

if [ "$reported" -eq 0 ]; then
  echo "No stale queued Build V8 runs found."
fi
