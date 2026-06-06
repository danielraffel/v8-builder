#!/usr/bin/env python3
"""Prefetch download_from_google_storage objects via HTTPS (curl) to dodge a
flaky-network gsutil/gs:// CLOSE_WAIT wedge.

Background (2026-06-05, iOS lane): on an unstable network, gsutil's boto `gs://`
transport wedges every download in CLOSE_WAIT (even a single serial cp), while
plain HTTPS to `storage.googleapis.com` (and CIPD's HTTPS transport) work fine.
The V8 clang/rust toolchains have their own HTTPS updaters
(tools/clang/scripts/update.py, tools/rust/update_rust.py); `gn` and other CIPD
deps fetch fine via `cipd ensure`. This helper covers the remaining
`download_from_google_storage` (.sha1-named) objects: for each <path>.sha1 with a
bucket, it fetches https://storage.googleapis.com/<bucket>/<sha1> -> <path> and
verifies the sha1. gclient's gsutil hooks then find <path> present and skip the
wedge-prone download. Auth-gated buckets (gcmole/jsfunfuzz/bazel — dev-only tools
not needed to build the sealed framework) 403 over anonymous HTTPS and are
skipped; that is expected and harmless for the build.

Usage: prefetch_gcs_https.py [--arm64-only]   (env V8_DIR overrides the checkout)
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

V8 = Path(os.environ.get(
    "V8_DIR",
    Path(__file__).resolve().parent.parent / "build" / "src" / "v8"))

# (bucket, sha1_file) pairs from DEPS download_from_google_storage hooks.
HOOKS = [
    ("chromium-v8-prebuilt-bazel/linux", "tools/bazel/bazel.sha1"),
    ("chromium-browser-clang", "tools/clang/dsymutil/bin/dsymutil.arm64.sha1"),
    ("chromium-browser-clang", "tools/clang/dsymutil/bin/dsymutil.x64.sha1"),
    ("chrome-v8-gcmole", "tools/gcmole/gcmole-tools.tar.gz.sha1"),
    ("chrome-v8-jsfunfuzz", "tools/jsfunfuzz/jsfunfuzz.tar.gz.sha1"),
    ("chromium-v8/llvm/arm64", "tools/sanitizers/linux/arm64/llvm-symbolizer.sha1"),
    ("v8-wasm-spec-tests", "test/wasm-spec-tests/tests.tar.gz.sha1"),
    ("v8-wasm-spec-tests", "test/wasm-js/tests.tar.gz.sha1"),
    ("chromium-browser-clang/ciopfs", "build/ciopfs.sha1"),
]


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run(
        ["curl", "-sS", "--retry", "8", "--retry-delay", "3", "--retry-all-errors",
         "--max-time", "300", "-o", str(out), url]).returncode
    return rc == 0


def main():
    only_arm = "--arm64-only" in sys.argv
    ok, skipped, failed = 0, 0, 0
    for bucket, sha1_file in HOOKS:
        # Skip clearly-non-arm64-mac artifacts to save time (this is a mac arm64 iOS host).
        if only_arm and ("x64" in sha1_file or "linux" in sha1_file or "ciopfs" in sha1_file):
            print(f"skip (not arm64-mac): {sha1_file}")
            skipped += 1
            continue
        sf = V8 / sha1_file
        if not sf.exists():
            print(f"no sha1 file (not in this checkout): {sha1_file}")
            skipped += 1
            continue
        target = V8 / sha1_file[:-len(".sha1")]
        want = sf.read_text().strip()
        if target.exists() and sha1(target) == want:
            print(f"present OK: {target.relative_to(V8)}")
            ok += 1
            continue
        url = f"https://storage.googleapis.com/{bucket}/{want}"
        print(f"fetch {url} -> {target.relative_to(V8)}")
        if fetch(url, target) and sha1(target) == want:
            print(f"  OK sha1 verified")
            ok += 1
        else:
            print(f"  FAILED (sha mismatch or download error)")
            failed += 1
    print(f"\nprefetch: ok={ok} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
