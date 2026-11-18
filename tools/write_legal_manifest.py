#!/usr/bin/env python3
"""Write, and check, the content-addressed legal manifest for one V8 cell.

A V8 archive ships a shared library linked from V8 and every third-party
library V8's DEPS pull in (ICU, zlib, Abseil, highway, simdutf, libc++ on
the platforms that bundle it, and so on), plus V8's public headers.  It
carries no license or notice file, so a consumer that redistributes the
library cannot tell from the archive which licenses it inherited.  This
manifest answers that from the build itself, without changing the archive:

    zip member -> the ninja output it packages (by name for the library,
                  by digest for headers and static-archive members)
               -> every object the link edge consumed (``ninja -t query``,
                  expanding static archives, never executables: mksnapshot
                  and torque are host tools and are not linked in)
               -> every source and header each object was compiled from
                  (ninja's compdb and its depfile log)
               -> the license/notice file(s) that govern each source

The manifest is ``exhaustive`` only when every zip member resolved to its
origin, every linked object resolved to at least one source, and every source
resolved to a license file.  Anything that did not resolve is listed under
``gaps``; ``--check`` fails while any gap remains.  Toolchain files (SDK and
system headers, the bundled clang's resource headers, the Rust toolchain's
standard library) are listed under ``toolchain_inputs`` with their digests:
they are recorded, not counted as gaps.

License attribution is the nearest ancestor directory inside the source tree
that holds a license or notice file; ``license_resolution`` names that rule
so a reviewer knows what was inferred.  A generated file is attributed
through the source directory its out-dir path mirrors.

The manifest has no timestamps, so the same inputs produce the same bytes.
``--content-addressed-dir`` also writes a copy named after its own SHA-256,
which is the name the release publishes:
``<archive stem>.legal-manifest.sha256-<digest>.json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Callable

SCHEMA = "v8-builder.legal-manifest.v1"
LICENSE_RESOLUTION = "nearest-ancestor-license-file"
LICENSE_NAME_RE = re.compile(
    r"^(LICEN[CS]E|COPYING|NOTICE|UNLICENSE|COPYRIGHT)([._-].*)?$", re.IGNORECASE)
CONTENT_ADDRESSED_RE = re.compile(r"\.legal-manifest\.sha256-([0-9a-f]{64})\.json$")
OBJECT_SUFFIXES = (".o", ".obj", ".res")
ARCHIVE_SUFFIXES = (".a", ".lib", ".rlib")
SHARED_SUFFIXES = (".so", ".dylib", ".dll", ".tbd")
# Trees inside the V8 checkout that are toolchains, not sources this build
# redistributes in source form.
DEFAULT_TOOLCHAIN_ROOTS = (
    "third_party/llvm-build",
    "third_party/rust-toolchain",
    "third_party/android_toolchain",
    "third_party/android_sdk",
    "third_party/depot_tools",
    "third_party/ninja",
    "build/linux",
    "buildtools/mac",
    "buildtools/linux64",
    "buildtools/win",
)
# Files this repository's build writes into the package; they are governed by
# the builder's own license.
BUILDER_METADATA = {"manifest.json", "V8.framework/Info.plist"}
HEADER_PREFIXES = ("include/", "V8.framework/Headers/")
LIBRARY_MEMBERS = ("lib/", "jniLibs/")

Runner = Callable[[list[str], Path], str]


def run_tool(args: list[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                          errors="replace").stdout


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def content_addressed_name(archive_name: str, data: bytes) -> str:
    stem = archive_name[:-4] if archive_name.endswith(".zip") else archive_name
    return f"{stem}.legal-manifest.sha256-{sha256_bytes(data)}.json"


def norm(path: str) -> str:
    """One spelling per file: real path, and case-folded where the OS folds case."""
    return os.path.normcase(os.path.realpath(path))


def relative(path: str, root: str) -> str | None:
    """``path`` relative to ``root`` (both normalised), in POSIX form, or None."""
    if path == root:
        return ""
    prefix = root.rstrip(os.sep) + os.sep
    if not path.startswith(prefix):
        return None
    return path[len(prefix):].replace(os.sep, "/")


# ---------------------------------------------------------------- ninja ----

def parse_ninja_deps(text: str) -> dict[str, list[str]]:
    """Parse ``ninja -t deps`` output into output -> [inputs] (ninja-relative)."""
    deps: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if not line.strip():
            current = None
        elif line[0] in " \t":
            if current is not None:
                current.append(line.strip())
        else:
            match = re.match(r"^(.*?): #deps \d+", line)
            current = deps.setdefault(match.group(1), []) if match else None
    return deps


def parse_compdb(text: str) -> dict[str, list[str]]:
    """Parse ``ninja -t compdb`` JSON into normalised output -> [normalised source]."""
    result: dict[str, list[str]] = defaultdict(list)
    for entry in json.loads(text or "[]"):
        output, source = entry.get("output"), entry.get("file")
        if not output or not source:
            continue
        directory = entry.get("directory", ".")
        result[norm(os.path.join(directory, output))].append(norm(os.path.join(directory, source)))
    return dict(result)


def parse_query(text: str) -> dict[str, list[str]]:
    """Parse ``ninja -t query`` output into target -> [explicit and implicit inputs].

    Order-only inputs (``||``) are not consumed by the command, so they are
    dropped; implicit inputs (``|``) are kept, because a linker script or an
    export list is an input to the link.
    """
    result: dict[str, list[str]] = {}
    target: str | None = None
    section = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            target = line.rstrip()[:-1]
            result.setdefault(target, [])
            section = ""
            continue
        stripped = line.strip()
        if stripped.startswith("input:"):
            section = "input"
            continue
        if stripped.startswith("outputs:") or stripped.startswith("validations:"):
            section = "other"
            continue
        if section != "input" or target is None:
            continue
        if stripped.startswith("|| "):
            continue
        if stripped.startswith("| "):
            stripped = stripped[2:]
        result[target].append(stripped)
    return result


def parse_ar(data: bytes) -> list[tuple[str, bytes]]:
    """Members of a System V / GNU / COFF ``ar`` archive, skipping symbol tables."""
    if not data.startswith(b"!<arch>\n"):
        return []
    members: list[tuple[str, bytes]] = []
    long_names = b""
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        name = header[:16].decode("ascii", errors="replace").rstrip()
        size = int(header[48:58].decode("ascii").strip() or 0)
        body = data[offset + 60:offset + 60 + size]
        offset += 60 + size + (size & 1)
        if name in ("/", "/SYM64/", "__.SYMDEF", "__.SYMDEF SORTED", "/<ECSYMBOLS>/"):
            continue
        if name == "//":
            long_names = body
            continue
        if name.startswith("#1/"):  # BSD long name stored before the body
            length = int(name[3:])
            name, body = body[:length].rstrip(b"\0").decode("utf-8", errors="replace"), body[length:]
        elif name.startswith("/") and name[1:].isdigit():
            start = int(name[1:])
            end = long_names.find(b"\n", start)
            name = long_names[start:end if end >= 0 else None].decode("utf-8", errors="replace")
        name = name.rstrip("/")
        members.append((name, body))
    return members


# ----------------------------------------------------------- the graph ----

class BuildGraph:
    """Linked objects, their sources, and license ownership for one cell."""

    def __init__(self, source_root: Path, out_dir: Path, ninja: str, runner: Runner = run_tool,
                 builder_root: Path | None = None, toolchain_roots: list[str] | None = None):
        self.source_root = norm(str(source_root))
        self.out_dir = norm(str(out_dir))
        self.out_path = Path(out_dir)
        self.builder_root = Path(builder_root) if builder_root else None
        roots = DEFAULT_TOOLCHAIN_ROOTS if toolchain_roots is None else toolchain_roots
        self.toolchain_roots = [norm(os.path.join(self.source_root, root)) for root in roots]
        self.ninja = ninja
        self.runner = runner
        self.gaps: list[dict[str, str]] = []
        self.sources: dict[str, set[str]] = defaultdict(set)
        self.files: dict[str, dict[str, object]] = {}
        self.licenses: dict[str, dict[str, str]] = {}
        self.repositories: dict[str, dict[str, str]] = {}
        self.toolchain_inputs: dict[str, str | None] = {}
        self._license_cache: dict[str, list[str]] = {}
        self._objects_by_size: dict[int, list[str]] | None = None

    def ninja_out(self, *args: str) -> str:
        return self.runner([self.ninja, "-C", self.out_dir, *args], Path(self.out_dir))

    def resolve(self, item: str) -> str:
        return norm(os.path.join(self.out_dir, item))

    # -- indexing -----------------------------------------------------------
    def index(self) -> None:
        for output, sources in parse_compdb(self.ninja_out("-t", "compdb")).items():
            self.sources[output].update(sources)
        for output, inputs in parse_ninja_deps(self.ninja_out("-t", "deps")).items():
            self.sources[self.resolve(output)].update(self.resolve(item) for item in inputs)

    def linked(self, library: str) -> tuple[list[str], list[str]]:
        """Objects the link of ``library`` consumed, and its other link inputs.

        Static archives (and Rust rlibs) the build produced are expanded to the
        objects they hold.  Anything else (an export list, a linker script, a
        system library) is returned as a link input.
        """
        objects: set[str] = set()
        others: set[str] = set()
        seen: set[str] = set()
        pending = [library]
        while pending:
            batch = [item for item in pending if item not in seen]
            pending = []
            if not batch:
                break
            seen.update(batch)
            for target, inputs in parse_query(self.ninja_out("-t", "query", *batch)).items():
                if target.endswith(".rlib"):
                    # A Rust crate's sources are inputs of its own edge.
                    self.sources[self.resolve(target)].update(
                        self.resolve(item) for item in inputs if not item.endswith(ARCHIVE_SUFFIXES))
                for item in inputs:
                    lower = item.lower()
                    produced = relative(self.resolve(item), self.out_dir) is not None
                    if lower.endswith(OBJECT_SUFFIXES):
                        objects.add(item)
                    elif lower.endswith(ARCHIVE_SUFFIXES) and produced:
                        if lower.endswith(".rlib"):
                            objects.add(item)
                        pending.append(item)
                    else:
                        others.add(item)
        return sorted(objects), sorted(others)

    # -- classification and licenses ---------------------------------------
    def license_for(self, directory: str) -> list[str]:
        """License ids governing ``directory`` (normalised): the nearest ancestor's files."""
        if directory in self._license_cache:
            return self._license_cache[directory]
        found: list[str] = []
        rel = relative(directory, self.source_root)
        if rel is not None:
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                names = []
            names = [name for name in names if LICENSE_NAME_RE.match(name)
                     and os.path.isfile(os.path.join(directory, name))]
            if names:
                for name in names:
                    path = os.path.join(directory, name)
                    license_id = f"source:{(rel + '/' if rel else '') + name}"
                    if license_id not in self.licenses:
                        data = Path(path).read_bytes()
                        self.licenses[license_id] = {"sha256": sha256_bytes(data),
                                                     "text": data.decode("utf-8", errors="replace")}
                    found.append(license_id)
            elif directory != self.source_root:
                found = self.license_for(os.path.dirname(directory))
        self._license_cache[directory] = found
        return found

    def builder_license(self) -> list[str]:
        if not self.builder_root:
            return []
        for name in sorted(os.listdir(self.builder_root)):
            if LICENSE_NAME_RE.match(name):
                path = self.builder_root / name
                license_id = f"builder:{name}"
                data = path.read_bytes()
                self.licenses.setdefault(license_id, {
                    "sha256": sha256_bytes(data), "text": data.decode("utf-8", errors="replace")})
                return [license_id]
        return []

    def repository_for(self, path: str) -> str:
        directory = os.path.dirname(path)
        while True:
            rel = relative(directory, self.source_root)
            if rel is None:
                return "."
            if os.path.exists(os.path.join(directory, ".git")):
                key = rel or "."
                if key not in self.repositories:
                    row: dict[str, str] = {}
                    for field, args in (("commit", ["git", "rev-parse", "HEAD"]),
                                        ("remote", ["git", "config", "--get", "remote.origin.url"])):
                        try:
                            row[field] = self.runner(args, Path(directory)).strip()
                        except (OSError, subprocess.CalledProcessError):
                            row[field] = ""
                    self.repositories[key] = row
                return key
            if directory == self.source_root:
                return "."
            directory = os.path.dirname(directory)

    def in_toolchain(self, path: str) -> bool:
        return any(relative(path, root) is not None for root in self.toolchain_roots)

    def mirrored_source_dir(self, path: str) -> str | None:
        rel = relative(path, self.out_dir)
        if rel is None:
            return None
        parts = PurePosixPath(rel).parts[:-1]
        if parts and parts[0] in ("gen", "obj"):
            parts = parts[1:]
        candidate = os.path.join(self.source_root, *parts) if parts else self.source_root
        candidate = os.path.normcase(candidate)
        while relative(candidate, self.source_root) is not None and not os.path.isdir(candidate):
            candidate = os.path.dirname(candidate)
        return candidate if relative(candidate, self.source_root) is not None else None

    def file_id(self, path: str) -> str | None:
        """Record one input file (normalised path); return its id, or None for a toolchain input."""
        if self.in_toolchain(path) or (relative(path, self.source_root) is None
                                       and relative(path, self.out_dir) is None):
            if path not in self.toolchain_inputs:
                self.toolchain_inputs[path] = sha256_file(Path(path)) if os.path.isfile(path) else None
            return None
        out_rel = relative(path, self.out_dir)
        if out_rel is not None:
            file_id = f"generated:{out_rel}"
            if file_id not in self.files:
                mirrored = self.mirrored_source_dir(path)
                licenses = self.license_for(mirrored) if mirrored else []
                self.files[file_id] = {
                    "sha256": sha256_file(Path(path)) if os.path.isfile(path) else None,
                    "licenses": licenses,
                    "generated_from": (f"source:{relative(mirrored, self.source_root) or '.'}"
                                       if mirrored else None),
                }
                if not licenses:
                    self.gaps.append({"kind": "generated-without-license", "path": file_id})
            return file_id
        rel = relative(path, self.source_root)
        file_id = f"source:{rel}"
        if file_id not in self.files:
            licenses = self.license_for(os.path.dirname(path))
            exists = os.path.isfile(path)
            self.files[file_id] = {
                "sha256": sha256_file(Path(path)) if exists else None,
                "licenses": licenses,
                "repository": self.repository_for(path),
            }
            if not licenses:
                self.gaps.append({"kind": "source-without-license", "path": file_id})
            if not exists:
                self.gaps.append({"kind": "source-missing", "path": file_id})
        return file_id

    def object_licenses(self, item: str) -> tuple[list[str], list[str]]:
        """(file ids, license ids) for one linked object (ninja-relative path)."""
        files, licenses = set(), set()
        resolved = self.resolve(item)
        sources = self.sources.get(resolved, ())
        if not sources and relative(resolved, self.out_dir) is None \
                and relative(resolved, self.source_root) is not None:
            # A checked-in input the link consumes directly (a crate's
            # precompiled .res data, say) is its own source.
            sources = (resolved,)
        for source in sorted(sources):
            file_id = self.file_id(source)
            if file_id:
                files.add(file_id)
                licenses.update(self.files[file_id]["licenses"])
        return sorted(files), sorted(licenses)

    def objects_by_digest(self, size: int, digest: str) -> list[str]:
        """Out-dir object files with this content (sizes indexed once, digests on demand)."""
        if self._objects_by_size is None:
            self._objects_by_size = defaultdict(list)
            for directory, _subdirs, names in os.walk(self.out_dir):
                for name in names:
                    if name.lower().endswith(OBJECT_SUFFIXES):
                        path = os.path.join(directory, name)
                        self._objects_by_size[os.path.getsize(path)].append(path)
        return sorted(path for path in self._objects_by_size.get(size, [])
                      if sha256_file(Path(path)) == digest)


