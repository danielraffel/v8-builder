#!/usr/bin/env python3
"""
seal/elf.py — Linux (ELF) seal + audit.

Same goal as the macOS lane: produce a SHARED libv8.so whose dynamic symbol table
exports ONLY the v8::/cppgc:: embedder ABI, keeping Abseil/ICU/zlib INTERNAL so they
cannot interpose Skia/Dawn's copies (the Abseil-ODR collision proven on macOS in
P0.3a applies identically on ELF).

On ELF the clean mechanism is a linker VERSION SCRIPT: `{ global: <v8 patterns>;
local: *; }`. As on macOS, V8 15.1's monolith is NOT self-contained (Rust Temporal),
so the production path mirrors P1c: an in-tree gn `v8_shared_library` target with
`ldflags = -Wl,--version-script=<file>` lets gn compute the full Rust+system closure.
This module emits the version script and runs the audit.

STATUS: implemented; **validates on a Linux runner (Tart/GitHub), not on this macOS
host** — do not report a Linux pass until CI runs it.

Usage:
  seal/elf.py version-script --out v8_exports.map
  seal/elf.py audit --lib libv8.so --policy seal/policy.json
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# ELF mangled names have NO leading underscore (unlike Mach-O): v8:: -> _ZN2v8...
#
# Use ONLY anchored mangled-prefix patterns (mirrors the proven macOS
# exported_symbols_list). Do NOT use an `extern "C++" { "v8::*" }` block: lld matches
# those globs against the DEMANGLED name, so `v8::*` also matches absl/icu *template
# instantiations parameterized on v8 types* (e.g.
# `absl::functional_internal::InvokeObject<v8::internal::maglev::...>`,
# `icu_77::...<v8::...>`) — their demangled names contain `v8::`, so the glob promoted
# ~1972 absl + ~7742 icu C++ symbols to GLOBAL, breaking the seal (the ODR collision we
# exist to prevent). The mangled prefixes below are anchored at the start of the *mangled*
# name (`_ZN2v8` = `::v8`, `_ZN6cppgc` = `::cppgc`), which absl (`_ZN4absl`) and icu
# (`_ZN6icu_`) can never match. Verified on a real x86_64 link: 0 absl/icu/zlib exported.
VERSION_SCRIPT = """\
{
  global:
    _ZN2v8*; _ZNK2v8*; _ZTVN2v8*; _ZTIN2v8*; _ZTSN2v8*;
    _ZN6cppgc*; _ZNK6cppgc*; _ZTVN6cppgc*; _ZTIN6cppgc*; _ZTSN6cppgc*;
  local:
    *;
};
"""

# An exported symbol is legitimate ONLY if its mangled name starts with one of these
# (the v8::/cppgc:: embedder ABI). Anything else in the dynamic export table is a seal
# leak. This is the allow-list the audit enforces — strictly stronger than a deny-list,
# which is blind to internals it didn't think to name (it missed icu's C++ namespace).
EXPORT_ALLOW_MANGLED = (
    "_ZN2v8", "_ZNK2v8", "_ZTVN2v8", "_ZTIN2v8", "_ZTSN2v8",
    "_ZN6cppgc", "_ZNK6cppgc", "_ZTVN6cppgc", "_ZTIN6cppgc", "_ZTSN6cppgc",
)


def write_version_script(out):
    Path(out).write_text(VERSION_SCRIPT)
    print(f"[seal/elf] wrote version script: {out}")


def _exported_symbols(lib):
    """Return the names of TRULY EXPORTED symbols (the ones that can ODR-collide):
    GLOBAL or WEAK binding, DEFAULT visibility, defined (Ndx != UND).

    readelf -sW --dyn-syms columns: Num: Value Size Type Bind Vis Ndx Name
    """
    out = subprocess.run(["readelf", "-sW", "--dyn-syms", str(lib)],
                         capture_output=True, text=True).stdout
    names = []
    for line in out.splitlines():
        f = line.split()
        # need at least 8 fields and a numeric "Num:" first column
        if len(f) < 8 or not f[0].rstrip(":").isdigit():
            continue
        bind, vis, ndx, name = f[4], f[5], f[6], f[7]
        if bind in ("GLOBAL", "WEAK") and vis == "DEFAULT" and ndx != "UND":
            names.append(name)
    return names


def audit(lib, policy_path):
    pol = json.loads(Path(policy_path).read_text())
    deny = pol["audit"]["must_be_zero_global"]
    exported = _exported_symbols(lib)
    if not exported:
        print(f"[seal/elf] AUDIT FAIL — no exported symbols in {lib}", file=sys.stderr)
        raise SystemExit(1)

    # ALLOW-LIST (primary, unfakeable): every exported symbol MUST be on the
    # v8::/cppgc:: embedder ABI. Anything else is a seal leak — including internals no
    # deny-list named (the old deny-list missed icu's C++ namespace `_ZN6icu_` and let
    # ~7742 icu + ~1972 absl template instantiations through; an allow-list cannot).
    leaks = [s for s in exported if not s.startswith(EXPORT_ALLOW_MANGLED)]
    if leaks:
        from collections import Counter
        # bucket by the leading mangled namespace token for a readable report
        buckets = Counter()
        for s in leaks:
            m = re.match(r"_ZN?K?\d+([A-Za-z_]+)", s)
            buckets[m.group(1) if m else s[:12]] += 1
        print(f"[seal/elf] AUDIT FAIL — {len(leaks)} non-v8/cppgc symbols exported "
              f"(seal leak). Top namespaces: {dict(buckets.most_common(6))}", file=sys.stderr)
        for s in leaks[:5]:
            print(f"   e.g. {s}", file=sys.stderr)
        raise SystemExit(1)

    # DENY-LIST (defense-in-depth + clearer message if a known internal ever appears).
    denied = sorted({d for d in deny for s in exported if s.startswith(d)})
    if denied:
        print(f"[seal/elf] AUDIT FAIL — exported denied internals: {denied}", file=sys.stderr)
        raise SystemExit(1)

    v8n = sum(1 for s in exported if s.startswith(("_ZN2v8", "_ZNK2v8", "_ZTVN2v8",
                                                   "_ZTIN2v8", "_ZTSN2v8")))
    if v8n == 0:
        print("[seal/elf] AUDIT FAIL — no v8 symbols exported", file=sys.stderr)
        raise SystemExit(1)
    print(f"[seal/elf] AUDIT OK — {len(exported)} exports, all v8::/cppgc:: "
          f"({v8n} v8); 0 absl/icu/zlib leaks")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("version-script"); v.add_argument("--out", required=True)
    a = sub.add_parser("audit"); a.add_argument("--lib", required=True); a.add_argument("--policy", required=True)
    args = p.parse_args()
    if args.cmd == "version-script":
        write_version_script(args.out)
    else:
        audit(args.lib, args.policy)


if __name__ == "__main__":
    main()
