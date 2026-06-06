#!/usr/bin/env python3
"""
tools/rhel8/check_glibc_floor.py — assert a sealed libv8.so's glibc symbol-version
floor is at or below a maximum (task #24b, release portability).

The floor is the HIGHEST GLIBC_x.y version any symbol in the .so's
`.gnu.version_r` requires. A dynamic loader on a host whose glibc is older than
that floor will refuse to load the library (`version GLIBC_x.y not found`). So
"floor <= 2.28" means: loadable on any glibc >= 2.28 host (RHEL/Rocky/Alma 8,
Amazon Linux 2, SLES 15, Debian 10, Ubuntu 18.04 and newer).

We parse `objdump -T` (the same command in the task's acceptance gate). Each
versioned-undefined symbol line carries a `GLIBC_x.y` tag; we take the max by
version sort. GLIBC_PRIVATE is ignored (it is not a portability constraint — it
is glibc-internal and always satisfied by the matching libc).

Usage:
  check_glibc_floor.py --lib libv8.so [--max 2.28] [--json]

Exit 0 if floor <= max (or no GLIBC refs at all); non-zero otherwise.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def _ver_key(v):
    # "2.28" -> (2, 28); sorts numerically, not lexically (so 2.9 < 2.28).
    return tuple(int(x) for x in v.split("."))


def glibc_floor(lib):
    """Return (floor_str_or_None, sorted_unique_versions) from objdump -T."""
    out = subprocess.run(["objdump", "-T", str(lib)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"objdump -T failed on {lib}:\n{out.stderr}")
    versions = set()
    # objdump -T prints e.g. "... GLIBC_2.28  memcpy". Match the real glibc
    # versions, skip GLIBC_PRIVATE (not a portability floor).
    for m in re.finditer(r"GLIBC_(\d+\.\d+(?:\.\d+)?)", out.stdout):
        versions.add(m.group(1))
    if not versions:
        return None, []
    ordered = sorted(versions, key=_ver_key)
    return ordered[-1], ordered


def main():
    p = argparse.ArgumentParser(description="Assert a libv8.so glibc floor <= max")
    p.add_argument("--lib", required=True, help="path to the sealed libv8.so")
    p.add_argument("--max", default="2.28",
                   help="maximum acceptable glibc floor (default 2.28 = Rocky/RHEL 8)")
    p.add_argument("--json", action="store_true", help="emit a JSON summary")
    args = p.parse_args()

    lib = Path(args.lib)
    if not lib.exists():
        raise SystemExit(f"library not found: {lib}")

    floor, versions = glibc_floor(lib)
    ok = floor is None or _ver_key(floor) <= _ver_key(args.max)

    if args.json:
        print(json.dumps({
            "lib": str(lib),
            "glibc_floor": floor,
            "max_allowed": args.max,
            "all_glibc_versions": versions,
            "ok": ok,
        }, indent=2))
    else:
        print(f"[glibc-floor] {lib.name}: floor={floor or 'none'} "
              f"(allowed <= {args.max}); versions={versions or '[]'}")

    if not ok:
        print(f"[glibc-floor] FAIL — floor {floor} exceeds {args.max}; "
              f"the .so will NOT load on glibc-{args.max} hosts", file=sys.stderr)
        return 1
    print(f"[glibc-floor] OK — loadable on glibc >= {floor or args.max} hosts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
