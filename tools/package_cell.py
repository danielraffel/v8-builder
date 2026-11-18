#!/usr/bin/env python3
"""Pack one build cell into its release archive, byte-for-byte reproducibly.

The archive is written in the build job, next to the toolchain that produced
it, so the legal manifest and toolchain receipt can be written against the
exact bytes a release publishes.  Packing the same directory twice gives the
same bytes: members are sorted, every timestamp is 1980-01-01, and a member's
mode is 0755 or 0644 depending only on its owner-execute bit.  Symlinks are
stored as the file they point to, which is what the release has always held
(``actions/upload-artifact`` flattens them).  A symlink, to a file or a
directory, whose real path leaves the cell is refused: the archive holds the
cell and nothing else.

Excluded, as before: the top-level ``validate/`` offline harness (the
android and cross-built Windows validators, which duplicate the shipped
library), ``.DS_Store`` and ``__MACOSX/``.

Usage: package_cell.py <cell dir> <archive.zip>
"""
from __future__ import annotations

import os
import stat
import sys
import zipfile
from pathlib import Path

FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def excluded(rel: str) -> bool:
    parts = rel.split("/")
    return parts[0] == "validate" or "__MACOSX" in parts or parts[-1] == ".DS_Store"


def inside(path: Path, root: Path) -> bool:
    """Whether ``path``'s real path is ``root`` (already resolved) or below it."""
    real = path.resolve()
    return real == root or root in real.parents


def members(cell: Path) -> list[tuple[str, Path]]:
    root = cell.resolve()
    found = []
    escapes = []
    # The real directories on the walked path to each directory, so a symlink
    # back to one of them (a cycle) is not followed forever.
    chain = {str(cell): frozenset({root})}
    for directory, subdirs, names in os.walk(cell, followlinks=True):
        here = Path(directory)
        above = chain.pop(directory)
        kept = []
        for name in sorted(subdirs):
            path = here / name
            if excluded(path.relative_to(cell).as_posix()):
                continue
            if not inside(path, root):
                escapes.append(f"{path.relative_to(cell).as_posix()} -> {path.resolve()}")
                continue
            real = path.resolve()
            if real in above:
                escapes.append(f"{path.relative_to(cell).as_posix()} -> {real} (a cycle)")
                continue
            chain[str(path)] = above | {real}
            kept.append(name)
        subdirs[:] = kept
        for name in names:
            path = here / name
            rel = path.relative_to(cell).as_posix()
            if excluded(rel):
                continue
            if not inside(path, root):
                escapes.append(f"{rel} -> {path.resolve()}")
            elif path.is_file():
                found.append((rel, path))
    if escapes:
        raise SystemExit("package_cell: symlinks leave the cell " + str(cell) + ": " + "; ".join(sorted(escapes)))
    return sorted(found)


def pack(cell: Path, archive: Path) -> list[str]:
    rows = members(cell)
    if not rows:
        raise SystemExit(f"package_cell: {cell} holds nothing to package")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as handle:
        for rel, path in rows:
            info = zipfile.ZipInfo(rel, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if os.stat(path).st_mode & stat.S_IXUSR else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3  # unix, so the mode is honoured
            handle.writestr(info, path.read_bytes(), compresslevel=6)
    return [rel for rel, _ in rows]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    names = pack(Path(argv[1]), Path(argv[2]))
    print(f"package_cell: {argv[2]} ({len(names)} members)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
