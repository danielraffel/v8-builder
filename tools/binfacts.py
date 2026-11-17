#!/usr/bin/env python3
"""Read the load-time facts of a shared library from its bytes.

The toolchain receipt records, for the library a release archive ships, what
a consumer's loader will see: the machine it is for, the libraries it needs,
the minimum OS it was built for and, on ELF, the highest GLIBC_/GLIBCXX_
symbol version it references.  These are read from the bytes in the archive,
so ``--check`` can recompute them and refuse a receipt that describes some
other file.

Pure Python, no tools, so the same reading runs on every runner and in the
unit tests.  Mach-O (thin and fat), ELF64 little-endian and PE32+ are
supported; anything else is reported as ``unknown``.
"""
from __future__ import annotations

import re
import struct

ELF_MACHINES = {0x3E: "x64", 0xB7: "arm64", 0x03: "x86", 0x28: "arm"}
MACHO_CPUS = {0x01000007: "x86_64", 0x0100000C: "arm64", 0x0000000C: "arm", 0x00000007: "x86"}
MACHO_PLATFORMS = {1: "macos", 2: "ios", 3: "tvos", 4: "watchos", 6: "maccatalyst",
                   7: "ios-simulator", 8: "tvos-simulator", 9: "watchos-simulator",
                   11: "visionos", 12: "visionos-simulator"}
MACHO_TOOLS = {1: "clang", 2: "swift", 3: "ld", 4: "lld"}
PE_MACHINES = {0x8664: "x64", 0xAA64: "arm64", 0x014C: "x86", 0xA641: "arm64ec"}

LC_ID_DYLIB = 0x0D
LC_LOAD_DYLIB = 0x0C
LC_REEXPORT_DYLIB = 0x8000001F
LC_LOAD_WEAK_DYLIB = 0x80000018
LC_BUILD_VERSION = 0x32
LC_VERSION_MIN = {0x24: "macos", 0x25: "ios"}

VERSION_RE = re.compile(r"\b(GLIBCXX|GLIBC|CXXABI)_([0-9]+(?:\.[0-9]+)+)\b")


def _version(packed: int) -> str:
    return f"{packed >> 16}.{(packed >> 8) & 0xFF}.{packed & 0xFF}"


