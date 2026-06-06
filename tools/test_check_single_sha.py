#!/usr/bin/env python3
"""Unit tests for tools/check_single_sha.py — the single-V8-SHA release gate.

Runs two ways, no third-party deps required:
    python3 -m pytest tools/test_check_single_sha.py     # if pytest is installed
    python3 tools/test_check_single_sha.py               # standalone fallback

The gate must PASS when every per-platform artifact names the same V8 build
(`v8_version`, `pair.built_revision`, `pair.v8` agree across all manifests) and
must FAIL (SystemExit / nonzero) when any cell names a different revision.
"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_single_sha  # noqa: E402


@contextlib.contextmanager
def _silenced():
    """Swallow the gate's own stdout/stderr diagnostics during a test."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield


def _write_manifest(d, platform, arch, v8_version, built_revision, v8_sha):
    cell = Path(d) / f"{platform}-{arch}"
    cell.mkdir(parents=True, exist_ok=True)
    (cell / "manifest.json").write_text(json.dumps({
        "platform": platform,
        "arch": arch,
        "v8_version": v8_version,
        "pair": {"built_revision": built_revision, "v8": v8_sha},
    }))


def _matching_set(d):
    _write_manifest(d, "mac", "arm64", "15.1.0", "rev-aaaa", "sha-1111")
    _write_manifest(d, "linux", "x64", "15.1.0", "rev-aaaa", "sha-1111")
    _write_manifest(d, "win", "x64", "15.1.0", "rev-aaaa", "sha-1111")


def test_matching_revisions_pass():
    with tempfile.TemporaryDirectory() as d:
        _matching_set(d)
        # main() returns normally (no SystemExit) when all agree.
        with _silenced():
            check_single_sha.main(d)   # must not raise


def test_one_mismatched_v8_sha_fails():
    with tempfile.TemporaryDirectory() as d:
        _matching_set(d)
        # one cell at a DIFFERENT lkgr v8 sha
        _write_manifest(d, "android", "arm64", "15.1.0", "rev-aaaa", "sha-DIFFERENT")
        raised = False
        try:
            with _silenced():
                check_single_sha.main(d)
        except SystemExit as e:
            raised = True
            assert e.code == 1
        assert raised, "expected SystemExit(1) on mixed v8 sha"


def test_one_mismatched_v8_version_fails():
    with tempfile.TemporaryDirectory() as d:
        _matching_set(d)
        _write_manifest(d, "win", "arm64", "15.2.0", "rev-aaaa", "sha-1111")
        raised = False
        try:
            with _silenced():
                check_single_sha.main(d)
        except SystemExit as e:
            raised = True
            assert e.code == 1
        assert raised, "expected SystemExit(1) on mixed v8_version"


def test_one_mismatched_built_revision_fails():
    with tempfile.TemporaryDirectory() as d:
        _matching_set(d)
        _write_manifest(d, "linux", "arm64", "15.1.0", "rev-bbbb", "sha-1111")
        raised = False
        try:
            with _silenced():
                check_single_sha.main(d)
        except SystemExit as e:
            raised = True
            assert e.code == 1
        assert raised, "expected SystemExit(1) on mixed built_revision"


def test_no_manifests_fails():
    with tempfile.TemporaryDirectory() as d:
        raised = False
        try:
            with _silenced():
                check_single_sha.main(d)
        except SystemExit as e:
            raised = True
            # this path exits with a string message (code is the message, truthy)
            assert e.code
        assert raised, "expected SystemExit when no manifest.json found"


def test_single_manifest_passes():
    with tempfile.TemporaryDirectory() as d:
        _write_manifest(d, "mac", "arm64", "15.1.0", "rev-aaaa", "sha-1111")
        with _silenced():
            check_single_sha.main(d)   # one artifact trivially agrees with itself


# ---- standalone runner (no pytest) ----------------------------------------
def _run_standalone():
    import types
    g = dict(globals())
    tests = sorted(n for n, f in g.items()
                   if n.startswith("test_") and isinstance(f, types.FunctionType))
    fails = 0
    print("tools/test_check_single_sha.py")
    for name in tests:
        try:
            g[name]()
            print(f"  [ok] {name}")
        except AssertionError as e:
            fails += 1
            print(f"  [FAIL] {name}: {e}")
    if fails:
        print(f"\n{fails} test(s) FAILED")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    _run_standalone()
