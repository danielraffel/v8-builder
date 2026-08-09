#!/usr/bin/env python3
"""Resolve an exact V8 pin from a Chromium milestone branch.

The milestone branch DEPS file is authoritative for the Chromium-side Skia/V8/Dawn
tuple.  skia-builder follows Skia's own DEPS for Dawn, so callers may also record the
actually-built Dawn SHA; a mismatch is surfaced explicitly rather than hidden.
"""

import argparse
import base64
import json
import re
import urllib.request


CHROMIUMDASH = "https://chromiumdash.appspot.com/fetch_milestones?num=40"
GITILES = "https://chromium.googlesource.com/chromium/src"
SKIA_GITILES = "https://skia.googlesource.com/skia"
KEYS = ("skia_revision", "v8_revision", "dawn_revision")


def _json_url(url):
    raw = urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "replace")
    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1]
    return json.loads(raw)


def _milestone_info(milestone):
    rows = _json_url(CHROMIUMDASH)
    for row in rows:
        if int(row.get("milestone", -1)) == milestone:
            return row
    raise SystemExit(f"milestone_pin: ChromiumDash has no M{milestone} entry")


def _extract_deps(text):
    result = {}
    for key in KEYS:
        match = re.search(rf"'{key}':\s*'([0-9a-f]{{40}})'", text)
        if not match:
            raise SystemExit(f"milestone_pin: missing {key} in Chromium DEPS")
        result[key.removesuffix("_revision")] = match.group(1)
    return result


def _skia_release_pins(tag):
    meta = _json_url(f"{SKIA_GITILES}/+/refs/heads/{tag}?format=JSON")
    revision = meta["commit"]
    encoded = urllib.request.urlopen(
        f"{SKIA_GITILES}/+/{revision}/DEPS?format=TEXT", timeout=30
    ).read()
    deps = base64.b64decode(encoded).decode("utf-8", "replace")
    match = re.search(
        r'"third_party/externals/dawn"\s*:\s*"[^"]+@([0-9a-f]{40})"', deps
    )
    if not match:
        raise SystemExit(f"milestone_pin: missing Dawn revision in Skia {tag} DEPS")
    return revision, match.group(1)


def milestone_lock(milestone, expected_skia=None, built_dawn=None, skia_release_tag=None):
    if skia_release_tag:
        wanted = f"chrome/m{milestone}"
        if skia_release_tag != wanted:
            raise SystemExit(f"milestone_pin: expected Skia release {wanted}, got {skia_release_tag}")
        release_skia, release_dawn = _skia_release_pins(skia_release_tag)
        expected_skia = expected_skia or release_skia
        built_dawn = built_dawn or release_dawn
    info = _milestone_info(milestone)
    branch = str(info["chromium_branch"])
    commit_meta = _json_url(f"{GITILES}/+/refs/branch-heads/{branch}?format=JSON")
    revision = commit_meta["commit"]
    deps_meta = _json_url(f"{GITILES}/+/{revision}/DEPS?format=JSON")
    encoded = urllib.request.urlopen(
        f"{GITILES}/+/{revision}/DEPS?format=TEXT", timeout=30
    ).read()
    pins = _extract_deps(base64.b64decode(encoded).decode("utf-8", "replace"))
    if expected_skia and pins["skia"] != expected_skia:
        raise SystemExit(
            f"milestone_pin: M{milestone} Chromium Skia {pins['skia']} does not match "
            f"published Skia {expected_skia}"
        )
    result = {
        "source": "chromium-milestone-branch-deps",
        "pair_kind": "chromium-milestone",
        "milestone": milestone,
        "chromium_branch": branch,
        "chromium_revision": revision,
        "chromium_deps_blob": deps_meta["id"],
        **pins,
        "repos": {
            "skia": "https://skia.googlesource.com/skia.git",
            "v8": "https://chromium.googlesource.com/v8/v8.git",
            "dawn": "https://dawn.googlesource.com/dawn.git",
        },
    }
    if built_dawn:
        result["built_dawn"] = built_dawn
        result["dawn_matches_chromium"] = built_dawn == pins["dawn"]
    if skia_release_tag:
        result["skia_release_tag"] = skia_release_tag
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("milestone", type=int)
    parser.add_argument("--expected-skia")
    parser.add_argument("--built-dawn")
    parser.add_argument("--skia-release-tag")
    args = parser.parse_args(argv)
    print(json.dumps(milestone_lock(
        args.milestone, args.expected_skia, args.built_dawn, args.skia_release_tag
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