# ------------------------------------------------------------- manifest ----

def build_manifest(archive: Path, graph: BuildGraph, library: str,
                   platform: str = "", arch: str = "", member: str = "") -> dict[str, object]:
    """``library`` is the link output in the out dir; ``member`` the archive
    member that ships it (by default, the member whose file name is ``library``)."""
    graph.index()
    objects, link_inputs = graph.linked(library)
    if not objects:
        graph.gaps.append({"kind": "link-without-objects", "path": library})
    object_rows: dict[str, list[str]] = {}
    library_licenses: set[str] = set()
    for item in objects:
        files, licenses = graph.object_licenses(item)
        if not files:
            graph.gaps.append({"kind": "object-without-sources", "path": item})
        elif not licenses:
            graph.gaps.append({"kind": "object-without-license", "path": item})
        object_rows[item] = licenses
        library_licenses.update(licenses)
    link_rows: dict[str, str | None] = {}
    for item in link_inputs:
        file_id = graph.file_id(graph.resolve(item))
        link_rows[item] = file_id
        if file_id:
            library_licenses.update(graph.files[file_id]["licenses"])
    root_licenses = graph.license_for(graph.source_root)

    zip_members = []
    with zipfile.ZipFile(archive) as handle:
        for info in sorted(handle.infolist(), key=lambda row: row.filename):
            if info.is_dir():
                continue
            name = info.filename
            data = handle.read(info)
            row: dict[str, object] = {"name": name, "sha256": sha256_bytes(data)}
            base = PurePosixPath(name).name
            if name in BUILDER_METADATA:
                row["kind"] = "builder-metadata"
                row["licenses"] = graph.builder_license()
            elif name.startswith(HEADER_PREFIXES):
                prefix = next(p for p in HEADER_PREFIXES if name.startswith(p))
                source = norm(os.path.join(graph.source_root, "include", name[len(prefix):]))
                if os.path.isfile(source) and sha256_file(Path(source)) == row["sha256"]:
                    file_id = graph.file_id(source)
                    row["kind"] = "header"
                    row["origin"] = file_id
                    row["licenses"] = graph.files[file_id]["licenses"] if file_id else []
                else:
                    row["kind"] = "unresolved"
                    row["licenses"] = []
            elif name == member if member else base == library:
                row["kind"] = "linked-library"
                row["origin"] = f"generated:{library}"
                built = os.path.join(graph.out_dir, library)
                row["origin_sha256"] = sha256_file(Path(built)) if os.path.isfile(built) else None
                row["note"] = "the packaged copy is stripped, so its bytes differ from the link output"
                row["licenses"] = sorted(library_licenses)
            elif base.lower().endswith(".dll.lib") and name.startswith(LIBRARY_MEMBERS):
                row["kind"] = "import-library"
                row["origin"] = f"generated:{base}"
                row["licenses"] = root_licenses
            elif base.lower().endswith((".lib", ".a")) and name.startswith(LIBRARY_MEMBERS):
                row["kind"] = "static-archive"
                members, licenses = [], set()
                for member_name, body in parse_ar(data):
                    found = graph.objects_by_digest(len(body), sha256_bytes(body))
                    rel = relative(found[0], graph.out_dir) if found else None
                    member_licenses: list[str] = []
                    if rel is not None:
                        _files, member_licenses = graph.object_licenses(rel)
                    members.append({"name": member_name, "object": rel, "licenses": member_licenses})
                    licenses.update(member_licenses)
                    if rel is None or not member_licenses:
                        graph.gaps.append({"kind": "archive-member-unresolved",
                                           "path": f"{name}:{member_name}"})
                if not members:
                    graph.gaps.append({"kind": "archive-without-members", "path": name})
                row["members"] = members
                row["licenses"] = sorted(licenses)
            else:
                row["kind"] = "unresolved"
                row["licenses"] = []
            if row["kind"] == "unresolved" or not row["licenses"]:
                graph.gaps.append({"kind": "member-unresolved", "path": name})
            zip_members.append(row)

    gaps = sorted({json.dumps(gap, sort_keys=True) for gap in graph.gaps})
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "license_resolution": LICENSE_RESOLUTION,
        "archive": {"name": archive.name, "sha256": sha256_file(archive)},
        "target": {"platform": platform, "arch": arch, "library": library,
                   "member": member or next((row["name"] for row in zip_members
                                             if row["kind"] == "linked-library"), "")},
        "repositories": dict(sorted(graph.repositories.items())),
        "zip_members": zip_members,
        "linked_objects": dict(sorted(object_rows.items())),
        "link_inputs": dict(sorted(link_rows.items())),
        "files": dict(sorted(graph.files.items())),
        "licenses": dict(sorted(graph.licenses.items())),
        "toolchain_inputs": {key.replace(os.sep, "/"): value
                             for key, value in sorted(graph.toolchain_inputs.items())},
        "gaps": [json.loads(gap) for gap in gaps],
    }
    manifest["exhaustive"] = not manifest["gaps"]
    manifest["summary"] = {
        "zip_members": len(zip_members),
        "linked_objects": len(object_rows),
        "files": len(graph.files),
        "licenses": len(graph.licenses),
        "toolchain_inputs": len(graph.toolchain_inputs),
        "gaps": len(manifest["gaps"]),
    }
    return manifest


