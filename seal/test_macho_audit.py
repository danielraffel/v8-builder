#!/usr/bin/env python3
"""Unit tests for seal/macho.py's export classifier (Mach-O / macOS + iOS).

Runs two ways, no third-party deps required:
    python3 -m pytest seal/test_macho_audit.py     # if pytest is installed
    python3 seal/test_macho_audit.py               # standalone fallback

The load-bearing case is the deny-list / substring FALSE-POSITIVE regression: a
legitimately sealed `v8::internal::` export whose *parameter* mangling mentions
`absl`/`u_` must NOT be flagged as a seal leak. This bit the iOS sealed-framework
gate — the real 23003-symbol sealed iOS dylib exports `v8::internal::CppGraphBuilder`
/ `EphemeronRememberedSet` methods that take `absl::flat_hash_set<...>` parameters,
and a naive `if "absl" in symbol` deny scan failed the audit on them even though the
export table contains zero standalone Abseil/ICU symbols.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macho import classify_exports  # noqa: E402

# Policy deny family names (substrings) — what a naive deny scan would look for.
DENY = ["absl", "icu", "u_", "zlib", "_ZN6icu_", "deflate", "inflate"]

# A clean sealed image: only v8::/cppgc:: ABI exports. Mach-O adds a leading '_',
# so v8:: -> __ZN2v8, cppgc:: -> __ZN6cppgc. Three are v8::, one is cppgc::.
CLEAN = [
    "__ZN2v87Isolate3NewERKNS_15ResourceLimitsE",   # __ZN2v8
    "__ZNK2v85Value7IsArrayEv",                      # __ZNK2v8
    "__ZTVN2v86String5Utf8ValueE",                   # __ZTVN2v8
    "__ZN6cppgc4HeapC1Ev",                           # cppgc:: (not counted in v8_count)
]

# REGRESSION fixtures: real sealed v8::internal:: exports whose parameter mangling
# carries `absl` / `u_` substrings. Taken verbatim from the built iOS sealed dylib.
PARAM_ABSL = [
    "__ZN2v88internal15CppGraphBuilder3RunEPNS0_7CppHeapEPNS0_21Heap"
    "SnapshotGeneratorEON4absl13flat_hash_setINS0_6TaggedINS0_5Union"
    "IJNS0_8JSObjectENS0_21CppHeapExternalObjectEEEEEENS0_6Object6Has"
    "herENSE_12KeyEqualSafeENSt3__19allocatorISD_EEEE",
    "__ZN2v88internal22EphemeronRememberedSet24RecordEphemeronKeyWrit"
    "esENS0_6TaggedINS0_18EphemeronHashTableEEEN4absl13flat_hash_setI"
    "iNS5_13hash_internal4HashIiEENSt3__18equal_toIiEENSA_9allocatorI"
    "iEEEE",
]

# A v8 method whose mangling embeds set_icu_collator / u_string — contains "u_" but is
# a real v8 ABI symbol (starts with __ZN2v8).
PARAM_U = [
    "__ZN2v88internal7Isolate16set_icu_collatorEPNS0_12icu_collatorE",  # contains "u_"
    "__ZN2v88internal8u_stringEv",  # synthetic: contains "u_string", still __ZN2v8
]

# Genuine leaks: standalone Abseil / ICU symbols with NO v8/cppgc prefix.
GENUINE_LEAKS = [
    "__ZN4absl13base_internal6GetTIDEv",     # standalone Abseil C++
    "_u_errorName",                          # standalone ICU C API
    "__ZN6icu_7677UnicodeString6lengthEv",   # standalone ICU C++
]


def test_clean_image_no_leaks():
    leaks, denied, v8_count = classify_exports(CLEAN, DENY)
    assert leaks == []
    assert denied == []
    assert v8_count == 3   # three v8:: exports; the cppgc:: one is not v8-counted


def test_v8_method_with_absl_param_is_not_a_leak():
    """The exact iOS regression: absl as a TEMPLATE PARAM must not trip the audit."""
    leaks, denied, v8_count = classify_exports(PARAM_ABSL, DENY)
    assert leaks == []
    assert denied == []
    assert v8_count == 2


def test_v8_method_with_u_substring_is_not_a_leak():
    leaks, denied, v8_count = classify_exports(PARAM_U, DENY)
    assert leaks == []
    assert denied == []
    assert v8_count == 2


def test_genuine_standalone_leaks_are_flagged():
    names = CLEAN + GENUINE_LEAKS
    leaks, denied, v8_count = classify_exports(names, DENY)
    assert len(leaks) == 3                       # exactly the standalone non-v8 symbols
    assert set(leaks) == set(GENUINE_LEAKS)
    assert "absl" in denied and "icu" in denied  # deny-list backstop fires on leaks
    assert v8_count == 3                          # v8 count unaffected by the leaks


def test_non_v8_c_symbol_is_a_leak():
    leaks, denied, v8_count = classify_exports(["_some_c_symbol"], DENY)
    assert leaks == ["_some_c_symbol"]
    assert v8_count == 0


def test_mixed_clean_and_param_and_leak():
    names = CLEAN + PARAM_ABSL + GENUINE_LEAKS
    leaks, denied, v8_count = classify_exports(names, DENY)
    # only the 3 standalone leaks; absl-param v8 methods stay allowed
    assert set(leaks) == set(GENUINE_LEAKS)
    assert v8_count == 5   # 3 from CLEAN + 2 from PARAM_ABSL


# ---- standalone runner (no pytest) ----------------------------------------
def _run_standalone():
    import types
    g = dict(globals())
    tests = sorted(n for n, f in g.items()
                   if n.startswith("test_") and isinstance(f, types.FunctionType))
    fails = 0
    print("seal/test_macho_audit.py")
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
