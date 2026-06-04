#!/usr/bin/env python3
"""
seal/elf.py — Linux (ELF) symbol sealing + audit.

FLAGSHIP (shared .so): build with a version script (--version-script) that puts only
v8::/cppgc:: in the global { } section and everything else in local: *; so ICU/zlib
are not exported. -fvisibility=hidden alone is insufficient for static archives, but
for a shared lib the version script is the clean boundary.

STATIC "lite" fallback: ld -r fold the closure into one .o, then
objcopy --keep-global-symbols=public.txt (keeps listed names global, localizes the
rest — the INVERSE of --localize-symbols; the naive per-object localize is wrong and
breaks V8's cross-object refs — Codex pass 1). Mind COMDAT/weak sections.

Audit: readelf -sW — deny prefixes must have 0 GLOBAL/exported symbols; keep prefixes
present + complete.

STATUS: skeleton — implement in Phase 1.
"""
import sys


def write_version_script(public_symbols, out_path):
    raise NotImplementedError("Phase 1: emit { global: v8::*; cppgc::*; local: *; }")


def audit(so_path, policy):
    raise NotImplementedError("Phase 1: readelf -sW audit against seal/policy.json")


if __name__ == "__main__":
    sys.stderr.write("seal/elf.py is a Phase-1 skeleton.\n")
    sys.exit(2)
