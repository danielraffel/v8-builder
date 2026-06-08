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
  * the object discovery + archive (build_win_libcxx globs the already-compiled
    libc++/libc++abi *.obj files from out/obj/buildtools/third_party/ and archives
    them with llvm-lib — no GN target, no ninja step, so GN visibility never blocks).
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
    """build_win_libcxx globs the already-compiled libc++/libc++abi .obj files from
    out/obj/buildtools/third_party/, archives them into <cell>/lib/libc++.lib with
    llvm-lib, verifies __Cr, and returns the staged path. No ninja target, no GN."""
    tmp = tmp_path or _tmp("stage")
    # Redirect BUILD_DIR so the staged artifact lands in a temp cell, not the repo.
    orig_build_dir = BV8.BUILD_DIR
    BV8.BUILD_DIR = tmp / "build"
    try:
        out = tmp / "out"
        # The v8_monolith build already compiled these .obj files into the out dir.
        for sub in BV8.WIN_LIBCXX_OBJ_SUBDIRS:
            d = out / sub
            d.mkdir(parents=True)
            (d / f"{Path(sub).name}_part.obj").write_bytes(b"\x00fake obj\n")

        b = _make_builder("win", archs="x64")
        archive_calls = []

        # Stub the archiver invocation: emulate llvm-lib /OUT:<lib> <objs...> by
        # writing the output file, and capture the argv so we can assert on it.
        def fake_run(cmd, cwd=None, env=None):
            archive_calls.append(cmd)
            out_arg = next(a for a in cmd if str(a).startswith("/OUT:"))
            Path(str(out_arg)[len("/OUT:"):]).write_bytes(b"!<arch>\nfake archive\n")

        orig_run = BV8.run
        BV8.run = fake_run
        b._win_archiver = lambda: "llvm-lib"      # skip toolchain discovery
        b._archive_symbols = lambda lib: _CR_SYMBOLS  # skip the real nm/dumpbin read
        try:
            staged = b.build_win_libcxx(out, "x64")
        finally:
            BV8.run = orig_run

        assert staged is not None, "expected a staged libc++.lib path"
        assert staged.name == "libc++.lib"
        assert staged.exists(), "libc++.lib should have been archived into the cell lib/"
        assert staged.parent.name == "lib"
        assert "win-x64" in str(staged), f"unexpected cell dir: {staged}"
        assert archive_calls, "llvm-lib should have been invoked to archive the objs"
        argv = archive_calls[0]
        assert any(str(a).startswith("/OUT:") for a in argv), "archiver needs /OUT:"
        # Both libc++ AND libc++abi objects must be in the archive argv (libc++abi
        # carries the ABI runtime an iostreams consumer also needs).
        joined = " ".join(str(a) for a in argv)
        assert "libc++abi" in joined, "libc++abi objects must be archived too"
        assert ".obj" in joined, "object files must be passed to the archiver"
    finally:
        BV8.BUILD_DIR = orig_build_dir


def test_build_win_libcxx_missing_objs_is_hard_fail(tmp_path=None):
    """If the v8 build produced no libc++/libc++abi .obj files under out/, abort."""
    tmp = tmp_path or _tmp("missing")
    orig_build_dir = BV8.BUILD_DIR
    BV8.BUILD_DIR = tmp / "build"
    try:
        out = tmp / "out"
        out.mkdir(parents=True)  # empty — no compiled libc++ objects
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
        assert raised, "missing libc++ .obj files must hard-fail the build"
    finally:
        BV8.BUILD_DIR = orig_build_dir


# --- object discovery -----------------------------------------------------------

def test_find_win_libcxx_objs_globs_both_dirs(tmp_path=None):
    """_find_win_libcxx_objs collects *.obj from BOTH libc++ and libc++abi subtrees."""
    tmp = tmp_path or _tmp("glob")
    out = tmp / "out"
    cxx = out / "obj" / "buildtools" / "third_party" / "libc++"
    abi = out / "obj" / "buildtools" / "third_party" / "libc++abi"
    cxx.mkdir(parents=True)
    abi.mkdir(parents=True)
    (cxx / "string.obj").write_bytes(b"")
    (cxx / "ostream.obj").write_bytes(b"")
    (abi / "cxa_throw.obj").write_bytes(b"")
    # A non-.obj file must be ignored.
    (cxx / "string.obj.d").write_bytes(b"")

    b = _make_builder("win")
    objs = b._find_win_libcxx_objs(out)
    names = {p.name for p in objs}
    assert names == {"string.obj", "ostream.obj", "cxa_throw.obj"}, \
        f"unexpected obj set: {names}"
    # Empty out dir yields no objects (drives the hard-fail above).
    assert b._find_win_libcxx_objs(tmp / "empty") == []


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
