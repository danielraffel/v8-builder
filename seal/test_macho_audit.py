#!/usr/bin/env python3
"""Unit tests for seal/macho.py's export classifier.

Run: python3 seal/test_macho_audit.py   (no third-party deps; exits non-zero on failure)

The load-bearing case is the deny-list false-positive regression: a legitimately
sealed `v8::internal::` export whose *parameter* mangling mentions `absl`/`u_`
must NOT be flagged as a seal leak. This bit the iOS sealed-framework gate — the
real 23003-symbol sealed iOS dylib exports four `v8::internal::CppGraphBuilder` /
`EphemeronRememberedSet` methods that take `absl::flat_hash_set<...>` parameters,
and a naive `if "absl" in symbol` deny scan failed the audit on them even though
the export table contains zero standalone Abseil/ICU symbols.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macho import classify_exports  # noqa: E402

DENY = ["absl", "icu", "u_", "zlib", "_ZN6icu_", "deflate", "inflate"]

_fails = 0


def check(name, cond):
    global _fails
    status = "ok" if cond else "FAIL"
    if not cond:
        _fails += 1
    print(f"  [{status}] {name}")


def main():
    print("seal/test_macho_audit.py")

    # 1) Clean sealed image: only v8::/cppgc:: exports → no leaks, no denials.
    clean = [
        "__ZN2v87Isolate3NewERKNS_15ResourceLimitsE",
        "__ZNK2v85Value7IsArrayEv",
        "__ZTVN2v86String5Utf8ValueE",
        "__ZN6cppgc4HeapC1Ev",
    ]
    leaks, denied, v8_count = classify_exports(clean, DENY)
    check("clean image: zero leaks", leaks == [])
    check("clean image: zero denials", denied == [])
    check("clean image: v8_count == 3", v8_count == 3)

    # 2) REGRESSION: a real sealed v8::internal:: export whose parameter mangling
    #    carries `absl`/`u_` substrings must be treated as ALLOWED (no leak, no deny).
    #    These four are taken verbatim from the built iOS sealed dylib.
    param_absl = [
        "__ZN2v88internal15CppGraphBuilder3RunEPNS0_7CppHeapEPNS0_21Heap"
        "SnapshotGeneratorEON4absl13flat_hash_setINS0_6TaggedINS0_5Union"
        "IJNS0_8JSObjectENS0_21CppHeapExternalObjectEEEEEENS0_6Object6Has"
        "herENSE_12KeyEqualSafeENSt3__19allocatorISD_EEEE",
        "__ZN2v88internal22EphemeronRememberedSet24RecordEphemeronKeyWrit"
        "esENS0_6TaggedINS0_18EphemeronHashTableEEEN4absl13flat_hash_setI"
        "iNS5_13hash_internal4HashIiEENSt3__18equal_toIiEENSA_9allocatorI"
        "iEEEE",
    ]
    leaks, denied, v8_count = classify_exports(param_absl, DENY)
    check("v8 method with absl-typed param: NOT a leak", leaks == [])
    check("v8 method with absl-typed param: NOT denied", denied == [])
    check("v8 method with absl-typed param: counted as v8", v8_count == 2)

    # 3) A genuine seal leak (standalone Abseil/ICU symbol, no v8 prefix) MUST be
    #    flagged by BOTH the allow-list (leak) and the deny-list.
    leaky = clean + [
        "__ZN4absl13base_internal6GetTIDEv",     # standalone Abseil
        "_u_errorName",                          # standalone ICU C API
        "__ZN6icu_7677UnicodeString6lengthEv",   # standalone ICU C++
    ]
    leaks, denied, v8_count = classify_exports(leaky, DENY)
    check("genuine leak: 3 allow-list leaks", len(leaks) == 3)
    check("genuine leak: deny-list fires", "absl" in denied and "icu" in denied)
    check("genuine leak: v8_count unaffected", v8_count == 3)

    # 4) Empty/uninteresting symbols don't crash and report no v8.
    leaks, denied, v8_count = classify_exports(["_some_c_symbol"], DENY)
    check("non-v8 C symbol: flagged as leak", leaks == ["_some_c_symbol"])
    check("non-v8 C symbol: v8_count == 0", v8_count == 0)

    if _fails:
        print(f"\n{_fails} check(s) FAILED")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
