#!/usr/bin/env python3
"""Name a cell's shipped library and the link output it was copied from.

Both come from the cell's ``manifest.json``.  ``lib`` is the archive member a
consumer loads (``lib/libv8.dylib``, ``jniLibs/arm64-v8a/libv8.so``,
``V8.framework/V8``).  ``link_output`` is the file the GN link wrote in the
out dir, which the legal manifest walks the build graph from.  A manifest
written before ``link_output`` was recorded names it by the member: its file
name, or for an Apple framework binary ``<Name>.framework/<Name>`` the dylib
GN emits for ``output_name = "<name>"``, ``lib<name>.dylib``.

Usage: cell_library.py <manifest.json>   prints "<member>\\t<link output>"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath


def framework_binary(member: str) -> bool:
    parts = PurePosixPath(member).parts
    return len(parts) >= 2 and parts[-2] == parts[-1] + ".framework"


def library_paths(manifest: dict) -> tuple[str, str]:
    member = str(manifest.get("lib", "")).strip()
    if not member or member.endswith("/"):
        raise SystemExit(f"cell_library: manifest.json names no library member (lib = {member!r})")
    link_output = str(manifest.get("link_output", "")).strip()
    if not link_output:
        name = PurePosixPath(member).name
        link_output = f"lib{name.lower()}.dylib" if framework_binary(member) else name
    return member, link_output


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    member, link_output = library_paths(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    print(f"{member}\t{link_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
