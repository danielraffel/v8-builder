#!/usr/bin/env python3
"""
seal/macho.py — macOS (Mach-O) symbol sealing + audit.

FLAGSHIP (shared .dylib): pass an -exported_symbols_list at link time so only the
v8::/cppgc:: ABI is exported; ICU/zlib stay internal (two-level namespace). This is
the same property `libnode` has (nm -gU shows 0 flat ICU symbols).

STATIC "lite" fallback: ld -r -exported_symbols_list single-object prelink.

The exported list MUST be generated (full mangled v8::/v8::platform/cppgc surface),
NOT hand-written as "v8::*" (proposal §6, Codex pass 1).

STATUS: skeleton — generate_export_list() / audit() to be implemented in Phase 2.
"""
import sys


def generate_export_list(built_lib_or_objs):
    raise NotImplementedError(
        "Phase 2: scrape mangled v8::/v8::platform/cppgc symbols, write exported_symbols.txt")


def audit(dylib_path, policy):
    # Phase 2: nm -gU <dylib>; assert deny prefixes have 0 global symbols and
    # keep prefixes are present + complete vs policy['audit'].
    raise NotImplementedError("Phase 2: nm -gU audit against seal/policy.json")


if __name__ == "__main__":
    sys.stderr.write("seal/macho.py is a Phase-2 skeleton.\n")
    sys.exit(2)