def validate_manifest(manifest: dict, archive: Path | None = None) -> list[str]:
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA:
        problems.append(f"schema = {manifest.get('schema')!r}")
    if manifest.get("gaps"):
        problems.append(f"{len(manifest['gaps'])} unresolved gap(s), first: {manifest['gaps'][0]}")
    if manifest.get("exhaustive") is not True:
        problems.append("exhaustive is not true")
    licenses = manifest.get("licenses") or {}
    if not licenses:
        problems.append("no license recorded")
    for license_id, row in licenses.items():
        if not row.get("text", "").strip():
            problems.append(f"license {license_id} has no text")
        elif sha256_bytes(row["text"].encode("utf-8")) != row.get("sha256") and "�" not in row["text"]:
            problems.append(f"license {license_id} text does not match its sha256")
    for file_id, row in (manifest.get("files") or {}).items():
        if not row.get("licenses"):
            problems.append(f"file {file_id} has no license")
        for license_id in row.get("licenses", []):
            if license_id not in licenses:
                problems.append(f"file {file_id} names unknown license {license_id}")
    if not manifest.get("linked_objects"):
        problems.append("no linked object recorded")
    members = manifest.get("zip_members") or []
    if not any(row.get("kind") == "linked-library" for row in members):
        problems.append("no zip member is the linked library")
    member = str((manifest.get("target") or {}).get("member", ""))
    if not member:
        problems.append("target.member names no library member")
    elif not any(row.get("name") == member and row.get("kind") == "linked-library" for row in members):
        problems.append(f"target.member {member!r} is not the linked-library zip member")
    for row in members:
        if not row.get("licenses"):
            problems.append(f"member {row.get('name')} has no license")
        for license_id in row.get("licenses", []):
            if license_id not in licenses:
                problems.append(f"member {row.get('name')} names unknown license {license_id}")
    if archive is not None:
        actual = sha256_file(archive)
        if (manifest.get("archive") or {}).get("sha256") != actual:
            problems.append(f"manifest archive sha256 != {actual}")
        with zipfile.ZipFile(archive) as handle:
            observed = {info.filename: sha256_bytes(handle.read(info))
                        for info in handle.infolist() if not info.is_dir()}
        recorded = {row.get("name"): row.get("sha256") for row in members}
        if observed != recorded:
            missing = sorted(set(observed) ^ set(recorded))
            changed = sorted(name for name in set(observed) & set(recorded)
                             if observed[name] != recorded[name])
            problems.append(f"zip members differ from the archive: added/removed {missing[:5]}, "
                            f"changed {changed[:5]}")
    return problems


