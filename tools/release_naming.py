#!/usr/bin/env python3
"""Name a release from a cell's manifest, and decide the tag a run publishes.

Every cell of a sweep names the same V8 build (the single-SHA gate), so each
build job can name its own archive from its own ``manifest.json``.  The
release job then checks that every archive carries the same id.

``id`` prints the release id for one manifest.  ``tag`` decides the publish
tag: a run publishes under ``v8-<id>`` unless ``release_tag`` names an
attested re-cut, ``v8-<id>-attested.<n>``.  Any other ``release_tag`` is
refused, so a dispatch cannot publish under an arbitrary name.  An
``-attested.<n>`` tag is a new release with new bytes: V8 builds are not
byte-reproducible, so it never replaces another release's assets.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ATTESTED = re.compile(r"^(v8-.+)-attested\.([1-9][0-9]*)$")


def clean(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-")


def release_meta(manifest: dict, v8_version_input: str = "") -> dict[str, object]:
    """Release id, display version and title for a cell manifest."""
    version = manifest.get("v8_version") or "unknown"
    pair = manifest.get("pair") or {}
    if pair.get("pair_kind") == "chromium-milestone" and pair.get("v8"):
        release_id = clean(f"m{pair['milestone']}-{version}-{pair['v8'][:12]}")
        title = f"M{pair['milestone']} matched V8 {version} ({pair['v8'][:12]})"
    elif pair.get("pair_kind") == "chromium-lkgr" and pair.get("v8"):
        release_id = clean(f"{version}-lkgr-{pair['v8'][:12]}")
        title = f"V8 {version} (Chromium LKGR {pair['v8'][:12]})"
    else:
        release_id = clean(v8_version_input or version)
        title = f"V8 {version}"
    return {"release_id": release_id, "display_version": version, "title": title, "pair": pair}


def publish_tag(release_id: str, release_tag: str = "") -> tuple[str, str]:
    """Return (base tag, publish tag) for a release id and an optional release_tag."""
    base = f"v8-{release_id}"
    if not release_tag or release_tag == base:
        return base, base
    match = ATTESTED.match(release_tag)
    if match and match.group(1) == base:
        return base, release_tag
    raise ValueError(f"release_tag={release_tag} must be {base} or {base}-attested.<n>")


def archive_name(cell_dir_name: str, release_id: str) -> str:
    return f"{cell_dir_name}-{release_id}.zip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("id", help="print the release metadata for one manifest")
    one.add_argument("manifest", type=Path)
    one.add_argument("--v8-version", default="")
    tag = sub.add_parser("tag", help="print base= and publish= tags")
    tag.add_argument("release_id")
    tag.add_argument("release_tag", nargs="?", default="")
    args = parser.parse_args(argv)
    if args.command == "id":
        meta = release_meta(json.loads(args.manifest.read_text(encoding="utf-8")), args.v8_version)
        print(json.dumps(meta, indent=2))
        return 0
    try:
        base, publish = publish_tag(args.release_id, args.release_tag)
    except ValueError as error:
        print(f"::error::{error}")
        return 1
    print(f"base={base}\npublish={publish}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
