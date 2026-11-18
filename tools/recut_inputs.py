#!/usr/bin/env python3
"""Derive an attested re-cut's build inputs from the release it re-cuts.

An ``-attested.<n>`` release rebuilds exactly what ``v8-<id>`` names, so the
V8 revision, the pair lock and the Skia validation release come from that
release's ``release-metadata.json``, not from the dispatch.  A dispatch that
names a different Skia validation release is refused rather than silently
overridden.

Prints ``v8_version=``, ``v8_revision=``, ``lkgr_lock_b64=`` and
``skia_release_tag=`` lines for ``$GITHUB_OUTPUT``.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

# Fields build-v8.py derives itself; everything else in a pair came from the lock.
DERIVED = {"this_artifact", "built_revision", "validated_skia_release"}


def recut_inputs(metadata: dict, skia_release_tag: str = "") -> dict[str, str]:
    manifests = metadata.get("manifests") or []
    if not manifests:
        raise ValueError("the base release-metadata.json has no manifests")
    pairs = {json.dumps(manifest.get("pair") or {}, sort_keys=True) for manifest in manifests}
    if len(pairs) != 1:
        raise ValueError("the base release's archives name different pairs")
    pair = json.loads(pairs.pop())
    revision = pair.get("v8") or pair.get("built_revision") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"the base release names no V8 revision (got {revision!r})")
    skia = pair.get("validated_skia_release") or ""
    if not skia:
        raise ValueError("the base release names no Skia validation release")
    if skia_release_tag and skia_release_tag != skia:
        raise ValueError(f"skia_release_tag={skia_release_tag} but the base release was validated "
                         f"against {skia}; dispatch with {skia}")
    lock = ""
    if pair.get("pair_kind") not in (None, "", "manual"):
        body = {key: value for key, value in pair.items() if key not in DERIVED}
        lock = base64.b64encode(json.dumps(body, sort_keys=True).encode()).decode()
    return {"v8_version": "", "v8_revision": revision, "lkgr_lock_b64": lock, "skia_release_tag": skia}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--skia-release-tag", default="")
    args = parser.parse_args(argv)
    try:
        values = recut_inputs(json.loads(args.metadata.read_text(encoding="utf-8")), args.skia_release_tag)
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    for key, value in values.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
