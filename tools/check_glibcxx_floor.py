#!/usr/bin/env python3
"""Assert the highest required GLIBCXX symbol version of a shared library."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def _key(version):
    return tuple(int(part) for part in version.split("."))


def versions_from_text(text):
    return sorted(set(re.findall(r"GLIBCXX_(\d+\.\d+(?:\.\d+)?)", text)), key=_key)


def glibcxx_floor(lib):
    result = subprocess.run(["objdump", "-T", str(lib)], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"objdump -T failed on {lib}:\n{result.stderr}")
    versions = versions_from_text(result.stdout)
    return (versions[-1] if versions else None), versions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lib", required=True, type=Path)
    parser.add_argument("--max", default="3.4.30")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.lib.exists():
        raise SystemExit(f"library not found: {args.lib}")
    floor, versions = glibcxx_floor(args.lib)
    ok = floor is None or _key(floor) <= _key(args.max)
    summary = {"lib": str(args.lib), "glibcxx_floor": floor,
               "max_allowed": args.max, "all_glibcxx_versions": versions, "ok": ok}
    print(json.dumps(summary, indent=2) if args.json else summary)
    if not ok:
        raise SystemExit(f"GLIBCXX floor {floor} exceeds {args.max}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
