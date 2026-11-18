#!/usr/bin/env python3
"""Write the evidence that a validator ran and passed.

A job writes ``<validator>.ran`` only after the validator's run returned 0,
and deletes it before the run, so a marker left by an earlier run cannot
stand in for this one.  The toolchain receipt reads it
(``--consumer-ran-evidence``) and counts the validator as run only when the
first line is that validator's sha256.  ``--library`` adds a line
``library <sha256>`` for the library file the run loaded, for a run whose
loader found the library through its environment (``LD_LIBRARY_PATH`` on an
Android device) rather than through the validator's own run-path.

Usage:
    ran_marker.py clear <marker>
    ran_marker.py write <marker> <validator> [--library <file the run loaded>]
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def marker_text(validator: Path, library: Path | None = None) -> str:
    lines = [sha256_file(validator)]
    if library is not None:
        lines.append(f"library {sha256_file(library)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("clear", "write"))
    parser.add_argument("marker", type=Path)
    parser.add_argument("validator", type=Path, nargs="?")
    parser.add_argument("--library", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "clear":
        args.marker.unlink(missing_ok=True)
        return 0
    if args.validator is None:
        parser.error("write needs the validator that ran")
    args.marker.write_text(marker_text(args.validator, args.library), encoding="utf-8")
    print(f"ran-marker: {args.marker} names {args.validator.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
