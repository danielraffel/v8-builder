#!/usr/bin/env python3
"""Write, and check, the toolchain receipt for one V8 cell.

The shipped library records almost nothing about the toolchain that built
it.  This receipt records the producer from the runner that built the
archive, and the load-time facts of the library itself, so a consumer can
tell whether its own toolchain and target OS are compatible:

* the ``cc``/``cxx`` and shared-library link commands GN wrote into the
  cell's ``toolchain.ninja``, and the compiler and linker each resolves to:
  path, SHA-256 and ``--version`` banner;
* the post-link tools (``strip``, and ``codesign`` on Apple) that rewrote
  the library before it was packaged;
* the platform SDK: Xcode and the SDK on Apple, the NDK on Android, the
  Windows SDK and MSVC tools on Windows, glibc and libstdc++ on Linux;
* the GN ``args.gn`` the cell was built with;
* the library's own facts, read from the archive (``binfacts.py``): machine,
  needed libraries, minimum OS, and on ELF the highest GLIBC_/GLIBCXX_
  version it references;
* the consumer link: the validator that linked against this library in the
  same job, when the cell has one, and whether it ran.

``--check`` fails when a field is empty or unknown, when the archive or the
library member is not the one the receipt names, when the library is for
another architecture or OS, when a Mach-O library was linked by a different
lld than the receipt names, or when an ELF library needs a newer glibc than
the producer had.  A receipt that does not match its archive is worse than
none.

Published as ``<archive stem>.<family>-toolchain.sha256-<digest>.json`` with
family ``apple``, ``linux``, ``android`` or ``windows``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as host_platform
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import binfacts  # noqa: E402

SCHEMA = "v8-builder.toolchain-receipt.v1"
FAMILY = {"mac": "apple", "ios": "apple", "linux": "linux", "android": "android", "win": "windows"}
CONTENT_ADDRESSED_RE = re.compile(r"\.(apple|linux|android|windows)-toolchain\.sha256-([0-9a-f]{64})\.json$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
BAD_VALUE_RE = re.compile(r"^(unknown|error.*|)$", re.IGNORECASE)
CLANG_BANNER_RE = re.compile(r"clang version (\d+\.\d+\.\d+)")
LLD_BANNER_RE = re.compile(r"\bLLD (\d+\.\d+\.\d+)")
TOOL_NAME_RE = re.compile(
    r"^(clang|clang\+\+|clang-cl|lld-link|ld\.lld|ld64\.lld|lld|gcc|g\+\+|cc|c\+\+)(-\d+)?(\.exe)?$",
    re.IGNORECASE)
ARCH_ALIASES = {"x64": "x64", "x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}
APPLE_PLATFORM = {("mac", ""): "macos", ("ios", "simulator"): "ios-simulator", ("ios", "device"): "ios"}
CONSUMER_STATUSES = {"linked", "linked-and-ran", "none"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def content_addressed_name(archive_name: str, family: str, data: bytes) -> str:
    stem = archive_name[:-4] if archive_name.endswith(".zip") else archive_name
    return f"{stem}.{family}-toolchain.sha256-{hashlib.sha256(data).hexdigest()}.json"


class Probe:
    """Runs host tools; replaced in tests."""

    def out(self, args: list[str], cwd: Path | None = None) -> str:
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                    errors="replace", timeout=120)
        except (OSError, subprocess.SubprocessError) as error:
            return f"error: {error}"
        return (result.stdout + result.stderr).strip()

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def exists(self, path: str) -> bool:
        return os.path.isfile(path)

    def sha256(self, path: str) -> str:
        try:
            return sha256_file(Path(path))
        except OSError as error:
            return f"error: {error}"


# ---------------------------------------------------------- toolchain.ninja ----

def toolchain_rules(text: str) -> dict[str, str]:
    """``rule <name>`` -> its ``command``, for one GN toolchain.ninja."""
    rules: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("rule "):
            current = line[5:].strip()
        elif current and line.strip().startswith("command = "):
            rules[current] = line.strip()[len("command = "):]
            current = None
    return rules


def program_in(command: str) -> str:
    """The compiler or linker a rule command runs, past wrappers (ccache, ninja -t msvc, python)."""
    try:
        tokens = shlex.split(command, posix=os.name != "nt" and "\\" not in command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        token = token.strip('"')
        if TOOL_NAME_RE.match(PurePosixPath(token.replace("\\", "/")).name):
            return token
    return ""


def tool_row(probe: Probe, program: str, out_dir: Path, version_args: list[str]) -> dict[str, str]:
    row = {"command": program or "unknown", "path": "unknown", "sha256": "unknown", "version": "unknown"}
    if not program:
        return row
    candidate = program if os.path.isabs(program) else os.path.normpath(os.path.join(out_dir, program))
    if not probe.exists(candidate):
        for suffix in ("", ".exe"):
            found = probe.which(program + suffix)
            if found:
                candidate = found
                break
    if probe.exists(candidate):
        row["path"] = os.path.realpath(candidate)
        row["sha256"] = probe.sha256(row["path"])
        banner = probe.out([row["path"], *version_args])
        row["version"] = banner.splitlines()[0] if banner else "unknown"
    return row


def linker_binary(link: dict[str, str], compiler_dir: str, platform: str) -> str:
    """The lld binary behind the link driver (clang++ -fuse-ld=lld, or lld-link itself)."""
    name = PurePosixPath(link.get("path", "").replace("\\", "/")).name.lower()
    if "lld" in name:
        return link["path"]
    candidates = {"mac": ["ld64.lld"], "ios": ["ld64.lld"], "win": ["lld-link.exe", "lld-link"]}.get(
        platform, ["ld.lld"])
    for candidate in candidates:
        path = os.path.join(compiler_dir, candidate)
        if os.path.isfile(path):
            return path
    return ""


# ---------------------------------------------------------------- receipt ----

def library_member(archive: Path, manifest_lib: str) -> tuple[str, bytes]:
    with zipfile.ZipFile(archive) as handle:
        names = set(handle.namelist())
        for candidate in (manifest_lib, f"{manifest_lib}/V8", manifest_lib.rstrip("/") + "/V8"):
            if candidate in names and not candidate.endswith("/"):
                return candidate, handle.read(candidate)
    raise SystemExit(f"toolchain-receipt: {archive.name} has no member {manifest_lib}")


def archive_manifest(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as handle:
        return json.loads(handle.read("manifest.json"))


ANDROID_NDK_DEPS_RE = re.compile(r"""['"]android_ndk_version['"]\s*:\s*(?:Str\()?\s*['"]([^'"]+)['"]""")


def android_ndk(source_root: Path) -> dict[str, object]:
    """The NDK revision: source.properties when the checkout has one, else DEPS.

    The DEPS-fetched cipd android_toolchain is stripped and ships no
    source.properties, so the revision is the one the checkout's DEPS pinned.
    """
    props = source_root / "third_party" / "android_toolchain" / "ndk" / "source.properties"
    if props.is_file():
        for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Pkg.Revision"):
                return {"ndk_revision": line.split("=", 1)[1].strip(), "ndk_revision_source": str(props)}
    deps = source_root / "DEPS"
    if deps.is_file():
        match = ANDROID_NDK_DEPS_RE.search(deps.read_text(encoding="utf-8", errors="replace"))
        if match:
            return {"ndk_revision": match.group(1),
                    "ndk_revision_source": f"{deps} android_ndk_version (the cipd android_toolchain version)"}
    return {"ndk_revision": "unknown", "ndk_revision_source": str(props)}


WIN_SDK_RE = re.compile(r"[\\/]Windows Kits[\\/]\d+[\\/](?:Include|Lib)[\\/](\d+(?:\.\d+){3})(?:[\\/]|$)",
                        re.IGNORECASE)
MSVC_RE = re.compile(r"^(.*?)[\\/]VC[\\/]Tools[\\/]MSVC[\\/](\d+(?:\.\d+)+)(?:[\\/]|$)", re.IGNORECASE)


def windows_sdk(out_dir: Path) -> dict[str, object]:
    """Windows SDK, MSVC tools and VS install dir the build compiled against.

    GN's setup_toolchain.py writes out/<cell>/environment.<cpu> as a NUL
    separated block, but keeps only INCLUDE, LIB, LIBPATH, PATH and a few
    others, so the versions are read from the paths in those.
    """
    env: dict[str, str] = {}
    for path in sorted(out_dir.glob("environment.*")):
        for item in path.read_bytes().split(b"\0"):
            key, _, value = item.decode("utf-8", errors="replace").partition("=")
            key = key.upper()
            if key in ("WINDOWSSDKVERSION", "VCTOOLSVERSION", "VSINSTALLDIR"):
                env.setdefault(key, value.rstrip("\\/"))
            elif key in ("INCLUDE", "LIB", "LIBPATH"):
                env[key] = env.get(key, "") + ";" + value
        if env:
            break
    for entry in ";".join(env.get(key, "") for key in ("INCLUDE", "LIB", "LIBPATH")).split(";"):
        entry = entry.strip().strip('"')
        sdk = WIN_SDK_RE.search(entry)
        if sdk:
            env.setdefault("WINDOWSSDKVERSION", sdk.group(1))
        msvc = MSVC_RE.search(entry)
        if msvc:
            env.setdefault("VCTOOLSVERSION", msvc.group(2))
            env.setdefault("VSINSTALLDIR", msvc.group(1))
    return {
        "windows_sdk_version": env.get("WINDOWSSDKVERSION", "unknown"),
        "msvc_tools_version": env.get("VCTOOLSVERSION", "unknown"),
        "vs_install_dir": env.get("VSINSTALLDIR", "unknown"),
    }


def platform_sdk(platform: str, cell: str, out_dir: Path, source_root: Path, probe: Probe) -> dict[str, object]:
    if platform in ("mac", "ios"):
        sdk = "macosx" if platform == "mac" else ("iphonesimulator" if "simulator" in cell else "iphoneos")
        return {
            "xcode": probe.out(["xcodebuild", "-version"]).replace("\n", " "),
            "sdk": sdk,
            "sdk_version": probe.out(["xcrun", "--sdk", sdk, "--show-sdk-version"]),
            "sdk_path": probe.out(["xcrun", "--sdk", sdk, "--show-sdk-path"]),
        }
    if platform == "android":
        return android_ndk(source_root)
    if platform == "win":
        return windows_sdk(out_dir)
    # linux
    glibc = probe.out(["ldd", "--version"]).splitlines()
    match = re.search(r"(\d+\.\d+)\s*$", glibc[0]) if glibc else None
    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            os_release[key] = value.strip('"')
    except OSError:
        pass
    return {"glibc": match.group(1) if match else "unknown",
            "os_release": os_release.get("PRETTY_NAME", "unknown")}


def post_link_tools(platform: str, source_root: Path, probe: Probe) -> dict[str, object]:
    if platform in ("mac", "ios"):
        strip = probe.out(["xcrun", "--find", "strip"])
        return {"strip": {"path": strip, "version": probe.out(["xcrun", "strip", "-V"]).splitlines()[0]
                          if probe.out(["xcrun", "strip", "-V"]) else "unknown",
                          "args": "-x"},
                "codesign": {"identity": "ad-hoc (-s -)", "path": probe.which("codesign") or "unknown"}}
    if platform in ("linux", "android"):
        bundled = source_root / "third_party" / "llvm-build" / "Release+Asserts" / "bin" / "llvm-strip"
        path = str(bundled) if bundled.is_file() else (probe.which("llvm-strip") or "unknown")
        return {"strip": {"path": path, "sha256": probe.sha256(path) if path != "unknown" else "unknown",
                          "version": probe.out([path, "--version"]).splitlines()[0]
                          if path != "unknown" else "unknown", "args": "strip-all"}}
    return {"strip": {"path": "none", "version": "none", "args": "not stripped (debug info is in the unshipped PDB)"}}


def consumer_row(consumer: Path | None, ran: bool, note: str, library_member: str,
                 library_sha256: str, linked_library: Path | None) -> dict[str, object]:
    if consumer is None:
        return {"status": "none", "reason": note}
    data = consumer.read_bytes()
    facts = binfacts.binary_facts(data)
    needed = [str(item) for item in facts.get("needed", [])]
    parts = PurePosixPath(library_member).parts
    # A framework binary is loaded as <Name>.framework/<Name>, anything else by its file name.
    framework = len(parts) >= 2 and parts[-2] == parts[-1] + ".framework"
    wanted = ("/".join(parts[-2:]) if framework else parts[-1]).lower()
    row: dict[str, object] = {
        "status": "linked-and-ran" if ran else "linked",
        "binary": consumer.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "arch": facts.get("arch", "unknown"),
        "needed": needed,
        "references_library": any(item.lower().endswith(wanted) for item in needed),
        "note": note,
    }
    if linked_library is not None and linked_library.is_file():
        linked = sha256_file(linked_library)
        row["linked_library_sha256"] = linked
        row["linked_the_packaged_bytes"] = linked == library_sha256
    return row


def build_receipt(archive: Path, platform: str, arch: str, cell: str, out_dir: Path, source_root: Path,
                  probe: Probe | None = None, container_image: str = "", consumer: Path | None = None,
                  consumer_ran: bool = False, consumer_note: str = "",
                  consumer_library: Path | None = None) -> dict[str, object]:
    probe = probe or Probe()
    toolchain_ninja = out_dir / "toolchain.ninja"
    rules = toolchain_rules(toolchain_ninja.read_text(encoding="utf-8", errors="replace")) \
        if toolchain_ninja.is_file() else {}
    cc = tool_row(probe, program_in(rules.get("cc", "")), out_dir, ["--version"])
    cxx = tool_row(probe, program_in(rules.get("cxx", "")), out_dir, ["--version"])
    link_driver = tool_row(probe, program_in(rules.get("solink", "")), out_dir, ["--version"])
    compiler_dir = os.path.dirname(cxx["path"]) if cxx["path"] != "unknown" else ""
    lld_path = linker_binary(link_driver, compiler_dir, platform)
    lld = {"path": lld_path or "unknown", "sha256": probe.sha256(lld_path) if lld_path else "unknown",
           "version": (probe.out([lld_path, "--version"]).splitlines() or ["unknown"])[0]
           if lld_path else "unknown"}
    args_gn = out_dir / "args.gn"
    args_text = args_gn.read_text(encoding="utf-8") if args_gn.is_file() else ""
    manifest = archive_manifest(archive)
    member, data = library_member(archive, str(manifest.get("lib", "")))
    member_sha = hashlib.sha256(data).hexdigest()
    receipt: dict[str, object] = {
        "schema": SCHEMA,
        "target": {"platform": platform, "arch": arch, "cell": cell},
        "archive": {"name": archive.name, "sha256": sha256_file(archive)},
        "library": {"member": member, "sha256": member_sha, "facts": binfacts.binary_facts(data)},
        "runner": {
            "os": host_platform.platform(),
            "runner_arch": os.environ.get("RUNNER_ARCH", host_platform.machine()),
            "image": os.environ.get("ImageOS", "local"),
            "image_version": os.environ.get("ImageVersion", "local"),
            "container_image": container_image or "none",
        },
        "gn": {"args_gn": args_text, "args_gn_sha256": hashlib.sha256(args_text.encode()).hexdigest()},
        "compiler": {"c": cc, "cxx": cxx, "rules": {key: rules[key] for key in ("cc", "cxx")
                                                    if key in rules}},
        "linker": {"driver": link_driver, "lld": lld,
                   "rule": rules.get("solink", "")},
        "post_link": post_link_tools(platform, source_root, probe),
        "platform_sdk": platform_sdk(platform, cell, out_dir, source_root, probe),
        "ccache": {"version": (probe.out(["ccache", "--version"]).splitlines() or ["none"])[0]
                   if probe.which("ccache") else "none",
                   "cc_wrapper": "ccache" if "cc_wrapper" in args_text else "none"},
        "consumer_link": consumer_row(consumer, consumer_ran, consumer_note, member,
                                      member_sha, consumer_library),
    }
    return receipt


def validate_receipt(receipt: dict, archive: Path | None = None) -> list[str]:
    problems: list[str] = []

    def walk(value: object, where: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{where}.{key}" if where else key)
        elif isinstance(value, str):
            if BAD_VALUE_RE.match(value.strip()) or value.startswith("error"):
                problems.append(f"{where} = {value!r}")
        elif value is None:
            problems.append(f"{where} is null")

    walk({key: receipt.get(key) for key in ("target", "runner", "compiler", "platform_sdk")}, "")
    walk({"linker": {"lld": (receipt.get("linker") or {}).get("lld")}}, "")
    if receipt.get("schema") != SCHEMA:
        problems.append(f"schema = {receipt.get('schema')!r}")
    target = receipt.get("target") or {}
    platform, arch, cell = target.get("platform", ""), target.get("arch", ""), target.get("cell", "")
    compiler = receipt.get("compiler") or {}
    versions = set()
    for side in ("c", "cxx"):
        row = compiler.get(side) or {}
        banner = CLANG_BANNER_RE.search(str(row.get("version", "")))
        if not banner:
            problems.append(f"compiler.{side}.version {row.get('version')!r} is not a clang banner")
        else:
            versions.add(banner.group(1))
        if not HEX_64.match(str(row.get("sha256", ""))):
            problems.append(f"compiler.{side}.sha256 is not a SHA-256")
    if len(versions) > 1:
        problems.append(f"cc and cxx are different clang versions: {sorted(versions)}")
    lld = (receipt.get("linker") or {}).get("lld") or {}
    lld_banner = LLD_BANNER_RE.search(str(lld.get("version", "")))
    if not lld_banner:
        problems.append(f"linker.lld.version {lld.get('version')!r} is not an LLD banner")
    if not HEX_64.match(str(lld.get("sha256", ""))):
        problems.append("linker.lld.sha256 is not a SHA-256")
    if not (receipt.get("gn") or {}).get("args_gn", "").strip():
        problems.append("gn.args_gn is empty")
    library = receipt.get("library") or {}
    facts = library.get("facts") or {}
    if ARCH_ALIASES.get(str(facts.get("arch")), facts.get("arch")) != ARCH_ALIASES.get(arch, arch):
        problems.append(f"library is {facts.get('arch')!r}, the cell is {arch!r}")
    if platform in ("mac", "ios"):
        build = facts.get("build_version") or {}
        env = "simulator" if "simulator" in cell else ("device" if platform == "ios" else "")
        wanted = APPLE_PLATFORM.get((platform, env))
        if build.get("platform") != wanted:
            problems.append(f"library platform {build.get('platform')!r}, the cell is {wanted!r}")
        if not build.get("minos"):
            problems.append("library records no minimum OS")
        linked_by = {row.get("version") for row in build.get("tools", []) if row.get("tool") == "lld"}
        if lld_banner and linked_by and linked_by != {lld_banner.group(1)}:
            problems.append(f"library was linked by lld {sorted(linked_by)}, "
                            f"receipt names {lld_banner.group(1)}")
    elif facts.get("format") != {"linux": "elf", "android": "elf", "win": "pe"}.get(platform):
        problems.append(f"library format {facts.get('format')!r} is wrong for {platform}")
    if platform == "linux":
        needed = str((facts.get("symbol_versions") or {}).get("max_glibc", ""))
        host = str((receipt.get("platform_sdk") or {}).get("glibc", ""))
        if not re.fullmatch(r"\d+(\.\d+)+", needed):
            problems.append("library references no GLIBC_ version")
        elif re.fullmatch(r"\d+\.\d+", host) and \
                tuple(map(int, needed.split("."))) > tuple(map(int, host.split("."))):
            problems.append(f"library needs glibc {needed}, newer than the producer's {host}")
    consumer = receipt.get("consumer_link") or {}
    status = consumer.get("status")
    if status not in CONSUMER_STATUSES:
        problems.append(f"consumer_link.status = {status!r}")
    elif status == "none":
        if not str(consumer.get("reason", "")).strip():
            problems.append("consumer_link is none without a reason")
    else:
        if not HEX_64.match(str(consumer.get("sha256", ""))):
            problems.append("consumer_link.sha256 is not a SHA-256")
        if consumer.get("references_library") is not True:
            problems.append("the consumer does not load the library")
        if ARCH_ALIASES.get(str(consumer.get("arch")), consumer.get("arch")) != ARCH_ALIASES.get(arch, arch):
            problems.append(f"the consumer is {consumer.get('arch')!r}, the cell is {arch!r}")
    if archive is not None:
        if (receipt.get("archive") or {}).get("sha256") != sha256_file(archive):
            problems.append("receipt archive sha256 does not match the archive")
        try:
            with zipfile.ZipFile(archive) as handle:
                data = handle.read(str(library.get("member", "")))
        except KeyError:
            problems.append(f"the archive has no member {library.get('member')!r}")
        else:
            if hashlib.sha256(data).hexdigest() != library.get("sha256"):
                problems.append("library sha256 does not match the archive member")
            if binfacts.binary_facts(data) != facts:
                problems.append("library facts do not match the archive member")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--platform", choices=sorted(FAMILY))
    parser.add_argument("--arch", default="")
    parser.add_argument("--cell", default="")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--container-image", default="")
    parser.add_argument("--consumer", type=Path, help="a validator that linked against this library")
    parser.add_argument("--consumer-library", type=Path, help="the library file the consumer linked")
    parser.add_argument("--consumer-ran", action="store_true", help="the consumer ran and passed")
    parser.add_argument("--consumer-note", default="")
    parser.add_argument("--content-addressed-dir", type=Path)
    parser.add_argument("--check", action="store_true",
                        help="validate an existing receipt against --archive instead of writing one")
    args = parser.parse_args(argv)

    if args.check:
        receipt = json.loads(args.output.read_text(encoding="utf-8"))
        problems = validate_receipt(receipt, args.archive)
        match = CONTENT_ADDRESSED_RE.search(args.output.name)
        if match and match.group(2) != sha256_file(args.output):
            problems.append(f"{args.output.name} does not carry its own sha256")
        if match and match.group(1) != FAMILY.get((receipt.get("target") or {}).get("platform", "")):
            problems.append(f"{args.output.name} names the wrong toolchain family")
        for problem in problems:
            print(f"toolchain-receipt: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"toolchain-receipt: {args.output} is proven")
        return 0

    if not (args.platform and args.arch and args.cell and args.out_dir and args.source_root):
        parser.error("--platform, --arch, --cell, --out-dir and --source-root are required")
    if args.consumer is None and not args.consumer_note:
        parser.error("without --consumer, --consumer-note must say why no consumer linked")
    receipt = build_receipt(args.archive, args.platform, args.arch, args.cell, args.out_dir,
                            args.source_root, container_image=args.container_image,
                            consumer=args.consumer, consumer_ran=args.consumer_ran,
                            consumer_note=args.consumer_note, consumer_library=args.consumer_library)
    data = canonical_bytes(receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    problems = validate_receipt(receipt, args.archive)
    for problem in problems:
        print(f"toolchain-receipt: {problem}", file=sys.stderr)
    if args.content_addressed_dir and not problems:
        args.content_addressed_dir.mkdir(parents=True, exist_ok=True)
        target = args.content_addressed_dir / content_addressed_name(
            args.archive.name, FAMILY[args.platform], data)
        target.write_bytes(data)
        print(f"toolchain-receipt: content-addressed copy {target}")
    summary = {key: receipt[key] for key in ("compiler", "platform_sdk", "consumer_link")}
    summary["compiler"] = {side: summary["compiler"][side] for side in ("c", "cxx")}
    print(json.dumps(summary, indent=1)[:4000])
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