def _cstr(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    return data[offset:end if end >= 0 else len(data)].decode("utf-8", errors="replace")


# ---------------------------------------------------------------- Mach-O ----

def _macho_slice(data: bytes) -> dict[str, object]:
    magic = struct.unpack("<I", data[:4])[0]
    if magic != 0xFEEDFACF:
        return {"format": "unknown"}
    cputype, _sub, _filetype, ncmds, _size = struct.unpack("<iiiII", data[4:24])
    facts: dict[str, object] = {
        "format": "mach-o",
        "arch": MACHO_CPUS.get(cputype & 0xFFFFFFFF, f"cpu-{cputype:#x}"),
        "install_name": "",
        "needed": [],
        "build_version": None,
    }
    offset = 32
    needed: list[str] = []
    for _ in range(ncmds):
        if offset + 8 > len(data):
            break
        cmd, cmdsize = struct.unpack("<II", data[offset:offset + 8])
        if cmdsize < 8:
            break
        body = data[offset:offset + cmdsize]
        if cmd in (LC_ID_DYLIB, LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB):
            name_offset = struct.unpack("<I", body[8:12])[0]
            name = _cstr(body, name_offset)
            if cmd == LC_ID_DYLIB:
                facts["install_name"] = name
            else:
                needed.append(name)
        elif cmd == LC_BUILD_VERSION:
            platform_id, minos, sdk, ntools = struct.unpack("<IIII", body[8:24])
            tools = []
            for index in range(ntools):
                tool, version = struct.unpack("<II", body[24 + 8 * index:32 + 8 * index])
                tools.append({"tool": MACHO_TOOLS.get(tool, str(tool)), "version": _version(version)})
            facts["build_version"] = {
                "platform": MACHO_PLATFORMS.get(platform_id, str(platform_id)),
                "minos": _version(minos), "sdk": _version(sdk), "tools": tools}
        elif cmd in LC_VERSION_MIN and facts["build_version"] is None:
            minos, sdk = struct.unpack("<II", body[8:16])
            facts["build_version"] = {"platform": LC_VERSION_MIN[cmd], "minos": _version(minos),
                                      "sdk": _version(sdk), "tools": []}
        offset += cmdsize
    facts["needed"] = sorted(needed)
    return facts


def macho_facts(data: bytes) -> dict[str, object]:
    if data[:4] == b"\xca\xfe\xba\xbe":  # fat, big-endian header
        count = struct.unpack(">I", data[4:8])[0]
        slices = []
        for index in range(count):
            _cpu, _sub, offset, size, _align = struct.unpack(
                ">iiIII", data[8 + 20 * index:28 + 20 * index])
            slices.append(_macho_slice(data[offset:offset + size]))
        return {"format": "mach-o-fat", "slices": slices,
                "arch": ",".join(sorted(str(s.get("arch")) for s in slices))}
    return _macho_slice(data)


# ------------------------------------------------------------------- ELF ----

def elf_facts(data: bytes) -> dict[str, object]:
    if data[:4] != b"\x7fELF" or len(data) < 64 or data[4] != 2 or data[5] != 1:
        return {"format": "unknown"}
    machine = struct.unpack("<H", data[18:20])[0]
    shoff = struct.unpack("<Q", data[40:48])[0]
    shentsize, shnum, shstrndx = struct.unpack("<HHH", data[58:64])
    sections = []
    for index in range(shnum):
        base = shoff + index * shentsize
        if base + 64 > len(data):
            break
        name, kind, _flags, _addr, offset, size, link, _info, _align, entsize = struct.unpack(
            "<IIQQQQIIQQ", data[base:base + 64])
        sections.append({"name_off": name, "type": kind, "offset": offset, "size": size,
                         "link": link, "entsize": entsize})
    names = sections[shstrndx] if shstrndx < len(sections) else None

    def sname(section: dict) -> str:
        return _cstr(data, names["offset"] + section["name_off"]) if names else ""

    by_name = {sname(section): section for section in sections}
    facts: dict[str, object] = {"format": "elf", "arch": ELF_MACHINES.get(machine, f"machine-{machine}"),
                                "soname": "", "needed": [], "comments": [], "symbol_versions": {}}
    dynamic = by_name.get(".dynamic")
    if dynamic:
        strtab = sections[dynamic["link"]] if dynamic["link"] < len(sections) else None
        needed = []
        for entry in range(dynamic["size"] // 16):
            tag, value = struct.unpack("<qQ", data[dynamic["offset"] + 16 * entry:
                                                   dynamic["offset"] + 16 * entry + 16])
            if tag == 0:
                break
            if strtab and tag in (1, 14):  # DT_NEEDED, DT_SONAME
                text = _cstr(data, strtab["offset"] + value)
                if tag == 1:
                    needed.append(text)
                else:
                    facts["soname"] = text
        facts["needed"] = sorted(needed)
    comment = by_name.get(".comment")
    if comment:
        raw = data[comment["offset"]:comment["offset"] + comment["size"]]
        facts["comments"] = sorted({part.decode("utf-8", errors="replace")
                                    for part in raw.split(b"\0") if part})
    verneed = by_name.get(".gnu.version_r")
    if verneed:
        strtab = sections[verneed["link"]] if verneed["link"] < len(sections) else None
        versions: list[str] = []
        offset = verneed["offset"]
        while strtab:
            _ver, count, _file, aux, nxt = struct.unpack("<HHIII", data[offset:offset + 16])
            aux_offset = offset + aux
            for _ in range(count):
                _hash, _flags, _other, name, aux_next = struct.unpack(
                    "<IHHII", data[aux_offset:aux_offset + 16])
                versions.append(_cstr(data, strtab["offset"] + name))
                if not aux_next:
                    break
                aux_offset += aux_next
            if not nxt:
                break
            offset += nxt
        facts["symbol_versions"] = max_symbol_versions(" ".join(versions))
    return facts


def max_symbol_versions(text: str) -> dict[str, str]:
    """The highest GLIBC_/GLIBCXX_/CXXABI_ version named in ``text``."""
    found: dict[str, tuple[int, ...]] = {}
    for family, version in VERSION_RE.findall(text):
        key = tuple(int(part) for part in version.split("."))
        if family not in found or key > found[family]:
            found[family] = key
    return {f"max_{family.lower()}": ".".join(map(str, key)) for family, key in sorted(found.items())}


# -------------------------------------------------------------------- PE ----

def pe_facts(data: bytes) -> dict[str, object]:
    if data[:2] != b"MZ" or len(data) < 0x40:
        return {"format": "unknown"}
    pe = struct.unpack("<I", data[0x3C:0x40])[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return {"format": "unknown"}
    machine, nsections, _stamp, _symptr, _nsyms, optsize, _chars = struct.unpack(
        "<HHIIIHH", data[pe + 4:pe + 24])
    opt = pe + 24
    magic = struct.unpack("<H", data[opt:opt + 2])[0]
    linker = f"{data[opt + 2]}.{data[opt + 3]}"
    facts: dict[str, object] = {"format": "pe", "arch": PE_MACHINES.get(machine, f"machine-{machine:#x}"),
                                "linker_version": linker, "needed": []}
    if magic != 0x20B:  # PE32+ only
        return facts
    major_os, minor_os = struct.unpack("<HH", data[opt + 40:opt + 44])
    major_sub, minor_sub = struct.unpack("<HH", data[opt + 48:opt + 52])
    facts["min_os"] = f"{major_os}.{minor_os}"
    facts["subsystem_version"] = f"{major_sub}.{minor_sub}"
    import_rva, _import_size = struct.unpack("<II", data[opt + 112 + 8:opt + 112 + 16])
    table = opt + optsize
    sections = []
    for index in range(nsections):
        base = table + 40 * index
        vsize, vaddr, rawsize, rawptr = struct.unpack("<IIII", data[base + 8:base + 24])
        sections.append((vaddr, max(vsize, rawsize), rawptr))

    def offset_of(rva: int) -> int | None:
        for vaddr, size, rawptr in sections:
            if vaddr <= rva < vaddr + size:
                return rawptr + rva - vaddr
        return None

    needed = []
    cursor = offset_of(import_rva) if import_rva else None
    while cursor is not None and cursor + 20 <= len(data):
        _lookup, _stamp2, _chain, name_rva, thunk = struct.unpack("<IIIII", data[cursor:cursor + 20])
        if not name_rva and not thunk:
            break
        name_offset = offset_of(name_rva)
        if name_offset is not None:
            needed.append(_cstr(data, name_offset))
        cursor += 20
    facts["needed"] = sorted(needed, key=str.lower)
    return facts


def binary_facts(data: bytes) -> dict[str, object]:
    if data[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return macho_facts(data)
    if data[:4] == b"\x7fELF":
        return elf_facts(data)
    if data[:2] == b"MZ":
        return pe_facts(data)
    return {"format": "unknown"}
