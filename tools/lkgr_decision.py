#!/usr/bin/env python3
"""Decide whether the LKGR watcher should dispatch a new V8 build.

The release cadence is keyed to the Chromium-co-tested Skia/V8/Dawn tuple, not to
every Chromium DEPS roll. Chromium LKGR can advance for unrelated dependencies; those
commits are recorded as provenance but should not trigger a V8 release unless one of
the tuple members changes.
"""

import argparse
import json
from pathlib import Path

TUPLE_KEYS = ("skia", "v8", "dawn")
EXPECTED_PLATFORM_COUNT = 8


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def lock_tuple(lock):
    return tuple((lock.get(k) or "") for k in TUPLE_KEYS)


def manifest_tuple(manifest):
    pair = manifest.get("pair") or {}
    return tuple((pair.get(k) or "") for k in TUPLE_KEYS)


def release_metadata_complete(metadata, current_lock):
    """Return whether metadata proves a complete, consumable current-tuple release."""
    if not metadata:
        return False
    current = lock_tuple(current_lock)
    if manifest_tuple(metadata) != current:
        return False
    assets = metadata.get("assets") or []
    manifests = metadata.get("manifests") or []
    if len(assets) != EXPECTED_PLATFORM_COUNT or len(manifests) != EXPECTED_PLATFORM_COUNT:
        return False
    for manifest in manifests:
        defines = manifest.get("consumer_defines")
        if manifest_tuple(manifest) != current:
            return False
        if (not isinstance(defines, list) or not defines or
                any(not isinstance(item, str) or not item for item in defines)):
            return False
    return True


def decide(current_lock, last_manifest=None, last_release_metadata=None, force=False):
    current = lock_tuple(current_lock)
    last = manifest_tuple(last_manifest) if last_manifest else ("", "", "")
    if force:
        should_dispatch, reason = True, "forced"
    elif current != last:
        should_dispatch, reason = True, "tuple-changed"
    elif not release_metadata_complete(last_release_metadata, current_lock):
        should_dispatch, reason = True, "release-incomplete"
    else:
        should_dispatch, reason = False, "tuple-unchanged"
    return {
        "should_dispatch": should_dispatch,
        "reason": reason,
        "current_tuple": dict(zip(TUPLE_KEYS, current)),
        "last_tuple": dict(zip(TUPLE_KEYS, last)),
    }


def _write_github_outputs(path, result, current_lock):
    lines = [
        f"newer={'yes' if result['should_dispatch'] else 'no'}",
        f"reason={result['reason']}",
        f"skia={current_lock.get('skia', '')}",
        f"v8={current_lock.get('v8', '')}",
        f"dawn={current_lock.get('dawn', '')}",
        f"chromium_revision={current_lock.get('chromium_revision', '')}",
        f"chromium_deps_blob={current_lock.get('chromium_deps_blob', '')}",
    ]
    with Path(path).open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--current", required=True, help="Current lkgr-lock.json")
    p.add_argument("--last-manifest", help="manifest.json from the latest release")
    p.add_argument("--last-release-metadata", help="release-metadata.json from the latest release")
    p.add_argument("--force", action="store_true", help="Dispatch regardless of tuple")
    p.add_argument("--github-output", help="Append GitHub Actions outputs here")
    args = p.parse_args(argv)

    current = load_json(args.current)
    last = load_json(args.last_manifest) if args.last_manifest else None
    metadata = load_json(args.last_release_metadata) if args.last_release_metadata else None
    result = decide(current, last, metadata, force=args.force)
    print(json.dumps(result, indent=2))
    if args.github_output:
        _write_github_outputs(args.github_output, result, current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
