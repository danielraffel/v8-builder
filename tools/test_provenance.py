#!/usr/bin/env python3
"""Unit tests for the release provenance tools.

Covers the reproducible packer, release naming, the binary-facts reader, the
legal manifest (against a fake ninja graph) and the toolchain receipt (against
a fake toolchain).  Each positive case is paired with controls that must make
``--check`` fail: a tampered archive, a library for another architecture, a
library linked by another lld, an unlicensed source, an unknown member.

Runs two ways, no third-party deps required:
    python3 -m pytest tools/test_provenance.py
    python3 tools/test_provenance.py
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import binfacts  # noqa: E402
import package_cell  # noqa: E402
import release_naming  # noqa: E402
import write_legal_manifest as legal  # noqa: E402
import write_toolchain_receipt as receipt_tool  # noqa: E402


# ------------------------------------------------------------- fixtures ----

def macho_dylib(cputype: int = 0x0100000C, platform: int = 1, minos: int = 0x000B0000,
                lld: str = "24.0.0", needed: tuple[str, ...] = ("/usr/lib/libSystem.B.dylib",)) -> bytes:
    def dylib_cmd(cmd: int, name: str) -> bytes:
        raw = name.encode() + b"\0"
        size = (24 + len(raw) + 7) & ~7
        return struct.pack("<IIIIII", cmd, size, 24, 0, 0, 0) + raw.ljust(size - 24, b"\0")

    major, minor, patch = (int(part) for part in lld.split("."))
    build = struct.pack("<IIIIII", 0x32, 32, platform, minos, 0x000F0500, 1) + \
        struct.pack("<II", 4, (major << 16) | (minor << 8) | patch)
    commands = [dylib_cmd(0x0D, "@rpath/libv8.dylib"), build] + [dylib_cmd(0x0C, item) for item in needed]
    body = b"".join(commands)
    header = struct.pack("<IiiIIII", 0xFEEDFACF, cputype, 0, 6, len(commands), len(body), 0) + b"\0" * 4
    return header + body + b"\0" * 64


def elf_so(machine: int = 0x3E, needed: tuple[str, ...] = ("libc.so.6",),
           versions: tuple[str, ...] = ("GLIBC_2.17", "GLIBC_2.34", "GLIBCXX_3.4.30")) -> bytes:
    dynstr = b"\0"
    offsets = {}
    for text in ("libv8.so", *needed, *versions):
        offsets[text] = len(dynstr)
        dynstr += text.encode() + b"\0"
    dynamic = b"".join(struct.pack("<qQ", 1, offsets[item]) for item in needed)
    dynamic += struct.pack("<qQ", 14, offsets["libv8.so"]) + struct.pack("<qQ", 0, 0)
    aux = b""
    for index, version in enumerate(versions):
        nxt = 16 if index < len(versions) - 1 else 0
        aux += struct.pack("<IHHII", 0, 0, index + 2, offsets[version], nxt)
    verneed = struct.pack("<HHIII", 1, len(versions), offsets[needed[0]], 16, 0) + aux
    shstr = b"\0.dynstr\0.dynamic\0.gnu.version_r\0.shstrtab\0"
    names = {".dynstr": 1, ".dynamic": 9, ".gnu.version_r": 18, ".shstrtab": 33}
    blobs = [(".dynstr", 3, dynstr, 0), (".dynamic", 6, dynamic, 1),
             (".gnu.version_r", 0x6FFFFFFE, verneed, 1), (".shstrtab", 3, shstr, 0)]
    data = bytearray(64)
    placed = []
    for name, kind, blob, link in blobs:
        placed.append((name, kind, len(data), len(blob), link))
        data += blob
    shoff = len(data)
    data += b"\0" * 64  # null section
    for name, kind, offset, size, link in placed:
        data += struct.pack("<IIQQQQIIQQ", names[name], kind, 0, 0, offset, size, link, 0, 1, 0)
    header = b"\x7fELF" + bytes([2, 1, 1]) + b"\0" * 9 + struct.pack(
        "<HHIQQQIHHHHHH", 3, machine, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(placed) + 1, len(placed))
    data[:64] = header
    return bytes(data)


def write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as handle:
        for name, data in sorted(members.items()):
            handle.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data)
    return path


def expect_failure(problems: list[str], fragment: str) -> None:
    assert any(fragment in problem for problem in problems), (fragment, problems)


# --------------------------------------------------------------- packer ----

def test_package_cell_is_reproducible_and_lean():
    with tempfile.TemporaryDirectory() as tmp:
        cell = Path(tmp) / "mac-arm64"
        (cell / "lib").mkdir(parents=True)
        (cell / "include" / "cppgc").mkdir(parents=True)
        (cell / "validate").mkdir()
        (cell / "lib" / "libv8.dylib").write_bytes(b"dylib")
        os.chmod(cell / "lib" / "libv8.dylib", 0o755)
        (cell / "include" / "v8.h").write_text("// v8\n")
        (cell / "include" / "cppgc" / "heap.h").write_text("// heap\n")
        (cell / "include" / "alias.h").symlink_to(cell / "include" / "v8.h")
        (cell / "manifest.json").write_text("{}\n")
        (cell / "validate" / "v8_identity_validator").write_bytes(b"exe")
        (cell / ".DS_Store").write_bytes(b"junk")
        first, second = Path(tmp) / "a.zip", Path(tmp) / "b.zip"
        package_cell.pack(cell, first)
        os.utime(cell / "include" / "v8.h", (1, 1))
        package_cell.pack(cell, second)
        assert first.read_bytes() == second.read_bytes()
        with zipfile.ZipFile(first) as handle:
            names = handle.namelist()
            assert names == sorted(names)
            assert "validate/v8_identity_validator" not in names and ".DS_Store" not in names
            assert handle.read("include/alias.h") == b"// v8\n"
            modes = {info.filename: (info.external_attr >> 16) & 0o777 for info in handle.infolist()}
            assert modes["lib/libv8.dylib"] == 0o755 and modes["manifest.json"] == 0o644


# --------------------------------------------------------------- naming ----

def test_release_ids_and_attested_tags():
    milestone = {"v8_version": "15.8.62.6", "pair": {"pair_kind": "chromium-milestone", "milestone": 158,
                                                     "v8": "38351a002e2c" + "0" * 28}}
    assert release_naming.release_meta(milestone)["release_id"] == "m158-15.8.62.6-38351a002e2c"
    lkgr = {"v8_version": "16.0.43", "pair": {"pair_kind": "chromium-lkgr", "v8": "4124bd6c411b" + "1" * 28}}
    assert release_naming.release_meta(lkgr)["release_id"] == "16.0.43-lkgr-4124bd6c411b"
    assert release_naming.release_meta({"v8_version": "15.1.27"}, "15.1 27")["release_id"] == "15.1-27"
    rid = "m158-15.8.62.6-38351a002e2c"
    assert release_naming.publish_tag(rid) == (f"v8-{rid}", f"v8-{rid}")
    assert release_naming.publish_tag(rid, f"v8-{rid}-attested.1")[1] == f"v8-{rid}-attested.1"
    for bad in (f"v8-{rid}-attested.0", f"v8-{rid}-attested.x", "v8-other-attested.1", "anything"):
        try:
            release_naming.publish_tag(rid, bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} was accepted")


# ------------------------------------------------------------ binfacts ----

def test_binfacts_reads_macho_and_elf():
    facts = binfacts.binary_facts(macho_dylib())
    assert facts["arch"] == "arm64" and facts["install_name"] == "@rpath/libv8.dylib"
    assert facts["build_version"]["platform"] == "macos" and facts["build_version"]["minos"] == "11.0.0"
    assert facts["build_version"]["tools"] == [{"tool": "lld", "version": "24.0.0"}]
    elf = binfacts.binary_facts(elf_so())
    assert elf["arch"] == "x64" and elf["soname"] == "libv8.so" and elf["needed"] == ["libc.so.6"]
    assert elf["symbol_versions"] == {"max_glibc": "2.34", "max_glibcxx": "3.4.30"}
    assert binfacts.binary_facts(b"not a binary") == {"format": "unknown"}


# ------------------------------------------------------- legal manifest ----

def test_ninja_parsers():
    query = ("libv8.so:\n  input: solink\n    obj/libv8_monolith.a\n    | ../../v8_embedder_exports.map\n"
             "    || obj/stamp.stamp\n  outputs:\n    v8_identity_validator\n")
    assert legal.parse_query(query) == {"libv8.so": ["obj/libv8_monolith.a", "../../v8_embedder_exports.map"]}
    deps = legal.parse_ninja_deps("obj/a.o: #deps 2, deps mtime 1 (VALID)\n    ../../a.cc\n    ../../a.h\n\n")
    assert deps == {"obj/a.o": ["../../a.cc", "../../a.h"]}
    long_name = b"a_rather_long_object_name.obj/\n"
    ar = b"!<arch>\n" + b"/".ljust(16) + b"0".ljust(32) + b"4".ljust(10) + b"`\n" + b"symt"
    ar += b"//".ljust(16) + b"0".ljust(32) + str(len(long_name)).encode().ljust(10) + b"`\n" + long_name + b"\n"
    ar += b"/0".ljust(16) + b"0".ljust(32) + b"3".ljust(10) + b"`\n" + b"obj\n"
    ar += b"short.o/".ljust(16) + b"0".ljust(32) + b"2".ljust(10) + b"`\n" + b"ok"
    assert legal.parse_ar(ar) == [("a_rather_long_object_name.obj", b"obj"), ("short.o", b"ok")]


class FakeTree:
    """A V8-shaped checkout with an out dir, and the ninja answers for it."""

    def __init__(self, root: Path):
        self.root = root
        self.src = root / "v8"
        self.out = self.src / "out" / "mac-arm64"
        for directory in ("src/api", "third_party/icu/source", "include", "third_party/llvm-build/include"):
            (self.src / directory).mkdir(parents=True, exist_ok=True)
        (self.out / "obj" / "src").mkdir(parents=True)
        (self.out / "gen").mkdir()
        (self.src / "LICENSE").write_text("V8 license\n")
        (self.src / "third_party" / "icu" / "LICENSE").write_text("ICU license\n")
        (self.src / "src" / "api" / "api.cc").write_text("// api\n")
        (self.src / "third_party" / "icu" / "source" / "uloc.cpp").write_text("// icu\n")
        (self.src / "include" / "v8.h").write_text("// header\n")
        (self.src / "third_party" / "llvm-build" / "include" / "stddef.h").write_text("// clang\n")
        (self.src / "v8_embedder_exports.txt").write_text("__ZN2v8*\n")
        (self.out / "gen" / "embedded.S").write_text("// generated\n")
        (self.out / "libv8.dylib").write_bytes(b"unstripped")
        self.builder = root / "builder"
        self.builder.mkdir()
        (self.builder / "LICENSE").write_text("builder license\n")
        self.extra_object_sources: list[str] = []

    def runner(self, args: list[str], cwd: Path) -> str:
        if args[:1] == ["git"]:
            return "deadbeef\n" if "rev-parse" in args else "https://example.invalid/v8.git\n"
        tool = args[args.index("-t") + 1]
        if tool == "compdb":
            entries = [{"directory": str(self.out), "file": "../../src/api/api.cc", "output": "obj/src/api.o"},
                       {"directory": str(self.out), "file": "../../third_party/icu/source/uloc.cpp",
                        "output": "obj/icu/uloc.o"},
                       {"directory": str(self.out), "file": "gen/embedded.S", "output": "obj/embedded.o"}]
            entries += [{"directory": str(self.out), "file": path, "output": "obj/extra.o"}
                        for path in self.extra_object_sources]
            return json.dumps(entries)
        if tool == "deps":
            return ("obj/src/api.o: #deps 3, deps mtime 1 (VALID)\n    ../../src/api/api.cc\n"
                    "    ../../include/v8.h\n    ../../third_party/llvm-build/include/stddef.h\n\n"
                    "obj/icu/uloc.o: #deps 1, deps mtime 1 (VALID)\n    ../../third_party/icu/source/uloc.cpp\n\n")
        if tool == "query":
            targets = args[args.index("query") + 1:]
            text = ""
            for target in targets:
                if target == "libv8.dylib":
                    text += ("libv8.dylib:\n  input: solink\n    obj/libv8_monolith.a\n"
                             "    | ../../v8_embedder_exports.txt\n")
                elif target == "obj/libv8_monolith.a":
                    objects = ["obj/src/api.o", "obj/icu/uloc.o", "obj/embedded.o"]
                    if self.extra_object_sources:
                        objects.append("obj/extra.o")
                    text += "obj/libv8_monolith.a:\n  input: alink\n" + "".join(f"    {o}\n" for o in objects)
            return text
        raise AssertionError(args)

    def archive(self, path: Path, extra: dict[str, bytes] | None = None) -> Path:
        members = {"lib/libv8.dylib": b"stripped", "include/v8.h": b"// header\n",
                   "manifest.json": b'{"lib": "lib/libv8.dylib"}\n'}
        members.update(extra or {})
        return write_zip(path, members)

    def graph(self) -> legal.BuildGraph:
        return legal.BuildGraph(self.src, self.out, "ninja", runner=self.runner, builder_root=self.builder)


def test_legal_manifest_is_exhaustive_and_checks_its_archive():
    with tempfile.TemporaryDirectory() as tmp:
        tree = FakeTree(Path(tmp))
        archive = tree.archive(Path(tmp) / "v8-mac-arm64-x.zip")
        manifest = legal.build_manifest(archive, tree.graph(), "libv8.dylib", "mac", "arm64")
        assert manifest["gaps"] == [] and manifest["exhaustive"] is True, manifest["gaps"]
        assert legal.validate_manifest(manifest, archive) == []
        members = {row["name"]: row for row in manifest["zip_members"]}
        assert members["lib/libv8.dylib"]["kind"] == "linked-library"
        assert set(members["lib/libv8.dylib"]["licenses"]) == {"source:LICENSE",
                                                              "source:third_party/icu/LICENSE"}
        assert members["include/v8.h"]["licenses"] == ["source:LICENSE"]
        assert members["manifest.json"]["licenses"] == ["builder:LICENSE"]
        assert manifest["files"]["generated:gen/embedded.S"]["licenses"] == ["source:LICENSE"]
        assert any(path.endswith("stddef.h") for path in manifest["toolchain_inputs"])
        assert manifest["link_inputs"] == {"../../v8_embedder_exports.txt": "source:v8_embedder_exports.txt"}
        # Same inputs, same bytes.
        again = legal.build_manifest(archive, tree.graph(), "libv8.dylib", "mac", "arm64")
        assert legal.canonical_bytes(again) == legal.canonical_bytes(manifest)

        # Control: another archive's bytes.
        tampered = tree.archive(Path(tmp) / "tampered.zip", {"include/v8.h": b"// changed\n"})
        expect_failure(legal.validate_manifest(manifest, tampered), "archive sha256")
        expect_failure(legal.validate_manifest(manifest, tampered), "zip members differ")
        # Control: a member nothing produced.
        stray = tree.archive(Path(tmp) / "stray.zip", {"lib/README": b"?"})
        stray_manifest = legal.build_manifest(stray, tree.graph(), "libv8.dylib", "mac", "arm64")
        assert {"kind": "member-unresolved", "path": "lib/README"} in stray_manifest["gaps"]
        expect_failure(legal.validate_manifest(stray_manifest, stray), "unresolved gap")


def test_legal_manifest_reports_an_unlicensed_source():
    with tempfile.TemporaryDirectory() as tmp:
        tree = FakeTree(Path(tmp))
        (tree.src / "LICENSE").unlink()
        archive = tree.archive(Path(tmp) / "v8-mac-arm64-x.zip")
        manifest = legal.build_manifest(archive, tree.graph(), "libv8.dylib", "mac", "arm64")
        assert {"kind": "source-without-license", "path": "source:src/api/api.cc"} in manifest["gaps"]
        assert manifest["exhaustive"] is False
        expect_failure(legal.validate_manifest(manifest, archive), "exhaustive is not true")


def test_legal_manifest_main_writes_only_proven_content_addressed_copies():
    with tempfile.TemporaryDirectory() as tmp:
        tree = FakeTree(Path(tmp))
        archive = tree.archive(Path(tmp) / "v8-mac-arm64-x.zip")
        manifest = legal.build_manifest(archive, tree.graph(), "libv8.dylib", "mac", "arm64")
        data = legal.canonical_bytes(manifest)
        named = Path(tmp) / legal.content_addressed_name(archive.name, data)
        named.write_bytes(data)
        assert legal.main([str(named), "--archive", str(archive), "--check"]) == 0
        renamed = Path(tmp) / f"v8-mac-arm64-x.legal-manifest.sha256-{'0' * 64}.json"
        renamed.write_bytes(data)
        assert legal.main([str(renamed), "--archive", str(archive), "--check"]) == 1


def test_find_ninja_is_absolute_for_a_relative_source_root():
    # The graph runs ninja with the out dir as its cwd, so a relative path
    # resolves against the wrong directory there.
    with tempfile.TemporaryDirectory() as tmp:
        ninja_dir = Path(tmp) / "v8" / "third_party" / "ninja"
        ninja_dir.mkdir(parents=True)
        (ninja_dir / "ninja").write_bytes(b"ninja")
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            found = legal.find_ninja(Path("v8"))
        finally:
            os.chdir(cwd)
        assert found and os.path.isabs(found), found
        assert Path(found).resolve() == (ninja_dir / "ninja").resolve()


def test_legal_manifest_resolves_a_checked_in_link_input():
    with tempfile.TemporaryDirectory() as tmp:
        tree = FakeTree(Path(tmp))
        data_dir = tree.src / "third_party" / "zoneinfo" / "src" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "zoneinfo64.res").write_bytes(b"res")
        (tree.src / "third_party" / "zoneinfo" / "LICENSE").write_text("zoneinfo license\n")
        base_runner = tree.runner

        def runner(args, cwd):
            text = base_runner(args, cwd)
            if "query" in args and "obj/libv8_monolith.a" in args:
                text += "    ../../third_party/zoneinfo/src/data/zoneinfo64.res\n"
            return text
        tree.runner = runner
        archive = tree.archive(Path(tmp) / "v8-win-x64-x.zip")
        manifest = legal.build_manifest(archive, tree.graph(), "libv8.dylib", "win", "x64")
        assert manifest["gaps"] == [], manifest["gaps"]
        assert manifest["linked_objects"]["../../third_party/zoneinfo/src/data/zoneinfo64.res"] == \
            ["source:third_party/zoneinfo/LICENSE"]

        # Control: without its license the same input is a gap, not silently licensed.
        (tree.src / "third_party" / "zoneinfo" / "LICENSE").unlink()
        (tree.src / "LICENSE").unlink()
        bare = legal.build_manifest(archive, tree.graph(), "libv8.dylib", "win", "x64")
        assert {"kind": "object-without-license",
                "path": "../../third_party/zoneinfo/src/data/zoneinfo64.res"} in bare["gaps"]


# ------------------------------------------------------ toolchain receipt ----

class FakeProbe(receipt_tool.Probe):
    def __init__(self, clang: str = "21.0.0", lld: str = "24.0.0"):
        self.clang, self.lld = clang, lld

    def out(self, args, cwd=None):
        name = os.path.basename(args[0])
        if name.startswith("clang"):
            return f"clang version {self.clang} (https://chromium.googlesource.com/a/llvm)\nTarget: arm64"
        if "lld" in name:
            return f"LLD {self.lld} (compatible with GNU linkers)"
        if args[:2] == ["xcodebuild", "-version"]:
            return "Xcode 16.4\nBuild version 16F6"
        if args[:1] == ["xcrun"]:
            return "15.5" if "--show-sdk-version" in args else "/Applications/Xcode.app/sdk"
        return "probe-output"

    def which(self, name):
        return None

    def exists(self, path):
        return os.path.isfile(path)

    def sha256(self, path):
        return hashlib.sha256(path.encode()).hexdigest()


def receipt_fixture(tmp: Path, library: bytes, probe: FakeProbe | None = None,
                    consumer: bytes | None = None) -> tuple[dict, Path]:
    out = tmp / "v8" / "out" / "mac-arm64"
    bin_dir = tmp / "v8" / "third_party" / "llvm-build" / "Release+Asserts" / "bin"
    out.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    for tool in ("clang", "clang++", "ld64.lld"):
        (bin_dir / tool).write_bytes(b"tool")
    prefix = "../../third_party/llvm-build/Release+Asserts/bin"
    (out / "toolchain.ninja").write_text(
        f"rule cc\n  command = ccache {prefix}/clang -c x\n"
        f"rule cxx\n  command = ccache {prefix}/clang++ -c x\n"
        f"rule solink\n  command = TOOL_VERSION=1 python3 ../../build/toolchain/apple/linker_driver.py "
        f"{prefix}/clang++ -shared\n")
    (out / "args.gn").write_text('target_cpu = "arm64"\ncc_wrapper = "ccache"\n')
    archive = write_zip(tmp / "v8-mac-arm64-x.zip", {
        "lib/libv8.dylib": library, "manifest.json": b'{"lib": "lib/libv8.dylib"}'})
    consumer_path = None
    if consumer is not None:
        consumer_path = tmp / "v8_skia_validate"
        consumer_path.write_bytes(consumer)
    receipt = receipt_tool.build_receipt(
        archive, "mac", "arm64", "mac-arm64", out, tmp / "v8", probe=probe or FakeProbe(),
        consumer=consumer_path, consumer_ran=consumer is not None,
        consumer_note="" if consumer is not None else "no consumer for this cell")
    return receipt, archive


def test_toolchain_receipt_proves_its_archive():
    with tempfile.TemporaryDirectory() as tmp:
        consumer = macho_dylib(needed=("@rpath/libv8.dylib", "/usr/lib/libSystem.B.dylib"))
        receipt, archive = receipt_fixture(Path(tmp), macho_dylib(), consumer=consumer)
        assert receipt_tool.validate_receipt(receipt, archive) == []
        assert receipt["compiler"]["cxx"]["path"].endswith("clang++")
        assert receipt["linker"]["lld"]["path"].endswith("ld64.lld")
        assert receipt["consumer_link"]["status"] == "linked-and-ran"
        assert receipt["consumer_link"]["references_library"] is True
        assert receipt["library"]["facts"]["build_version"]["platform"] == "macos"

        # Control: another archive.
        other = write_zip(Path(tmp) / "other.zip", {"lib/libv8.dylib": macho_dylib(minos=0x000C0000),
                                                    "manifest.json": b'{"lib": "lib/libv8.dylib"}'})
        expect_failure(receipt_tool.validate_receipt(receipt, other), "archive sha256")
        expect_failure(receipt_tool.validate_receipt(receipt, other), "library facts")


def test_toolchain_receipt_controls():
    with tempfile.TemporaryDirectory() as tmp:
        receipt, archive = receipt_fixture(Path(tmp) / "x86", macho_dylib(cputype=0x01000007))
        expect_failure(receipt_tool.validate_receipt(receipt, archive), "the cell is 'arm64'")
    with tempfile.TemporaryDirectory() as tmp:
        receipt, archive = receipt_fixture(Path(tmp), macho_dylib(lld="23.1.0"))
        expect_failure(receipt_tool.validate_receipt(receipt, archive), "linked by lld")
    with tempfile.TemporaryDirectory() as tmp:
        receipt, archive = receipt_fixture(Path(tmp), macho_dylib(platform=7))
        expect_failure(receipt_tool.validate_receipt(receipt, archive), "library platform")
    with tempfile.TemporaryDirectory() as tmp:
        receipt, archive = receipt_fixture(Path(tmp), macho_dylib(), probe=FakeProbe(clang="garbage"))
        receipt["compiler"]["cxx"]["version"] = "gcc version 12"
        expect_failure(receipt_tool.validate_receipt(receipt, archive), "not a clang banner")
    with tempfile.TemporaryDirectory() as tmp:
        receipt, archive = receipt_fixture(Path(tmp), macho_dylib())
        assert receipt["consumer_link"] == {"status": "none", "reason": "no consumer for this cell"}
        receipt["consumer_link"]["reason"] = " "
        expect_failure(receipt_tool.validate_receipt(receipt, archive), "without a reason")
    with tempfile.TemporaryDirectory() as tmp:
        unrelated = macho_dylib(needed=("/usr/lib/libSystem.B.dylib",))
        receipt, archive = receipt_fixture(Path(tmp), macho_dylib(), consumer=unrelated)
        expect_failure(receipt_tool.validate_receipt(receipt, archive), "does not load the library")


def test_linux_receipt_refuses_a_newer_glibc_than_the_producer():
    receipt = {"schema": receipt_tool.SCHEMA,
               "target": {"platform": "linux", "arch": "x64", "cell": "linux-x64"},
               "library": {"facts": binfacts.binary_facts(elf_so(versions=("GLIBC_2.39",)))},
               "platform_sdk": {"glibc": "2.35", "os_release": "Ubuntu 22.04"}}
    expect_failure(receipt_tool.validate_receipt(receipt), "newer than the producer's 2.35")
    receipt["library"]["facts"] = binfacts.binary_facts(elf_so(versions=("GLIBC_2.34",)))
    assert not any("newer than" in p for p in receipt_tool.validate_receipt(receipt))


def test_toolchain_program_parsing():
    assert receipt_tool.program_in("ccache ../../third_party/llvm-build/Release+Asserts/bin/clang++ -c") \
        == "../../third_party/llvm-build/Release+Asserts/bin/clang++"
    assert receipt_tool.program_in('"python3" "../../build/toolchain/gcc_solink_wrapper.py" '
                                   '--readelf=../../x/llvm-readelf -- ../../x/clang++ -shared') == "../../x/clang++"
    win = ('ninja -t msvc -e environment.x64 -- ..\\..\\third_party\\llvm-build\\Release+Asserts\\bin\\'
           'clang-cl.exe /c x.cc')
    assert receipt_tool.program_in(win).endswith("clang-cl.exe")


def test_windows_sdk_is_read_from_the_gn_environment_block():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        vs = r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise"
        kits = r"C:\Program Files (x86)\Windows Kits\10"
        block = {
            "INCLUDE": f"{vs}\\VC\\Tools\\MSVC\\14.44.35207\\include;{kits}\\include\\10.0.26100.0\\ucrt;",
            "LIB": f"{vs}\\VC\\Tools\\MSVC\\14.44.35207\\lib\\x64;{kits}\\lib\\10.0.26100.0\\um\\x64",
            "PATH": r"C:\Windows\system32",
        }
        (out / "environment.x64").write_text(
            "".join(f"{key}={value}\0" for key, value in block.items()) + "\0", encoding="utf-8")
        sdk = receipt_tool.windows_sdk(out)
        assert sdk == {"windows_sdk_version": "10.0.26100.0", "msvc_tools_version": "14.44.35207",
                       "vs_install_dir": vs}, sdk
        # Control: a block without SDK paths stays unknown, which --check rejects.
        (out / "environment.x64").write_text("PATH=C:\\Windows\0\0", encoding="utf-8")
        assert set(receipt_tool.windows_sdk(out).values()) == {"unknown"}


def test_android_ndk_revision_falls_back_to_deps():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "DEPS").write_text("vars = {\n  'android_ndk_version': Str('2@30.0.16248370'),\n}\n")
        assert receipt_tool.android_ndk(root)["ndk_revision"] == "2@30.0.16248370"
        props = root / "third_party" / "android_toolchain" / "ndk"
        props.mkdir(parents=True)
        (props / "source.properties").write_text("Pkg.Desc = Android NDK\nPkg.Revision = 30.0.16248370\n")
        assert receipt_tool.android_ndk(root)["ndk_revision"] == "30.0.16248370"
        # Control: neither record present.
        (props / "source.properties").unlink()
        (root / "DEPS").write_text("vars = {}\n")
        assert receipt_tool.android_ndk(root)["ndk_revision"] == "unknown"


def test_assemble_release_requires_both_records_per_archive():
    import assemble_release
    pair = {"pair_kind": "chromium-milestone", "milestone": 158, "v8": "38351a002e2c" + "0" * 28,
            "validated_skia_release": "chrome/m158"}
    with tempfile.TemporaryDirectory() as tmp:
        release = Path(tmp)
        rid = "m158-15.8.62.6-38351a002e2c"
        for cell in ("v8-mac-arm64", "v8-win-x64"):
            manifest = {"v8_version": "15.8.62.6", "platform": cell.split("-")[1], "arch": "x",
                        "consumer_defines": ["V8_COMPRESS_POINTERS"], "pair": pair}
            write_zip(release / f"{cell}-{rid}.zip", {"manifest.json": json.dumps(manifest).encode()})
            family = "apple" if "mac" in cell else "windows"
            (release / f"{cell}-{rid}.legal-manifest.sha256-{'a' * 64}.json").write_text("{}")
            (release / f"{cell}-{rid}.{family}-toolchain.sha256-{'b' * 64}.json").write_text("{}")
        meta = assemble_release.assemble(release, complete=False)
        assert meta["release_id"] == rid
        metadata = json.loads((release / "release-metadata.json").read_text())
        assert metadata["assets"] == [f"v8-mac-arm64-{rid}.zip", f"v8-win-x64-{rid}.zip"]
        assert metadata["provenance"][f"v8-win-x64-{rid}.zip"]["toolchain_receipt"].startswith("v8-win-x64")
        assert "gh attestation verify" in (release / "RELEASE_NOTES.md").read_text()
        try:
            assemble_release.assemble(release, complete=True)
        except assemble_release.AssemblyError as error:
            assert "incomplete" in str(error)
        else:
            raise AssertionError("an incomplete set was accepted with --complete")
        # Control: an archive without its receipt.
        next(release.glob("v8-win-x64-*.windows-toolchain.*.json")).unlink()
        try:
            assemble_release.assemble(release, complete=False)
        except assemble_release.AssemblyError as error:
            assert "has no toolchain_receipt" in str(error)
        else:
            raise AssertionError("an archive without a receipt was accepted")


def test_recut_inputs_come_from_the_base_release():
    import base64
    import recut_inputs
    pair = {"source": "chromium-milestone-branch-deps", "pair_kind": "chromium-milestone",
            "this_artifact": "v8", "built_revision": "3" * 40, "v8": "3" * 40, "milestone": 158,
            "validated_skia_release": "chrome/m158", "skia_release_tag": "chrome/m158"}
    metadata = {"manifests": [{"pair": pair}, {"pair": dict(pair)}]}
    values = recut_inputs.recut_inputs(metadata, "chrome/m158")
    assert values["v8_revision"] == "3" * 40 and values["skia_release_tag"] == "chrome/m158"
    lock = json.loads(base64.b64decode(values["lkgr_lock_b64"]))
    assert "built_revision" not in lock and lock["milestone"] == 158 and lock["v8"] == "3" * 40
    for bad, fragment in ((recut_inputs.recut_inputs, "validated against"),):
        try:
            bad(metadata, "chrome/m159")
        except ValueError as error:
            assert fragment in str(error)
        else:
            raise AssertionError("a different Skia release was accepted")
    mixed = {"manifests": [{"pair": pair}, {"pair": dict(pair, v8="4" * 40)}]}
    try:
        recut_inputs.recut_inputs(mixed)
    except ValueError as error:
        assert "different pairs" in str(error)
    else:
        raise AssertionError("mixed pairs were accepted")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {error!r}")
        else:
            print(f"ok   {test.__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
