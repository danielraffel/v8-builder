#!/usr/bin/env python3
"""Unit tests for seal/elf.py's export classifier (ELF / Linux + Android).

Runs two ways, no third-party deps required:
    python3 -m pytest seal/test_elf_audit.py     # if pytest is installed
    python3 seal/test_elf_audit.py               # standalone fallback

ELF mangled names have NO leading underscore (unlike Mach-O): v8:: -> _ZN2v8,
cppgc:: -> _ZN6cppgc. The allow-list is the seal gate; the deny-list is a
defense-in-depth backstop. The regression this guards: a v8::internal:: method
whose *parameter* mangling embeds `absl`/`icu`/`u_` must stay ALLOWED (it starts
with a v8 prefix), while a standalone `_ZN4absl...` / `_ZN6icu_...` / undecorated
`u_errorName` / `inflate` must be flagged a leak.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from elf import classify_exports  # noqa: E402

# The real deny-list the audit consumes (policy.json -> audit.must_be_zero_global).
DENY = ["ubrk_", "ucnv_", "uloc_", "u_", "inflate", "deflate",
        "_ZN4absl", "absl", "_ZN6icu_", "_ZN6icu"]

# Clean image: only v8::/cppgc:: ABI exports (no leading underscore on ELF).
CLEAN = [
    "_ZN2v87Isolate3NewERKNS_15ResourceLimitsE",   # v8::
    "_ZNK2v85Value7IsArrayEv",                      # const v8::
    "_ZTVN2v86String5Utf8ValueE",                   # vtable v8::
    "_ZN6cppgc4HeapC1Ev",                           # cppgc:: (not v8-counted)
]

# v8::internal:: methods carrying absl / icu / u_ in PARAMETER mangling — must be ALLOWED.
PARAM_LEAKY_LOOKING = [
    "_ZN2v88internal15CppGraphBuilder3RunEON4absl13flat_hash_setIiEE",  # absl param
    "_ZN2v88internal7Isolate16set_icu_collatorEPNS0_12icu_collatorE",   # icu + u_ in body
]

# Genuine leaks the allow-list must catch, including ones the OLD deny-list missed
# (icu's C++ namespace `_ZN6icu_` historically leaked ~7742 symbols).
GENUINE_LEAKS = [
    "_ZN4absl13base_internal6GetTIDEv",       # standalone Abseil
    "_ZN6icu_7717UnicodeStringeEv",           # standalone ICU C++ (allow-list catches it)
    "u_errorName",                            # undecorated ICU C API
    "inflate",                                # undecorated zlib
]


def test_clean_image_no_leaks():
    leaks, denied, v8_count = classify_exports(CLEAN, DENY)
    assert leaks == []
    assert denied == []
    assert v8_count == 3


def test_v8_method_with_absl_icu_param_is_not_a_leak():
    leaks, denied, v8_count = classify_exports(PARAM_LEAKY_LOOKING, DENY)
    assert leaks == []
    # deny-list only scans EXPORTED symbols by prefix; these start with _ZN2v8 so no
    # deny prefix matches them (the substring trap is avoided by using startswith).
    assert denied == []
    assert v8_count == 2


def test_genuine_leaks_flagged_by_allow_list():
    names = CLEAN + GENUINE_LEAKS
    leaks, denied, v8_count = classify_exports(names, DENY)
    assert set(leaks) == set(GENUINE_LEAKS)   # all four caught by the allow-list
    assert v8_count == 3                       # unaffected


def test_deny_list_backstop_fires_on_known_internals():
    names = CLEAN + ["_ZN4absl4FooEv", "inflate", "u_errorName", "_ZN6icu_7Foo"]
    leaks, denied, v8_count = classify_exports(names, DENY)
    # deny prefixes that matched some export
    assert "_ZN4absl" in denied
    assert "inflate" in denied
    assert "u_" in denied
    assert "_ZN6icu_" in denied


def test_only_cppgc_still_zero_v8_count():
    leaks, denied, v8_count = classify_exports(["_ZN6cppgc4HeapC1Ev"], DENY)
    assert leaks == []
    assert v8_count == 0   # cppgc is allowed but not v8 — audit() fails this case


# ---- standalone runner (no pytest) ----------------------------------------
def _run_standalone():
    import types
    g = dict(globals())
    tests = sorted(n for n, f in g.items()
                   if n.startswith("test_") and isinstance(f, types.FunctionType))
    fails = 0
    print("seal/test_elf_audit.py")
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
