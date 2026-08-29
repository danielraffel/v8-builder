#!/usr/bin/env python3
"""Pure decision helpers for matched-milestone discovery and idempotency."""

import argparse
import json
import re
from pathlib import Path


EXPECTED_ASSET_PREFIXES = (
    "v8-mac-arm64",
    "v8-mac-x86_64",
    "v8-linux-arm64",
    "v8-linux-x64",
    "v8-win-x64",
    "v8-win-arm64",
    "v8-android-arm64",
    "v8-ios-simulator-arm64",
)
ACTIVE_STATUSES = {"queued", "in_progress", "pending", "requested", "waiting"}


def select_latest_skia(releases):
    choices = []
    for release in releases:
        tag = release.get("tagName", "")
        match = re.fullmatch(r"chrome/m([0-9]+)", tag)
        if match and not release.get("isDraft") and not release.get("isPrerelease"):
            choices.append((int(match.group(1)), tag))
    if not choices:
        raise SystemExit("matched_release: no canonical published Skia milestone release")
    milestone, tag = max(choices)
    return {"milestone": milestone, "tag": tag}


def has_active_target(runs, target_id):
    return any(
        run.get("displayTitle") == target_id and run.get("status") in ACTIVE_STATUSES
        for run in runs
    )


def release_is_complete(release, metadata, expected):
    if not release or not metadata:
        return False
    asset_names = {asset.get("name", "") for asset in release.get("assets", [])}
    zip_names = set(metadata.get("assets", []))
    if "release-metadata.json" not in asset_names or len(zip_names) != len(EXPECTED_ASSET_PREFIXES):
        return False
    if not zip_names.issubset(asset_names):
        return False
    if any(not any(name.startswith(prefix + "-") for name in zip_names)
           for prefix in EXPECTED_ASSET_PREFIXES):
        return False
    pair = metadata.get("pair") or {}
    keys = ("milestone", "v8", "built_skia", "built_dawn", "skia_release_tag")
    if any(pair.get(key) != expected.get(key) for key in keys):
        return False
    manifests = metadata.get("manifests") or []
    if len(manifests) != len(EXPECTED_ASSET_PREFIXES):
        return False
    return all(all((item.get("pair") or {}).get(key) == expected.get(key) for key in keys)
               for item in manifests)


def _write_outputs(path, values):
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as out:
        for key, value in values.items():
            out.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select-skia")
    select.add_argument("releases", type=Path)
    select.add_argument("--github-output", type=Path)
    active = sub.add_parser("check-active")
    active.add_argument("runs", type=Path)
    active.add_argument("target_id")
    active.add_argument("--github-output", type=Path)
    complete = sub.add_parser("check-release")
    complete.add_argument("release", type=Path)
    complete.add_argument("metadata", type=Path)
    complete.add_argument("expected", type=Path)
    complete.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "select-skia":
        result = select_latest_skia(json.loads(args.releases.read_text()))
        _write_outputs(args.github_output, result)
    elif args.command == "check-active":
        result = {"active": has_active_target(json.loads(args.runs.read_text()), args.target_id)}
        _write_outputs(args.github_output, result)
    else:
        result = {"complete": release_is_complete(
            json.loads(args.release.read_text()),
            json.loads(args.metadata.read_text()),
            json.loads(args.expected.read_text()),
        )}
        _write_outputs(args.github_output, result)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
