#!/usr/bin/env python3
"""
check_single_sha.py — intra-repo single-V8-SHA release gate (task #29 / FR1 2-D pairs).

A release must NOT mix V8 revisions across its per-platform artifacts. This walks every
`manifest.json` under the given dir (the downloaded build artifacts) and asserts they all
name the SAME V8 build: `v8_version`, `pair.built_revision`, and `pair.v8` (the LKGR v8
SHA) must agree across all cells. A mismatch fails the release.

Usage: check_single_sha.py <artifacts_dir>
"""
import json
import sys
from pathlib import Path


def main(root):
    manifests = sorted(Path(root).rglob("manifest.json"))
    if not manifests:
        sys.exit(f"check_single_sha: no manifest.json under {root}")
    groups = {}
    for m in manifests:
        d = json.loads(m.read_text())
        pair = d.get("pair") or {}
        sig = (d.get("v8_version"), pair.get("built_revision"), pair.get("v8"))
        groups.setdefault(sig, []).append(f"{d.get('platform')}/{d.get('arch')}")
    if len(groups) != 1:
        print("SINGLE-SHA GATE FAIL — mixed V8 revisions across artifacts:", file=sys.stderr)
        for sig, cells in groups.items():
            print(f"   v8_version={sig[0]} built={sig[1]} lkgr_v8={sig[2]} : {cells}",
                  file=sys.stderr)
        sys.exit(1)
    (ver, built, lkgr_v8), cells = next(iter(groups.items()))
    total = sum(len(c) for c in groups.values())
    print(f"SINGLE-SHA OK — all {total} artifacts at V8 {ver} "
          f"(built {built}, lkgr_v8 {lkgr_v8}); cells: {cells}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
