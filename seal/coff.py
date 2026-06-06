#!/usr/bin/env python3
"""
seal/coff.py — Windows (PE/COFF) seal + audit.

Same goal as the macOS (Mach-O) and Linux (ELF) lanes: produce a SHARED v8.dll
whose EXPORT TABLE contains ONLY the v8::/cppgc:: embedder ABI, keeping
Abseil/ICU/zlib INTERNAL so they cannot interpose Skia/Dawn's copies (the
Abseil-ODR collision proven on macOS in P0.3a applies identically on PE).

WHY WINDOWS IS DIFFERENT (and mostly simpler):
A PE DLL exports a symbol ONLY if it is `__declspec(dllexport)` (V8's `V8_EXPORT`
macro, active when V8 is built shared) OR named in a `.def`/`/EXPORT:`. So the
ICU/zlib/Abseil objects inside libv8_monolith — which carry no dllexport — are
INTERNAL to v8.dll by construction. There is no whole-archive symbol-hiding step
like ELF/Mach-O need. The seal is therefore primarily a *configuration* (build the
v8:: surface with V8_EXPORT→dllexport via the in-tree `v8_sealed_shared` GN target)
plus this audit. A `.def` is available as a belt-and-suspenders / fallback.

STATUS: validates on a Windows runner (GitHub windows-2022 / Tart Windows VM), not
on a macOS host — do not report a Windows pass until that workflow runs it green.

Usage:
  seal/coff.py audit   --lib v8.dll --policy seal/policy.json
  seal/coff.py def-gen --monolith v8_monolith.lib --out v8_exports.def   [fallback]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _exports(lib):
    """Return the list of exported symbol names from a PE DLL.

    Prefer llvm-readobj (cross-platform, ships with the bundled clang toolchain so
    the audit can also run off-Windows); fall back to MSVC dumpbin on a real
    Windows runner.
    """
    lib = str(lib)
    if shutil.which("llvm-readobj"):
        out = subprocess.run(["llvm-readobj", "--coff-exports", lib],
                             capture_output=True, text=True).stdout
        # lines look like:  Name: ?Initialize@V8@v8@@SA_NXZ
        return [l.split("Name:", 1)[1].strip()
                for l in out.splitlines() if "Name:" in l]
    if shutil.which("dumpbin"):
        out = subprocess.run(["dumpbin", "/exports", lib],
                             capture_output=True, text=True).stdout
        names = []
        in_table = False
        for l in out.splitlines():
            s = l.strip()
            # the export table rows are: ordinal hint RVA name
            if s.startswith("ordinal") and "name" in s:
                in_table = True
                continue
            if in_table:
                parts = s.split()
                # a data row has >= 4 cols (ordinal hint RVA name); name is last
                if len(parts) >= 4 and parts[0].isdigit():
                    names.append(parts[-1])
        return names
    raise SystemExit("[seal/coff] need llvm-readobj or dumpbin to read PE exports")


# V8's INTENDED public export surface on PE. dllexport (V8_EXPORT) emits MORE than the
# Linux/mac version-script allow-list (v8::/cppgc:: only): V8 also marks `v8_inspector::`
# (the debugger protocol), `heap::base::` (conservative-stack lib), and a couple of
# extern "C" entry points (e.g. CrashForExceptionInNonABICompliantCodeRange on Windows).
# These are all V8's OWN symbols — ZERO collision risk with Skia/Dawn. So the seal goal
# (ICU/zlib/Abseil/protobuf NOT exported) is unaffected; we accept V8's full namespace
# surface and enforce the DENY list as the real seal gate. MSVC mangles the namespace
# chain right-to-left, so `@v8@@`/`@v8_inspector@@`/etc. appear before the type suffix.
V8_PUBLIC_NS = ("@v8@@", "@cppgc@@", "@v8_inspector@@", "@heap@@")


def _is_v8_cxx(name):
    return any(ns in name for ns in V8_PUBLIC_NS)


def audit(lib, policy_path):
    pol = json.loads(Path(policy_path).read_text())
    # Only the C-name prefixes matter for UNDECORATED PE exports (ICU's C API, zlib).
    deny_c = [d for d in pol["audit"]["must_be_zero_global"] if not d.startswith("_ZN")]
    exported = _exports(lib)
    if not exported:
        print(f"[seal/coff] AUDIT FAIL — no exports found in {lib}", file=sys.stderr)
        raise SystemExit(1)

    leaks = []
    for s in exported:
        if s.startswith("?"):
            # C++ mangled: must be on V8's public namespace surface. Catches absl
            # (?...@absl@@), icu (?...@icu_NN@@), std, protobuf — none of which are V8's.
            if not _is_v8_cxx(s):
                leaks.append(s)
        else:
            # Undecorated (extern "C"): a C ICU/zlib name (u_*, ubrk_, inflate, ...) is a
            # leak; anything else is one of V8's own C entry points — allowed.
            if any(s.startswith(d) for d in deny_c):
                leaks.append(s)

    if leaks:
        from collections import Counter
        buckets = Counter()
        for s in leaks:
            m = re.search(r"@([A-Za-z_][A-Za-z0-9_]*)@@", s) or re.match(r"([A-Za-z_]+)", s)
            buckets[m.group(1) if m else s[:16]] += 1
        print(f"[seal/coff] AUDIT FAIL — {len(leaks)} non-V8 exports (seal leak: "
              f"ICU/zlib/Abseil/protobuf/std). Top: {dict(buckets.most_common(6))}",
              file=sys.stderr)
        for s in leaks[:5]:
            print(f"   e.g. {s}", file=sys.stderr)
        raise SystemExit(1)

    v8n = sum(1 for s in exported if "@v8@@" in s)
    if v8n == 0:
        print(f"[seal/coff] AUDIT FAIL — no v8 symbols exported "
              f"(saw {len(exported)} exports)", file=sys.stderr)
        raise SystemExit(1)
    print(f"[seal/coff] AUDIT OK — {len(exported)} exports on V8's public surface "
          f"(v8/cppgc/v8_inspector/heap + V8 C entry points); 0 ICU/zlib/Abseil/protobuf leaks")


# --- Fallback export mechanism: generate a .def from the monolith ----------
# Only needed if V8_EXPORT→dllexport via the GN target does NOT yield the v8::
# surface (it should — Chromium ships v8.dll exactly this way). Generating a .def
# is version-fragile (the mangled v8:: surface is large), so it is the fallback,
# not the primary path. Mirror of elf.py's version-script emitter.
def def_gen(monolith, out):
    if shutil.which("llvm-nm"):
        nm = subprocess.run(["llvm-nm", "--defined-only", "--extern-only", str(monolith)],
                            capture_output=True, text=True).stdout
    elif shutil.which("dumpbin"):
        nm = subprocess.run(["dumpbin", "/symbols", str(monolith)],
                            capture_output=True, text=True).stdout
    else:
        raise SystemExit("[seal/coff] need llvm-nm or dumpbin for def-gen")
    # MSVC C++ mangling embeds the namespace as `@v8@@` / `@cppgc@@`. Keep external
    # defined symbols on the v8::/cppgc:: surface; everything else stays internal.
    keep = sorted({tok for line in nm.splitlines() for tok in line.split()
                   if ("@v8@@" in tok or "@cppgc@@" in tok) and tok.startswith("?")})
    Path(out).write_text("EXPORTS\n" + "".join(f"    {s}\n" for s in keep))
    print(f"[seal/coff] wrote {out}: {len(keep)} v8::/cppgc:: exports (fallback .def)")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.required = True  # py3.6 (Rocky 8) compat: required= kwarg is 3.7+
    a = sub.add_parser("audit"); a.add_argument("--lib", required=True)
    a.add_argument("--policy", required=True)
    d = sub.add_parser("def-gen"); d.add_argument("--monolith", required=True)
    d.add_argument("--out", required=True)
    args = p.parse_args()
    if args.cmd == "audit":
        audit(args.lib, args.policy)
    else:
        def_gen(args.monolith, args.out)


if __name__ == "__main__":
    main()
