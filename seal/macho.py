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


# An exported Mach-O symbol is legitimate ONLY if its (leading-underscore) mangled
# name starts with one of these — the v8::/cppgc:: embedder ABI. This is the ALLOW-LIST
# the audit enforces, mirroring elf.py: strictly stronger than a deny-list, which is
# blind to internals it didn't think to name (the ELF deny-list once missed icu's C++
# namespace `_ZN6icu_` and leaked ~7742 icu symbols; an allow-list cannot). Mach-O adds
# a leading `_`, so v8:: → `__ZN2v8`, cppgc:: → `__ZN6cppgc`.
EXPORT_ALLOW_MANGLED = (
    "__ZN2v8", "__ZNK2v8", "__ZTVN2v8", "__ZTIN2v8", "__ZTSN2v8",
    "__ZN6cppgc", "__ZNK6cppgc", "__ZTVN6cppgc", "__ZTIN6cppgc", "__ZTSN6cppgc",
)


def _exported_symbols(lib):
    """Names of globally-visible defined (exported) symbols in the Mach-O image.
    nm -gU: -g = global/external only, -U = defined only (omit undefined). The name is
    the last whitespace field of each `<addr> <type> <name>` line."""
    out = subprocess.run(["nm", "-gU", str(lib)], capture_output=True, text=True).stdout
    names = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        names.append(parts[-1])
    return names


def audit(lib, policy_path):
    pol = json.loads(Path(policy_path).read_text())
    deny = pol["audit"]["must_be_zero_global"]
    exported = _exported_symbols(lib)
    if not exported:
        print(f"[seal/macho] AUDIT FAIL — no exported symbols in {lib}", file=sys.stderr)
        raise SystemExit(1)

    # ALLOW-LIST (primary, unfakeable): every exported symbol MUST be on the
    # v8::/cppgc:: embedder ABI. Anything else is a seal leak. nm may also list a few
    # linker-synthesized symbols on Mach-O; none of those start with `__ZN2v8`/`__ZN6cppgc`,
    # so the allow-list correctly rejects an unsealed image.
    leaks = [s for s in exported if not s.startswith(EXPORT_ALLOW_MANGLED)]
    if leaks:
        from collections import Counter
        import re as _re
        buckets = Counter()
        for s in leaks:
            m = _re.match(r"__ZN?K?\d+([A-Za-z_]+)", s)
            buckets[m.group(1) if m else s[:12]] += 1
        print(f"[seal/macho] AUDIT FAIL — {len(leaks)} non-v8/cppgc symbols exported "
              f"(seal leak). Top namespaces: {dict(buckets.most_common(6))}",
              file=sys.stderr)
        for s in leaks[:5]:
            print(f"   e.g. {s}", file=sys.stderr)
        raise SystemExit(1)

    # DENY-LIST (defense-in-depth + clearer message if a known internal ever appears).
    denied = sorted({d for d in deny for s in exported if d in s})
    if denied:
        print(f"[seal/macho] AUDIT FAIL — exported denied internals: {denied}",
              file=sys.stderr)
        raise SystemExit(1)

    v8_count = sum(1 for s in exported
                   if s.startswith(("__ZN2v8", "__ZNK2v8", "__ZTVN2v8",
                                    "__ZTIN2v8", "__ZTSN2v8")))
    if v8_count == 0:
        print("[seal/macho] AUDIT FAIL — no v8 symbols exported", file=sys.stderr)
        raise SystemExit(1)
    print(f"[seal/macho] AUDIT OK — {len(exported)} exports, all v8::/cppgc:: "
          f"({v8_count} v8); 0 absl/icu/zlib leaks")


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
