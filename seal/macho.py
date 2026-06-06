#!/usr/bin/env python3
"""
seal/macho.py — macOS seal: wrap a static v8_monolith.a into a SHARED libv8.dylib
whose export table contains ONLY the v8::/cppgc:: ABI. Everything else (Abseil,
ICU, zlib, ...) is pulled in via -force_load but kept OUT of the dynamic export
table, so it cannot interpose Skia/Dawn's copies (Phase-0 finding: duplicate
Abseil exports between V8 and Dawn abort the process).

This is the provider-side equivalent of the export-hide we verified works in P0.3a.

Usage:
  seal/macho.py seal  --monolith <libv8_monolith.a> --out <libv8.dylib> --policy <policy.json>
  seal/macho.py audit --lib <libv8.dylib> --policy <policy.json>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# Mach-O exported-symbols-list patterns for the V8 embedder ABI. Mangled C++ names
# carry a leading underscore on Mach-O (e.g. v8::... -> __ZN2v8...). Covers methods,
# const methods, vtables (ZTV), typeinfo (ZTI) and typeinfo-names (ZTS).
KEEP_PATTERNS = [
    "__ZN2v8*", "__ZNK2v8*", "__ZTVN2v8*", "__ZTIN2v8*", "__ZTSN2v8*",
    "__ZN6cppgc*", "__ZNK6cppgc*", "__ZTVN6cppgc*", "__ZTIN6cppgc*", "__ZTSN6cppgc*",
]


def run(cmd):
    print("[seal/macho] $ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True)


def seal(monolith, out, policy_path):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    exports = out.parent / "v8_exports.txt"
    exports.write_text("\n".join(KEEP_PATTERNS) + "\n")

    # Whole-archive the monolith into a dylib, exporting only the V8 ABI patterns.
    # System libs V8 needs on macOS; extend if the link reports undefined symbols.
    cmd = [
        "clang++", "-dynamiclib", "-std=c++20", "-stdlib=libc++",
        "-o", str(out),
        "-Wl,-force_load," + str(monolith),
        "-Wl,-exported_symbols_list," + str(exports),
        "-Wl,-install_name,@rpath/libv8.dylib",
        "-framework", "CoreFoundation",
        "-framework", "Foundation",
        "-lc++", "-lc++abi",
    ]
    run(cmd)
    audit(out, policy_path)
    print(f"[seal/macho] sealed dylib: {out}")


# Allow-list = exactly the surface the -exported_symbols_list pins (mangled, leading _).
# We AUDIT with an allow-list, not a substring deny-list, for the same reason elf.py and
# coff.py do: a deny substring-check FALSE-POSITIVES badly. Many legitimate v8::internal
# symbols contain a deny token in their mangled name while themselves being v8 symbols —
# e.g. `...set_icu_collator...` / `...u_string...` contain "u_", and a method taking an
# `absl::flat_hash_set` TEMPLATE PARAMETER (`v8::internal::CppGraphBuilder::Run`) contains
# "absl" — yet all start with __ZN2v8 and are part of the embedder ABI. The allow-list
# still catches a REAL leak (`__ZN4absl...`, `_u_errorName`, ...) because it doesn't start
# with a v8/cppgc prefix.
V8_ALLOW_PREFIXES = tuple(p.rstrip("*") for p in KEEP_PATTERNS)


def _exported_names(lib):
    # nm -gU: globally-visible (exported) DEFINED symbols. Format: "<addr> <type> <name>".
    out = subprocess.run(["nm", "-gU", str(lib)], capture_output=True, text=True).stdout
    return [parts[-1] for parts in (ln.split() for ln in out.splitlines()) if parts]


def audit(lib, policy_path):
    json.loads(Path(policy_path).read_text())  # validate policy is readable/well-formed
    names = _exported_names(lib)
    if not names:
        print(f"[seal/macho] AUDIT FAIL — no exports found in {lib}", file=sys.stderr)
        raise SystemExit(1)
    leaks = [n for n in names if not any(n.startswith(p) for p in V8_ALLOW_PREFIXES)]
    if leaks:
        print(f"[seal/macho] AUDIT FAIL — {len(leaks)} non-v8/cppgc exports (seal leak: "
              f"ICU/zlib/Abseil/std/etc.)", file=sys.stderr)
        for n in leaks[:5]:
            print(f"   e.g. {n}", file=sys.stderr)
        raise SystemExit(1)
    v8_count = sum(1 for n in names if n.startswith("__ZN2v8"))
    if v8_count == 0:
        print("[seal/macho] AUDIT FAIL — no v8 symbols exported", file=sys.stderr)
        raise SystemExit(1)
    print(f"[seal/macho] AUDIT OK — {len(names)} exports, all v8::/cppgc::; "
          f"0 ICU/zlib/Abseil leaks")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.required = True  # py3.6 (Rocky 8) compat: required= kwarg is 3.7+
    s = sub.add_parser("seal"); s.add_argument("--monolith", required=True)
    s.add_argument("--out", required=True); s.add_argument("--policy", required=True)
    a = sub.add_parser("audit"); a.add_argument("--lib", required=True)
    a.add_argument("--policy", required=True)
    args = p.parse_args()
    if args.cmd == "seal":
        seal(args.monolith, args.out, args.policy)
    else:
        audit(args.lib, args.policy)


if __name__ == "__main__":
    main()
