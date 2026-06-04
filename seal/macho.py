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


def audit(lib, policy_path):
    pol = json.loads(Path(policy_path).read_text())
    deny = pol["audit"]["must_be_zero_global"]
    must = pol["audit"]["must_be_present"]
    # nm -gU: only globally-visible (exported) defined symbols in the dylib.
    out = subprocess.run(["nm", "-gU", str(lib)], capture_output=True, text=True).stdout
    syms = out.splitlines()
    leaks = [d for d in deny if any(d in s for s in syms)]
    if leaks:
        print(f"[seal/macho] AUDIT FAIL — exported denied symbols: {leaks}", file=sys.stderr)
        # show a few examples
        for d in leaks:
            ex = [s for s in syms if d in s][:3]
            print("   e.g. " + " | ".join(ex), file=sys.stderr)
        raise SystemExit(1)
    v8_count = sum(1 for s in syms if "v8" in s)
    if v8_count == 0:
        print("[seal/macho] AUDIT FAIL — no v8 symbols exported", file=sys.stderr)
        raise SystemExit(1)
    print(f"[seal/macho] AUDIT OK — 0 denied exports; {v8_count} v8 symbols exported")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
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
