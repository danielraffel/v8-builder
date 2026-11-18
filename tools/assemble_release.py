#!/usr/bin/env python3
"""Assemble a release directory from the build jobs' packages.

Each build job uploads its archive with the two records that prove it: the
legal manifest and the toolchain receipt, both named after the archive and
their own sha256.  This step, which runs no V8 code, checks that the set is
consistent and writes the two files that describe it:

* ``release-metadata.json`` (schema 1): ``assets`` is the archive list,
  ``manifests`` every archive's ``manifest.json``, and ``provenance`` maps
  each archive to its legal manifest and toolchain receipt;
* ``RELEASE_NOTES.md``.

It refuses a set where an archive lacks either record or has more than one,
where a record names an archive that is not present, where the archives do
not share one release id, or where the archives are not exactly the cells
the dispatch selected (``--expect``, the workflow's ``platforms`` input). A
selected cell that was skipped or failed upstream leaves its archive out, so
it fails here instead of reading as green.
It prints ``release_id=``, ``title=`` and ``display_version=`` lines for
``$GITHUB_OUTPUT``.

Usage: assemble_release.py <release dir> [--expect all|<id>,<id>...] [--v8-version X]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_naming  # noqa: E402

EXPECTED = (
    "v8-mac-arm64", "v8-mac-x86_64", "v8-linux-arm64", "v8-linux-x64",
    "v8-win-x64", "v8-win-arm64", "v8-android-arm64", "v8-ios-simulator-arm64",
)
# The workflow's platform ids (matrix platform/arch) and the cell each packs.
PLATFORM_CELLS = {
    "mac/arm64": "v8-mac-arm64", "mac/x86_64": "v8-mac-x86_64",
    "linux/arm64": "v8-linux-arm64", "linux/x64": "v8-linux-x64",
    "win/x64": "v8-win-x64", "win/arm64": "v8-win-arm64",
    "android/arm64": "v8-android-arm64", "ios/arm64": "v8-ios-simulator-arm64",
}
assert sorted(PLATFORM_CELLS.values()) == sorted(EXPECTED)
RECORD_RE = re.compile(
    r"^(?P<stem>.+)\.(?P<kind>legal-manifest|apple-toolchain|linux-toolchain|android-toolchain|"
    r"windows-toolchain)\.sha256-[0-9a-f]{64}\.json$")


class AssemblyError(Exception):
    pass


def cell_of(archive_name: str, release_id: str) -> str:
    suffix = f"-{release_id}.zip"
    if not archive_name.endswith(suffix):
        raise AssemblyError(f"{archive_name} does not carry release id {release_id}")
    return archive_name[:-len(suffix)]


def expected_cells(platforms: str) -> list[str]:
    """The cells a ``platforms`` input selects: ``all`` or comma-separated exact ids."""
    platforms = platforms.strip()
    if platforms == "all":
        return sorted(EXPECTED)
    ids = [item.strip() for item in platforms.split(",") if item.strip()]
    if not ids:
        raise AssemblyError("no platform selected")
    unknown = sorted(set(ids) - set(PLATFORM_CELLS))
    if unknown:
        raise AssemblyError(f"unknown platform ids {unknown}; known: {sorted(PLATFORM_CELLS)}")
    return sorted({PLATFORM_CELLS[item] for item in ids})


def assemble(release: Path, complete: bool = False, v8_version: str = "",
             expect: str | None = None) -> dict[str, object]:
    zips = sorted(release.glob("*.zip"))
    if not zips:
        raise AssemblyError(f"no archive in {release}")
    manifests = {}
    for archive in zips:
        with zipfile.ZipFile(archive) as handle:
            manifests[archive.name] = json.loads(handle.read("manifest.json"))
    metas = {name: release_naming.release_meta(manifest, v8_version) for name, manifest in manifests.items()}
    ids = {meta["release_id"] for meta in metas.values()}
    if len(ids) != 1:
        raise AssemblyError(f"archives name different releases: {sorted(ids)}")
    release_id = ids.pop()
    cells = {archive.name: cell_of(archive.name, release_id) for archive in zips}
    if complete:
        expect = "all"
    if expect is not None:
        wanted = expected_cells(expect)
        missing = sorted(set(wanted) - set(cells.values()))
        extra = sorted(set(cells.values()) - set(wanted))
        if missing or extra or len(zips) != len(wanted):
            raise AssemblyError(f"incomplete release assets for platforms={expect!r}: "
                                f"count={len(zips)} expected={len(wanted)} missing={missing} extra={extra}")

    provenance: dict[str, dict[str, str]] = {archive.name: {} for archive in zips}
    for record in sorted(release.glob("*.json")):
        if record.name == "release-metadata.json":
            continue
        match = RECORD_RE.match(record.name)
        if not match:
            raise AssemblyError(f"unrecognised record {record.name}")
        archive_name = f"{match.group('stem')}.zip"
        if archive_name not in provenance:
            raise AssemblyError(f"{record.name} names an archive that is not in the release")
        key = "legal_manifest" if match.group("kind") == "legal-manifest" else "toolchain_receipt"
        if key in provenance[archive_name]:
            raise AssemblyError(f"{archive_name} has more than one {key}")
        provenance[archive_name][key] = record.name
    for archive_name, row in provenance.items():
        for key in ("legal_manifest", "toolchain_receipt"):
            if key not in row:
                raise AssemblyError(f"{archive_name} has no {key}")

    for name, manifest in manifests.items():
        defines = manifest.get("consumer_defines")
        if (not isinstance(defines, list) or not defines or
                any(not isinstance(item, str) or not item for item in defines)):
            raise AssemblyError(f"missing consumer_defines in {name}")

    first = metas[zips[0].name]
    pair = first["pair"]
    metadata = {
        "schema": 1,
        "assets": [archive.name for archive in zips],
        "pair": pair,
        "manifests": [manifests[archive.name] for archive in zips],
        "provenance": provenance,
    }
    (release / "release-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (release / "RELEASE_NOTES.md").write_text(
        release_notes(str(first["display_version"]), pair, [manifests[a.name] for a in zips]),
        encoding="utf-8")
    return {"release_id": release_id, "title": first["title"], "display_version": first["display_version"]}


def release_notes(version: str, pair: dict, manifests: list[dict]) -> str:
    lines = [f"**V8 {version}**: a sealed, embeddable, shared V8 that coexists with "
             f"Skia/Dawn (exports only `v8::`/`cppgc::`; ICU/zlib/Abseil kept internal). "
             f"`Intl` on. See the README for the ABI/consumer contract.\n"]
    if pair.get("pair_kind") == "chromium-milestone":
        skia_release = pair.get("skia_release_tag") or pair.get("validated_skia_release")
        if skia_release:
            lines.append(f"**Matched Skia/Dawn release:** [{skia_release}]"
                         f"(https://github.com/danielraffel/skia-builder/releases/tag/{skia_release})  ")
            lines.append("Use either release independently, or both as the matched toolchain.\n")
        lines.append(f"Chromium M{pair.get('milestone', '?')} branch tuple:")
        for label, key in (("Chromium branch", "chromium_branch"), ("Chromium revision", "chromium_revision"),
                           ("DEPS blob", "chromium_deps_blob"), ("Skia", "skia"),
                           ("Skia actually built", "built_skia")):
            lines.append(f"- {label}: `{pair.get(key, 'unknown')}`")
        lines.append(f"- Skia pin identical: `{pair.get('skia_matches_chromium', False)}`")
        lines.append(f"- V8: `{pair.get('v8', 'unknown')}`")
        lines.append(f"- Chromium Dawn: `{pair.get('dawn', 'unknown')}`")
        lines.append(f"- Skia-built Dawn: `{pair.get('built_dawn', 'unknown')}`")
        lines.append(f"- Dawn pins identical: `{pair.get('dawn_matches_chromium', False)}`\n")
    elif pair.get("pair_kind") == "chromium-lkgr":
        lines.append("Chromium LKGR tuple:")
        for label, key in (("Chromium revision", "chromium_revision"), ("DEPS blob", "chromium_deps_blob"),
                           ("Skia", "skia"), ("V8", "v8"), ("Dawn", "dawn")):
            lines.append(f"- {label}: `{pair.get(key, 'unknown')}`")
        lines.append("")
    lines.append("| platform | arch | v8 | sealed | source | validation Skia |")
    lines.append("|---|---|---|---|---|---|")
    for manifest in manifests:
        row_pair = manifest.get("pair", {}) or {}
        skia = row_pair.get("validated_skia_release") or row_pair.get("skia_release") or "-"
        source = row_pair.get("pair_kind") or row_pair.get("source") or "manual"
        plat = manifest.get("platform")
        # iOS carries an env axis (device|simulator) and validates through the
        # Mach-O seal audit and the Simulator ODR gate, not the Skia harness.
        if plat == "ios":
            plat = f"ios ({manifest.get('ios_environment', '?')})"
            skia = "seal+ODR gate"
        lines.append(f"| {plat} | {manifest.get('arch')} | {manifest.get('v8_version')} | "
                     f"{'yes' if manifest.get('sealed') else 'NO'} | {source} | {skia} |")
    lines.append("\nEvery artifact in this release names the same V8 revision "
                 "(single-SHA gate). Each is build + seal-audited (0 ICU/zlib/Abseil "
                 "exports) + identity-checked (no skip-pass).\n")
    lines.append("**Provenance.** Every archive is published with three records named after it: "
                 "`<archive>.legal-manifest.sha256-<digest>.json` (every linked object traced to its "
                 "sources and their license files, with the license texts), "
                 "`<archive>.<apple|linux|android|windows>-toolchain.sha256-<digest>.json` (the compiler, "
                 "linker, SDK and the library's own load-time facts) and "
                 "`<archive>.provenance-attestation.sha256-<digest>.sigstore.json` (a GitHub build-provenance "
                 "attestation over the archives, the records and `release-metadata.json`). "
                 "Verify an archive with `gh attestation verify <archive> --repo danielraffel/v8-builder`.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("release", type=Path)
    parser.add_argument("--complete", action="store_true", help="require every platform (--expect all)")
    parser.add_argument("--expect", help="require exactly the cells this platforms input selects")
    parser.add_argument("--v8-version", default="")
    args = parser.parse_args(argv)
    try:
        meta = assemble(args.release, args.complete, args.v8_version, expect=args.expect)
    except AssemblyError as error:
        print(f"::error::{error}")
        return 1
    for key, value in meta.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
