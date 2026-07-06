#!/usr/bin/env python3
"""Unit tests for LKGR watcher dispatch decisions."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lkgr_decision  # noqa: E402


def _lock(skia="skia-a", v8="v8-a", dawn="dawn-a"):
    return {
        "source": "chromium-lkgr-deps",
        "chromium_revision": "chromium-a",
        "chromium_deps_blob": "deps-a",
        "skia": skia,
        "v8": v8,
        "dawn": dawn,
    }


def _manifest(skia="skia-a", v8="v8-a", dawn="dawn-a"):
    return {"pair": {"skia": skia, "v8": v8, "dawn": dawn}}


def test_matching_tuple_does_not_dispatch():
    result = lkgr_decision.decide(_lock(), _manifest())
    assert result["should_dispatch"] is False
    assert result["reason"] == "tuple-unchanged"


def test_changed_v8_dispatches():
    result = lkgr_decision.decide(_lock(v8="v8-b"), _manifest(v8="v8-a"))
    assert result["should_dispatch"] is True
    assert result["reason"] == "tuple-changed"


def test_changed_skia_dispatches():
    result = lkgr_decision.decide(_lock(skia="skia-b"), _manifest(skia="skia-a"))
    assert result["should_dispatch"] is True


def test_changed_dawn_dispatches():
    result = lkgr_decision.decide(_lock(dawn="dawn-b"), _manifest(dawn="dawn-a"))
    assert result["should_dispatch"] is True


def test_missing_last_manifest_dispatches():
    result = lkgr_decision.decide(_lock(), None)
    assert result["should_dispatch"] is True


def test_unrelated_chromium_revision_change_does_not_dispatch():
    cur = _lock()
    cur["chromium_revision"] = "chromium-b"
    cur["chromium_deps_blob"] = "deps-b"
    result = lkgr_decision.decide(cur, _manifest())
    assert result["should_dispatch"] is False


def test_force_dispatches_even_when_tuple_matches():
    result = lkgr_decision.decide(_lock(), _manifest(), force=True)
    assert result["should_dispatch"] is True
    assert result["reason"] == "forced"


def test_github_output_writer():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "outputs"
        result = lkgr_decision.decide(_lock(), None)
        lkgr_decision._write_github_outputs(out, result, _lock())
        text = out.read_text()
        assert "newer=yes\n" in text
        assert "v8=v8-a\n" in text


def _run_standalone():
    import types
    g = dict(globals())
    tests = sorted(n for n, f in g.items()
                   if n.startswith("test_") and isinstance(f, types.FunctionType))
    fails = 0
    print("tools/test_lkgr_decision.py")
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