def find_ninja(source_root: Path) -> str | None:
    """An absolute ninja path: the graph runs it with the out dir as its cwd."""
    for name in ("ninja.exe", "ninja"):
        candidate = source_root / "third_party" / "ninja" / name
        if candidate.is_file():
            return os.path.abspath(candidate)
    found = shutil.which("ninja")
    return os.path.abspath(found) if found else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--out-dir", type=Path, help="the GN out dir the cell was built in")
    parser.add_argument("--library", help="the link output in --out-dir (libv8.dylib, v8.dll, ...)")
    parser.add_argument("--member", default="",
                        help="the archive member that ships --library (V8.framework/V8, lib/v8.dll, ...)")
    parser.add_argument("--platform", default="")
    parser.add_argument("--arch", default="")
    parser.add_argument("--builder-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--ninja")
    parser.add_argument("--content-addressed-dir", type=Path)
    parser.add_argument("--check", action="store_true",
                        help="validate an existing manifest against --archive instead of writing one")
    args = parser.parse_args(argv)

    if args.check:
        manifest = json.loads(args.output.read_text(encoding="utf-8"))
        problems = validate_manifest(manifest, args.archive)
        match = CONTENT_ADDRESSED_RE.search(args.output.name)
        if match and match.group(1) != sha256_file(args.output):
            problems.append(f"{args.output.name} does not carry its own sha256")
        for problem in problems:
            print(f"legal-manifest: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"legal-manifest: {args.output} is proven")
        return 0

    if not (args.source_root and args.out_dir and args.library):
        parser.error("--source-root, --out-dir and --library are required to write a manifest")
    ninja = args.ninja
    if ninja and ("/" in ninja or os.sep in ninja):
        ninja = os.path.abspath(ninja)
    ninja = ninja or find_ninja(args.source_root)
    if not ninja:
        parser.error("no ninja binary found; pass --ninja")
    graph = BuildGraph(args.source_root, args.out_dir, ninja, builder_root=args.builder_root)
    manifest = build_manifest(args.archive, graph, args.library, args.platform, args.arch, args.member)
    data = canonical_bytes(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"legal-manifest: {json.dumps(manifest['summary'])}")
    for gap in manifest["gaps"][:50]:
        print(f"legal-manifest: gap: {gap}", file=sys.stderr)
    problems = validate_manifest(manifest, args.archive)
    for problem in problems:
        print(f"legal-manifest: {problem}", file=sys.stderr)
    # Only a proven manifest gets the name a release publishes.
    if args.content_addressed_dir and not problems:
        args.content_addressed_dir.mkdir(parents=True, exist_ok=True)
        target = args.content_addressed_dir / content_addressed_name(args.archive.name, data)
        target.write_bytes(data)
        print(f"legal-manifest: content-addressed copy {target}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
