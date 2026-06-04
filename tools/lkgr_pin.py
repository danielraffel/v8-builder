#!/usr/bin/env python3
"""
lkgr_pin.py — prove we can source a Chromium-co-tested Skia/V8/Dawn revision set.

Fetches Chromium's LKGR (Last Known Good Revision) DEPS and extracts the exact
skia/v8/dawn commit SHAs Chromium tested together, emitting a lockfile JSON. This
is the *source of truth* for a truly co-tested pair (proposal DEPS-PAIR / FR).

Caveat (documented in the FR): these are the revisions Chromium expects together;
a standalone Pulp build still needs a reproducible recipe (GN args, sysroot, libc++).
"""
import base64, json, re, sys, urllib.request

DEPS_URL = "https://chromium.googlesource.com/chromium/src/+/lkgr/DEPS?format=TEXT"
KEYS = ("skia_revision", "v8_revision", "dawn_revision")

def main():
    raw = urllib.request.urlopen(DEPS_URL, timeout=30).read()
    text = base64.b64decode(raw).decode("utf-8", "replace")
    out = {"source": "chromium-lkgr-deps"}
    for k in KEYS:
        m = re.search(rf"'{k}':\s*'([0-9a-f]{{40}})'", text)
        out[k.replace("_revision", "")] = m.group(1) if m else None
    # repos the SHAs map to (from DEPS)
    out["repos"] = {
        "skia": "https://skia.googlesource.com/skia.git",
        "v8":   "https://chromium.googlesource.com/v8/v8.git",
        "dawn": "https://dawn.googlesource.com/dawn.git",
    }
    print(json.dumps(out, indent=2))

if __name__ == "__main__":
    main()
