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


def audit(lib, policy_path):
    pol = json.loads(Path(policy_path).read_text())
    deny = pol["audit"]["must_be_zero_global"]
    exported = _exports(lib)
    if not exported:
        print(f"[seal/coff] AUDIT FAIL — no exports found in {lib}", file=sys.stderr)
        raise SystemExit(1)
    # Substring match is robust for both bare C ICU/zlib names (u_*, inflate, ...)
    # and MSVC-mangled C++ (Abseil mangles its namespace as `@absl@@`, ICU as
    # `@icu_NN@`, so the bare 'absl'/'icu' tokens in the policy still match).
    leaks = []
    for d in deny:
        hits = [s for s in exported if d in s]
        if hits:
            leaks.append((d, hits[:3]))
    if leaks:
        print("[seal/coff] AUDIT FAIL — exported denied internals:", file=sys.stderr)
        for d, ex in leaks:
            print(f"   {d}: {' | '.join(ex)}", file=sys.stderr)
        raise SystemExit(1)
    # MSVC mangles the v8 namespace as `@v8@@`; require the v8 surface is present.
    v8n = sum(1 for s in exported if "@v8@@" in s or "@v8@" in s)
    if v8n == 0:
        print(f"[seal/coff] AUDIT FAIL — no v8 symbols exported "
              f"(saw {len(exported)} exports)", file=sys.stderr)
        raise SystemExit(1)
    print(f"[seal/coff] AUDIT OK — 0 denied internal exports; "
          f"{v8n} v8 exports of {len(exported)} total")


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
    sub = p.add_subparsers(dest="cmd", required=True)
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
