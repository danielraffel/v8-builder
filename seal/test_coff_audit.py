#!/usr/bin/env python3
"""Unit tests for seal/coff.py's export classifier (PE/COFF / Windows).

Runs two ways, no third-party deps required:
    python3 -m pytest seal/test_coff_audit.py     # if pytest is installed
    python3 seal/test_coff_audit.py               # standalone fallback

MSVC mangles the namespace chain right-to-left, so V8's surface appears as
`@v8@@` / `@cppgc@@` / `@v8_inspector@@` / `@heap@@` BEFORE the type suffix.
The two historical FALSE-POSITIVES this guards against:
  1. The undecorated deny scan is a PREFIX match (startswith), not a substring —
     `cpu_features_query` must NOT leak just because it contains `u_`.
  2. A C++ mangled symbol that resolves to `@v8@@` but carries `@absl@@` as a
     TEMPLATE PARAMETER must NOT leak — _is_v8_cxx() looks for V8's namespace
     anywhere in the name, so `@v8@@` presence vouches for it.
Real leaks: a `?...@absl@@` C++ export (no `@v8@@`), undecorated `u_errorName`,
`inflate`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coff import classify_exports, _is_v8_cxx  # noqa: E402

# Undecorated (extern "C") deny prefixes the audit derives from policy.json
# (the non-_ZN entries of audit.must_be_zero_global).
DENY_C = ["ubrk_", "ucnv_", "uloc_", "u_", "inflate", "deflate", "absl"]

# Clean PE export table: V8's own public surface only.
CLEAN = [
    "?Initialize@V8@v8@@SA_NXZ",                       # v8::V8::Initialize  -> @v8@@
    "?New@Isolate@v8@@SAPEAV12@AEBUCreateParams@2@@Z",  # v8::Isolate::New   -> @v8@@
    "?MakeGarbageCollected@cppgc@@...@Z",               # cppgc::             -> @cppgc@@
    "?createV8Inspector@V8Inspector@v8_inspector@@...", # v8_inspector::      -> @v8_inspector@@
    "?Stack@base@heap@@...",                            # heap::base::        -> @heap@@
    "V8_Initialize",                                    # extern "C" V8 entry — allowed
    "cpu_features_query",                               # HISTORICAL FP: contains "u_"
]

# C++ v8 method whose TEMPLATE PARAMETER is an absl type — resolves to @v8@@, so allowed.
PARAM_ABSL_CXX = [
    "?Run@CppGraphBuilder@internal@v8@@QEAAXV?$flat_hash_set@H@absl@@@Z",
]

# Genuine leaks: a C++ absl/icu export with NO @v8@@, and undecorated ICU/zlib C names.
GENUINE_LEAKS = [
    "?GetTID@base_internal@absl@@YAHXZ",   # C++ absl, no @v8@@ -> leak
    "u_errorName",                          # undecorated ICU C API -> leak (u_ prefix)
    "inflate",                              # undecorated zlib -> leak
]


def test_is_v8_cxx_helper():
    assert _is_v8_cxx("?Initialize@V8@v8@@SA_NXZ")
    assert _is_v8_cxx("?X@cppgc@@YAXXZ")
    assert _is_v8_cxx("?X@v8_inspector@@YAXXZ")
    assert _is_v8_cxx("?X@base@heap@@YAXXZ")
    # absl-only mangling is NOT v8
    assert not _is_v8_cxx("?GetTID@base_internal@absl@@YAHXZ")


def test_clean_image_no_leaks():
    leaks, v8_count = classify_exports(CLEAN, DENY_C)
    assert leaks == []
    assert v8_count == 2   # two exports contain @v8@@ (Initialize + New)


def test_cpu_underscore_is_not_a_leak():
    """HISTORICAL FP #1: `cpu_features_query` contains `u_` but is not an ICU symbol.
    Deny is a prefix match, so it stays allowed."""
    leaks, v8_count = classify_exports(["cpu_features_query"], DENY_C)
    assert leaks == []


def test_v8_cxx_with_absl_template_param_is_not_a_leak():
    """HISTORICAL FP #2: @v8@@ method with @absl@@ template param must be allowed."""
    leaks, v8_count = classify_exports(PARAM_ABSL_CXX, DENY_C)
    assert leaks == []
    assert v8_count == 1


def test_genuine_leaks_flagged():
    names = CLEAN + GENUINE_LEAKS
    leaks, v8_count = classify_exports(names, DENY_C)
    assert set(leaks) == set(GENUINE_LEAKS)
    assert v8_count == 2   # unaffected by the leaks


def test_cxx_absl_without_v8_is_a_leak():
    leaks, v8_count = classify_exports(["?GetTID@base_internal@absl@@YAHXZ"], DENY_C)
    assert leaks == ["?GetTID@base_internal@absl@@YAHXZ"]


def test_undecorated_icu_zlib_leak():
    leaks, v8_count = classify_exports(["u_errorName", "inflate", "ubrk_open"], DENY_C)
    assert set(leaks) == {"u_errorName", "inflate", "ubrk_open"}


# ---- standalone runner (no pytest) ----------------------------------------
def _run_standalone():
    import types
    g = dict(globals())
    tests = sorted(n for n, f in g.items()
                   if n.startswith("test_") and isinstance(f, types.FunctionType))
    fails = 0
    print("seal/test_coff_audit.py")
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
