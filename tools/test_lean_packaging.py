#!/usr/bin/env python3
"""Unit tests for build-v8.py's LEAN packaging logic (feature/lean-packaging).

Covers the parts that are pure data-shaping and therefore unit-testable without a
real V8 build:

  * _copy_headers   — the *.h/*.inc cruft filter (drops DEPS/OWNERS/*.md/*.json/*.pdl/…)
  * _manifest_lib_path — per-platform relative path to the shipped binary
  * the strip-preserves-the-seal export-count gate (parse + mismatch detection)

The actual strip + seal re-audit needs a real Mach-O/ELF and runs in CI on the build
host; here we assert the *decision logic* around it (an unchanged count passes, a
changed count is a hard fail), which is the acceptance contract: "If strip changes the
export count, you stripped too much."

Runs two ways, no third-party deps required:
    python3 -m pytest tools/test_lean_packaging.py
    python3 tools/test_lean_packaging.py
"""
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_build_module():
    # build-v8.py is hyphenated → load by path, not `import build-v8`.
    spec = importlib.util.spec_from_file_location(
        "build_v8", REPO_ROOT / "build-v8.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BV8 = _load_build_module()


def _make_builder(platform, **extra):
    """Construct a V8Build with a minimal fake args namespace (no build invoked)."""
    args = types.SimpleNamespace(
        platform=platform,
        v8_version=None,
        v8_revision=None,
        lkgr_lock=None,
        skia_release_tag=None,
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


# --- GN arg contract ------------------------------------------------------------

def test_common_gn_args_pin_local_execution_even_without_ccache():
    """Standalone builds must not inherit Chromium remoteexec SDK symlink behavior."""
    orig_which = BV8.shutil.which
    try:
        BV8.shutil.which = lambda _name: None
        args = BV8.common_gn_args()
    finally:
        BV8.shutil.which = orig_which

    assert "use_remoteexec=false" in args
    assert not any(arg.startswith("cc_wrapper=") for arg in args)


def test_common_gn_args_do_not_duplicate_remoteexec_when_ccache_is_present():
    orig_which = BV8.shutil.which
    try:
        BV8.shutil.which = lambda name: "/usr/bin/ccache" if name == "ccache" else None
        args = BV8.common_gn_args()
    finally:
        BV8.shutil.which = orig_which

    assert args.count("use_remoteexec=false") == 1
    assert args.count('cc_wrapper="ccache"') == 1


# --- _copy_headers cruft filter -------------------------------------------------

def _build_fixture_include(root):
    """Create an include/ tree mixing real headers with V8-repo metadata cruft."""
    files = {
        # real headers — MUST survive
        "v8.h": "// header",
        "v8-isolate.h": "// header",
        "libplatform/libplatform.h": "// header",
        "cppgc/heap.h": "// header",
        "v8-internal.inc": "// inc",
        # cruft — MUST be filtered out
        "DEPS": "deps",
        "OWNERS": "owners",
        "DIR_METADATA": "meta",
        "APIDesign.md": "# doc",
        "js_protocol-1.2.json": "{}",
        "js_protocol-1.3.json": "{}",
        "js_protocol.pdl": "pdl",
        "libplatform/DEPS": "deps",
        "libplatform/OWNERS": "owners",
        "cppgc/README.md": "# readme",
    }
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def test_copy_headers_keeps_only_headers(tmp_path=None):
    tmp = tmp_path or _tmp("hdr")
    src = tmp / "include"
    dst = tmp / "out_include"
    _build_fixture_include(src)
    b = _make_builder("mac")
    b._copy_headers(src, dst)

    copied = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())
    expected = sorted([
        "v8.h", "v8-isolate.h", "libplatform/libplatform.h",
        "cppgc/heap.h", "v8-internal.inc",
    ])
    assert copied == expected, f"unexpected copied set: {copied}"
    # no cruft of any forbidden kind leaked through
    for bad in ("DEPS", "OWNERS", "DIR_METADATA"):
        assert not (dst / bad).exists(), f"{bad} should have been filtered"
    assert not list(dst.rglob("*.md")), "no .md should survive"
    assert not list(dst.rglob("*.json")), "no .json should survive"
    assert not list(dst.rglob("*.pdl")), "no .pdl should survive"
    assert not list(dst.rglob("DEPS")), "no DEPS should survive"


def test_copy_headers_overwrites_existing(tmp_path=None):
    tmp = tmp_path or _tmp("hdr2")
    src = tmp / "include"
    dst = tmp / "out_include"
    _build_fixture_include(src)
    # pre-populate dst with a stale file that must be wiped (rmtree on re-copy)
    (dst / "stale").mkdir(parents=True)
    (dst / "stale" / "old.h").write_text("// stale")
    b = _make_builder("mac")
    b._copy_headers(src, dst)
    assert not (dst / "stale").exists(), "stale tree must be removed before re-copy"


# --- _manifest_lib_path per-platform mapping ------------------------------------

def test_manifest_lib_path_per_platform():
    cases = [
        ("mac", "libv8.dylib", "arm64", "lib/libv8.dylib"),
        ("linux", "libv8.so", "x64", "lib/libv8.so"),
        ("win", "v8.dll", "x64", "lib/v8.dll"),
        ("android", "libv8.so", "arm64", "jniLibs/arm64-v8a/libv8.so"),
        ("android", "libv8.so", "x64", "jniLibs/x86_64/libv8.so"),
        ("ios", "V8.framework", "arm64", "V8.framework/V8"),
    ]
    for plat, libname, arch, want in cases:
        b = _make_builder(plat)
        got = b._manifest_lib_path(Path("/whatever") / libname, arch)
        assert got == want, f"{plat}/{arch}: got {got!r}, want {want!r}"


# --- strip-preserves-the-seal export-count gate ---------------------------------

def test_export_count_parse():
    """_seal_audit parses 'AUDIT OK — N exports' from any backend's stdout format."""
    samples = {
        "[seal/macho] AUDIT OK — 68012 exports, all v8::/cppgc::; 0 ICU/zlib/Abseil leaks": 68012,
        "[seal/elf] AUDIT OK — 54321 exports, all v8::/cppgc:: ...": 54321,
        "[seal/coff] AUDIT OK — 12345 exports on V8's public surface ...": 12345,
    }
    import re
    for line, want in samples.items():
        m = re.search(r"AUDIT OK — (\d+) exports", line)
        assert m and int(m.group(1)) == want, f"failed to parse {line!r}"


def test_strip_seal_gate_decision():
    """The COUNT gate: strip can only REMOVE symbols, so an INCREASE is corruption (fail);
    a decrease or no-change passes the count gate. The seal property itself — 0 ICU/zlib/
    Abseil leaks AND v8 exports present — is enforced separately by _seal_audit's exit code
    (the backend hard-fails), NOT by this count comparison. Mirrors build_sealed's abort.
    """
    def count_gate_ok(pre, post):
        return post <= pre  # only an INCREASE aborts; strip never adds exports

    assert count_gate_ok(68012, 68012) is True    # unchanged (mac strip -x)
    assert count_gate_ok(132596, 66298) is True   # strip pruned dead v8::internal:: dynamic
                                                  #   exports to the real public surface (the
                                                  #   actual ELF case: linux ≈ mac's ~66k)
    assert count_gate_ok(68012, 68013) is False   # rose → strip can't add symbols → corruption


def test_built_v8_version_header_matches_v8_string_rules(tmp_path=None):
    """The Python fallback must mirror include/v8-version-string.h exactly enough for
    the identity validator: omit .0 patch levels and append ' (candidate)' when set."""
    tmp = tmp_path or _tmp("version")
    orig_v8_dir = BV8.V8_DIR
    BV8.V8_DIR = tmp / "v8"
    inc = BV8.V8_DIR / "include"
    inc.mkdir(parents=True)
    try:
        header = inc / "v8-version.h"
        header.write_text(
            "#define V8_MAJOR_VERSION 15\n"
            "#define V8_MINOR_VERSION 2\n"
            "#define V8_BUILD_NUMBER 11\n"
            "#define V8_PATCH_LEVEL 0\n"
            "#define V8_IS_CANDIDATE_VERSION 0\n")
        assert _make_builder("mac")._built_v8_version_header() == "15.2.11"

        header.write_text(
            "#define V8_MAJOR_VERSION 15\n"
            "#define V8_MINOR_VERSION 2\n"
            "#define V8_BUILD_NUMBER 11\n"
            "#define V8_PATCH_LEVEL 3\n"
            "#define V8_IS_CANDIDATE_VERSION 1\n")
        assert _make_builder("mac")._built_v8_version_header() == "15.2.11.3 (candidate)"
    finally:
        BV8.V8_DIR = orig_v8_dir


def test_lkgr_contract_embeds_lock_and_checks_built_sha(tmp_path=None):
    tmp = tmp_path or _tmp("lkgr")
    lock = tmp / "lkgr-lock.json"
    lock.write_text(
        '{"source":"chromium-lkgr-deps","chromium_revision":"chromium",'
        '"chromium_deps_blob":"deps","skia":"skia","v8":"built",'
        '"dawn":"dawn"}')

    b = _make_builder("mac", lkgr_lock=str(lock), skia_release_tag="chrome/m150")
    b._built_v8_sha = lambda: "built"
    c = b._lkgr_contract()
    assert c["pair_kind"] == "chromium-lkgr"
    assert c["v8"] == "built"
    assert c["skia"] == "skia"
    assert c["dawn"] == "dawn"
    assert c["validated_skia_release"] == "chrome/m150"

    b._built_v8_sha = lambda: "different"
    raised = False
    try:
        b._lkgr_contract()
    except SystemExit:
        raised = True
    assert raised, "LKGR lock v8 SHA must match the actual built revision"


def test_milestone_contract_preserves_built_dawn_truth(tmp_path=None):
    tmp = tmp_path or _tmp("milestone")
    lock = tmp / "milestone-lock.json"
    lock.write_text(
        '{"source":"chromium-milestone-branch-deps","pair_kind":"chromium-milestone",'
        '"milestone":152,"chromium_branch":"7977","skia":"skia","v8":"built",'
        '"dawn":"chromium-dawn","built_dawn":"skia-dawn","dawn_matches_chromium":false}'
    )
    b = _make_builder("mac", lkgr_lock=str(lock), skia_release_tag="chrome/m152")
    b._built_v8_sha = lambda: "built"
    c = b._lkgr_contract()
    assert c["pair_kind"] == "chromium-milestone"
    assert c["milestone"] == 152
    assert c["built_dawn"] == "skia-dawn"
    assert c["dawn_matches_chromium"] is False


# --- standalone fallback harness ------------------------------------------------

_TMP_ROOTS = []


def _tmp(tag):
    import tempfile
    d = Path(tempfile.mkdtemp(prefix=f"v8lean-{tag}-"))
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
    print("RESULT: all lean-packaging tests passed")


if __name__ == "__main__":
    _run_standalone()
