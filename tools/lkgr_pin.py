#!/usr/bin/env python3
"""
lkgr_pin.py — prove we can source a Chromium-co-tested Skia/V8/Dawn revision set.

Fetches Chromium's LKGR (Last Known Good Revision) DEPS and extracts the exact
skia/v8/dawn commit SHAs Chromium tested together, emitting a lockfile JSON. This
is the *source of truth* for a truly co-tested pair (proposal DEPS-PAIR / FR).

Caveat (documented in the FR): these are the revisions Chromium expects together;
a standalone Pulp build still needs a reproducible recipe (GN args, sysroot, libc++).
"""
import base64, json, re, urllib.request

DEPS_URL = "https://chromium.googlesource.com/chromium/src/+/lkgr/DEPS?format=TEXT"
COMMIT_URL = "https://chromium.googlesource.com/chromium/src/+/lkgr?format=JSON"
DEPS_META_URL = "https://chromium.googlesource.com/chromium/src/+/lkgr/DEPS?format=JSON"
KEYS = ("skia_revision", "v8_revision", "dawn_revision")


def _gitiles_json(url):
    raw = urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "replace")
    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1]
    return json.loads(raw)


def current_lock():
    commit = _gitiles_json(COMMIT_URL)
    deps_meta = _gitiles_json(DEPS_META_URL)
    raw = urllib.request.urlopen(DEPS_URL, timeout=30).read()
    text = base64.b64decode(raw).decode("utf-8", "replace")
    out = {
        "source": "chromium-lkgr-deps",
        "chromium_revision": commit.get("commit"),
        "chromium_deps_blob": deps_meta.get("id"),
    }
    committer = commit.get("committer") or {}
    if committer.get("time"):
        out["chromium_lkgr_time"] = committer["time"]
    for k in KEYS:
        m = re.search(rf"'{k}':\s*'([0-9a-f]{{40}})'", text)
        out[k.replace("_revision", "")] = m.group(1) if m else None
    # repos the SHAs map to (from DEPS)
    out["repos"] = {
        "skia": "https://skia.googlesource.com/skia.git",
        "v8":   "https://chromium.googlesource.com/v8/v8.git",
        "dawn": "https://dawn.googlesource.com/dawn.git",
    }
    missing = [k for k in ("skia", "v8", "dawn") if not out.get(k)]
    if missing:
        raise SystemExit(f"lkgr_pin: missing DEPS keys: {', '.join(missing)}")
    return out


def main():
    out = current_lock()
    print(json.dumps(out, indent=2))

if __name__ == "__main__":
    main()
