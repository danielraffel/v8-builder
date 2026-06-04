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
import subprocess
import sys
from pathlib import Path

# ELF mangled names have NO leading underscore (unlike Mach-O): v8:: -> _ZN2v8...
VERSION_SCRIPT = """\
{
  global:
    extern "C++" {
      "v8::*";
      "cppgc::*";
      v8::*;
      cppgc::*;
    };
    _ZN2v8*; _ZNK2v8*; _ZTVN2v8*; _ZTIN2v8*; _ZTSN2v8*;
    _ZN6cppgc*; _ZNK6cppgc*; _ZTVN6cppgc*; _ZTIN6cppgc*; _ZTSN6cppgc*;
  local:
    *;
};
"""


def write_version_script(out):
    Path(out).write_text(VERSION_SCRIPT)
    print(f"[seal/elf] wrote version script: {out}")


def audit(lib, policy_path):
    pol = json.loads(Path(policy_path).read_text())
    deny = pol["audit"]["must_be_zero_global"]
    # readelf --dyn-syms: only the dynamic (exported) symbol table.
    out = subprocess.run(["readelf", "-sW", "--dyn-syms", str(lib)],
                         capture_output=True, text=True).stdout
    # exported = GLOBAL/WEAK + DEFINED (not UND)
    exported = [l for l in out.splitlines()
                if (" GLOBAL " in l or " WEAK " in l) and " UND " not in l]
    leaks = []
    for d in deny:
        # bare-internal leak: symbol token starts with the denied prefix
        if any(l.split()[-1].startswith(d) or l.split()[-1].startswith("_ZN4absl") and d == "absl"
               for l in exported if l.split()):
            leaks.append(d)
    if leaks:
        print(f"[seal/elf] AUDIT FAIL — exported denied internals: {leaks}", file=sys.stderr)
        raise SystemExit(1)
    v8n = sum(1 for l in exported if "2v8" in l)
    if v8n == 0:
        print("[seal/elf] AUDIT FAIL — no v8 symbols exported", file=sys.stderr)
        raise SystemExit(1)
    print(f"[seal/elf] AUDIT OK — 0 denied internal exports; {v8n} v8 dynsyms")


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
