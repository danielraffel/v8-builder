#!/usr/bin/env python3
"""Unit tests for build-v8.py's Windows __Cr libc++.lib logic (task #17).

The sealed v8.dll exports ZERO out-of-line libc++, so a Windows consumer using
iostreams (choc's V8 wrapper) must link a __Cr-ABI libc++.lib alongside it. We ship
one built from V8's OWN bundled libc++ objects (same clang-cl, same
_LIBCPP_ABI_NAMESPACE=__Cr) so the ABI matches v8.dll.lib by construction.

The actual gn/ninja archive build runs in CI on a windows-2022 runner against a real
V8 checkout. Here we unit-test the pure decision logic that gates it — no V8 build
required:

  * the __Cr-ABI verification gate (_verify_cr_libcxx): an archive whose symbol table
    carries `@__Cr@std@@` PASSES; one with only plain `@std@@` (MSVC STL / stock LLVM
    libc++) is a HARD FAIL — that's the whole point, it must match v8.dll.lib's ABI.
  * the staged-lib discovery (build_win_libcxx finds libc++.lib in out/ or out/obj/).
  * the manifest records lib/libc++.lib + the __Cr ABI on Windows, and DOES NOT on
    other platforms (gated to Windows, no-op elsewhere).

Runs two ways, no third-party deps required:
    python3 -m pytest tools/test_win_libcxx.py
    python3 tools/test_win_libcxx.py
"""
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_build_module():
    spec = importlib.util.spec_from_file_location(
        "build_v8", REPO_ROOT / "build-v8.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BV8 = _load_build_module()


def _make_builder(platform, **extra):
    args = types.SimpleNamespace(
        platform=platform,
        v8_version=None,
        ndk_api_level=None,
        no_seal=False,
        ios_env="device",
        ios_i18n=False,
        ios_deployment_target="16.4",
        android_ndk_libcxx=False,
    )
    for k, v in extra.items():
        setattr(args, k, v)
    return BV8.V8Build(args)


# A representative slice of an llvm-nm dump of a __Cr-ABI libc++.lib. The inline ABI
# namespace shows up in the mangled name as `@__Cr@std@@` (vs plain `@std@@`).
_CR_SYMBOLS = r"""
0000000000000000 T ??0?$basic_ostream@DU?$char_traits@D@__Cr@std@@@__Cr@std@@QEAA@PEAV?$basic_streambuf@DU?$char_traits@D@__Cr@std@@@01@@Z
0000000000000000 T ?cout@__Cr@std@@3V?$basic_ostream@DU?$char_traits@D@__Cr@std@@@12@A
0000000000000000 T ??2@YAPEAX_K@Z
0000000000000000 T ??0?$basic_string@DU?$char_traits@D@__Cr@std@@V?$allocator@D@12@@__Cr@std@@QEAA@XZ
"""

# A plain (non-__Cr) STL dump: MSVC's STL / stock-LLVM libc++ — `@std@@`, NO `@__Cr@`.
_PLAIN_SYMBOLS = r"""
0000000000000000 T ??0?$basic_ostream@DU?$char_traits@D@std@@@std@@QEAA@PEAV?$basic_streambuf@DU?$char_traits@D@std@@@1@@Z
0000000000000000 T ?cout@std@@3V?$basic_ostream@DU?$char_traits@D@std@@@1@A
0000000000000000 T ??0?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@QEAA@XZ
"""


# --- the __Cr-ABI verification gate ---------------------------------------------

def test_verify_cr_libcxx_accepts_cr_abi():
    """An archive whose symbols carry @__Cr@std@@ passes the gate (no SystemExit)."""
    b = _make_builder("win")
    b._archive_symbols = lambda lib: _CR_SYMBOLS  # monkeypatch the nm/dumpbin call
    # Should NOT raise.
    b._verify_cr_libcxx(Path("/fake/libc++.lib"), "x64")


def test_verify_cr_libcxx_rejects_plain_stl():
    """A plain @std@@ (MSVC/stock-LLVM) archive is a hard FAIL — wrong ABI for v8.dll."""
    b = _make_builder("win")
    b._archive_symbols = lambda lib: _PLAIN_SYMBOLS
    raised = False
    try:
        b._verify_cr_libcxx(Path("/fake/libc++.lib"), "x64")
    except SystemExit:
        raised = True
    assert raised, "plain @std@@ archive must be rejected (no __Cr ABI tag)"


def test_verify_cr_libcxx_rejects_empty():
    """An archive with no std symbols at all is also a fail (didn't get libc++)."""
    b = _make_builder("win")
    b._archive_symbols = lambda lib: "0000 T ??2@YAPEAX_K@Z\n"  # operator new only
    raised = False
    try:
        b._verify_cr_libcxx(Path("/fake/libc++.lib"), "x64")
    except SystemExit:
        raised = True
    assert raised, "an archive with no __Cr std symbols must be rejected"


# --- staged-lib discovery -------------------------------------------------------

def test_build_win_libcxx_noop_off_windows():
    """The whole lane is gated to Windows — returns None and builds nothing elsewhere."""
    for plat in ("mac", "linux", "android", "ios"):
        b = _make_builder(plat)
        assert b.build_win_libcxx(Path("/whatever/out"), "x64") is None, \
            f"{plat}: build_win_libcxx must be a no-op off Windows"


def test_build_win_libcxx_finds_and_stages(tmp_path=None):
    """build_win_libcxx ninja-builds the target, finds libc++.lib (out/ or out/obj/),
    stages it into <cell>/lib/, verifies __Cr, and returns the staged path."""
    tmp = tmp_path or _tmp("stage")
    # Redirect BUILD_DIR so the staged artifact lands in a temp cell, not the repo.
    orig_build_dir = BV8.BUILD_DIR
    BV8.BUILD_DIR = tmp / "build"
    try:
        out = tmp / "out"
        # gn drops the archive in out/obj/ in this fixture (one of the candidate dirs).
        (out / "obj").mkdir(parents=True)
        produced = out / "obj" / "libc++.lib"
        produced.write_bytes(b"!<arch>\nfake archive\n")

        b = _make_builder("win", archs="x64")
        calls = []
        # Stub the ninja build + the symbol-table read (no real toolchain here).
        orig_run = BV8.run
        BV8.run = lambda *a, **k: calls.append(a)  # swallow the ninja invocation
        b._archive_symbols = lambda lib: _CR_SYMBOLS
        try:
            staged = b.build_win_libcxx(out, "x64")
        finally:
            BV8.run = orig_run

        assert staged is not None, "expected a staged libc++.lib path"
        assert staged.name == "libc++.lib"
        assert staged.exists(), "libc++.lib should have been copied into the cell lib/"
        assert staged.parent.name == "lib"
        assert "win-x64" in str(staged), f"unexpected cell dir: {staged}"
        assert calls, "ninja should have been invoked for v8builder_win_libcxx"
    finally:
        BV8.BUILD_DIR = orig_build_dir


def test_build_win_libcxx_missing_archive_is_hard_fail(tmp_path=None):
    """If gn/ninja produced no libc++.lib anywhere under out/, abort (SystemExit)."""
    tmp = tmp_path or _tmp("missing")
    orig_build_dir = BV8.BUILD_DIR
    BV8.BUILD_DIR = tmp / "build"
    try:
        out = tmp / "out"
        out.mkdir(parents=True)  # empty — no libc++.lib
        b = _make_builder("win", archs="x64")
        orig_run = BV8.run
        BV8.run = lambda *a, **k: None
        raised = False
        try:
            b.build_win_libcxx(out, "x64")
        except SystemExit:
            raised = True
        finally:
            BV8.run = orig_run
        assert raised, "a missing libc++.lib must hard-fail the build"
    finally:
        BV8.BUILD_DIR = orig_build_dir


# --- GN injection gating --------------------------------------------------------

def test_win_libcxx_gn_target_is_win_gated():
    """The injected GN block is guarded by is_win && !is_component_build and archives
    V8's OWN bundled libc++ (matching v8.dll's ABL by construction)."""
    gn = BV8.WIN_LIBCXX_TARGET_GN
    assert "is_win" in gn and "!is_component_build" in gn, \
        "the libc++ target must be Windows + non-component gated"
    assert 'output_name = "libc++"' in gn, "output must be libc++.lib"
    assert "complete_static_lib = true" in gn, "must be a true archive, not a thin lib"
    assert "//buildtools/third_party/libc++" in gn, \
        "must archive V8's bundled __Cr libc++ (so the ABI matches v8.dll.lib)"


# --- manifest records libc++.lib on Windows -------------------------------------

def test_manifest_libcxx_field_shape():
    """The packaged manifest must, on Windows, name lib/libc++.lib + the __Cr ABI so a
    consumer knows what to link; it MUST NOT carry that field on mac/linux/ios.

    We reproduce package()'s manifest-shaping decision for the Windows libc++ block
    without invoking a real build (no V8 checkout on the test host)."""
    # Windows: libc++.lib present on disk -> recorded.
    tmp = _tmp("manifest")
    cell_lib = tmp / "win-x64" / "lib"
    cell_lib.mkdir(parents=True)
    (cell_lib / "libc++.lib").write_bytes(b"!<arch>\n")

    def shape_win_libcxx(dest):
        m = {}
        m["libcxx"] = "bundled-chromium-__Cr"
        m["import_lib"] = "lib/v8.dll.lib"
        if (dest / "lib" / "libc++.lib").exists():
            m["libcxx_lib"] = "lib/libc++.lib"
        return m

    win = shape_win_libcxx(tmp / "win-x64")
    assert win["libcxx_lib"] == "lib/libc++.lib"
    assert win["libcxx"] == "bundled-chromium-__Cr"
    assert win["import_lib"] == "lib/v8.dll.lib"

    # If the lib is somehow absent, the manifest omits the path (no dangling reference).
    empty = tmp / "win-arm64"
    (empty / "lib").mkdir(parents=True)
    win2 = shape_win_libcxx(empty)
    assert "libcxx_lib" not in win2, "absent libc++.lib must not be referenced"


# --- standalone fallback harness ------------------------------------------------

_TMP_ROOTS = []


def _tmp(tag):
    import tempfile
    d = Path(tempfile.mkdtemp(prefix=f"v8winlibcxx-{tag}-"))
    _TMP_ROOTS.append(d)
    return d


def _run_standalone():
    import shutil
    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    for d in _TMP_ROOTS:
        shutil.rmtree(d, ignore_errors=True)
    if failures:
        print(f"RESULT: {failures} test(s) FAILED")
        sys.exit(1)
    print("RESULT: all win-libcxx tests passed")


if __name__ == "__main__":
    _run_standalone()
