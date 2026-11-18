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
  the library before it was packaged, each with its path, SHA-256 and
  version;
* the platform SDK: Xcode and the SDK on Apple, the NDK on Android, the
  Windows SDK and MSVC tools on Windows, glibc and libstdc++ on Linux;
* the GN ``args.gn`` the cell was built with;
* the library's own facts, read from the archive (``binfacts.py``): machine,
  needed libraries, minimum OS, and on ELF the highest GLIBC_/GLIBCXX_
  version it references;
* the consumer link: the validator that linked against this library in the
  same job, when the cell has one; whether it ran (only when the file its
  passing run wrote names this binary); and ``link_proof``: the consumer's
  loader entry for the library, resolved through the consumer's own run-path
  (or the library digest its passing run recorded), and whether those are the
  packaged bytes (``linked_packaged_bytes``) or cannot be shown to be
  (``linked_unverified``, with the reason);
* ``runtime_identity``: ``ran`` only when that consumer ran and loaded the
  packaged bytes, else ``not_run`` with the reason.  A release labels a
  ``not_run`` cell runtime-unverified.

``--check`` fails when a field is empty or unknown, when the consumer link
claims the packaged bytes but its loader entry, search path or digest says
otherwise, when ``runtime_identity`` claims more than the consumer link
shows, when the archive or the library member is not the one the receipt
names, when the library is for another architecture or OS, when a Mach-O
library was linked by a different lld than the receipt names, or when an ELF
library needs a newer glibc than the producer had.  A receipt that does not match its archive is worse than
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
import posixpath
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


CODESIGN_PROJECT_RE = re.compile(r"PROJECT:(codesign-[0-9][0-9.]*)")
POST_LINK_TOOLS = {"mac": ("strip", "codesign"), "ios": ("strip", "codesign"),
                   "linux": ("strip",), "android": ("strip",), "win": ("strip",)}


def version_line(text: str) -> str:
    """The line of a --version banner that carries the version number."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if re.search(r"version \d", line, re.IGNORECASE):
            return line
    return lines[0] if lines else "unknown"


def post_link_tools(platform: str, source_root: Path, probe: Probe) -> dict[str, object]:
    """The tools that rewrote the library after the link, each with its path,
    sha256 and version, so --check can require evidence of every one."""
    if platform in ("mac", "ios"):
        strip = probe.out(["xcrun", "--find", "strip"]).strip() or "unknown"
        xcode = probe.out(["xcodebuild", "-version"]).replace("\n", " ").strip() or "unknown"
        codesign = probe.which("codesign") or "unknown"
        project = CODESIGN_PROJECT_RE.search(probe.out(["what", codesign])) if codesign != "unknown" else None
        return {"strip": {"path": strip, "sha256": probe.sha256(strip) if strip != "unknown" else "unknown",
                          # cctools strip has no version option; it is the one this Xcode ships.
                          "version": xcode, "version_source": "xcodebuild -version (the Xcode that ships strip)",
                          "args": "-x"},
                "codesign": {"path": codesign,
                             "sha256": probe.sha256(codesign) if codesign != "unknown" else "unknown",
                             "version": project.group(1) if project else "unknown",
                             "version_source": "what(1) PROJECT tag",
                             "identity": "ad-hoc (-s -)"}}
    if platform in ("linux", "android"):
        bundled = source_root / "third_party" / "llvm-build" / "Release+Asserts" / "bin" / "llvm-strip"
        path = str(bundled) if bundled.is_file() else (probe.which("llvm-strip") or "unknown")
        return {"strip": {"path": path, "sha256": probe.sha256(path) if path != "unknown" else "unknown",
                          "version": version_line(probe.out([path, "--version"]))
                          if path != "unknown" else "unknown", "args": "strip-all"}}
    return {"strip": {"path": "none", "version": "none", "args": "not stripped (debug info is in the unshipped PDB)"}}


def post_link_problems(platform: str, post_link: object) -> list[str]:
    """Every post-link tool the platform runs must be recorded with usable evidence."""
    problems: list[str] = []
    rows = post_link if isinstance(post_link, dict) else {}
    for tool in POST_LINK_TOOLS.get(platform, ()):
        row = rows.get(tool)
        if not isinstance(row, dict):
            problems.append(f"post_link.{tool} is not recorded")
            continue
        for key, value in row.items():
            text = str(value).strip()
            if value is None or BAD_VALUE_RE.match(text) or text.lower().startswith("error"):
                problems.append(f"post_link.{tool}.{key} = {value!r}")
        if platform == "win" and tool == "strip" and row.get("path") == "none":
            if not str(row.get("args", "")).strip():
                problems.append("post_link.strip is none without saying why")
            continue
        if not HEX_64.match(str(row.get("sha256", ""))):
            problems.append(f"post_link.{tool}.sha256 is not a SHA-256")
        if str(row.get("path", "")).strip() in ("", "none", "unknown"):
            problems.append(f"post_link.{tool}.path = {row.get('path')!r}")
    return problems


LINK_PROOFS = {"linked_packaged_bytes", "linked_unverified"}
RUNTIME_STATUSES = {"ran", "not_run"}


def read_ran_evidence(path: Path | None) -> tuple[str, str]:
    """``(consumer sha256, library sha256)`` from a run's evidence file.

    The run writes the file only after it passed: the consumer's sha256 on the
    first line and, when the run loaded the library by a path the consumer's
    own run-path does not name (``LD_LIBRARY_PATH`` on an Android device), a
    second line ``library <sha256>`` for the library file it loaded.
    """
    if path is None or not path.is_file():
        return "", ""
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    consumer = lines[0] if lines else ""
    library = ""
    for line in lines[1:]:
        key, _, value = line.partition(" ")
        if key == "library":
            library = value.strip()
    return consumer, library


def library_identity(library_member: str, facts: dict) -> str:
    """The name a consumer's loader entry carries for this library: the Mach-O
    install name, the ELF soname, or the PE file name."""
    if facts.get("format") == "mach-o-fat":
        slices = facts.get("slices") or [{}]
        return str(slices[0].get("install_name", ""))
    if facts.get("format") == "mach-o":
        return str(facts.get("install_name", ""))
    if facts.get("format") == "elf":
        return str(facts.get("soname", ""))
    return PurePosixPath(library_member).name


def entry_names_library(entry: str, identity: str, fmt: str) -> bool:
    if not entry or not identity:
        return False
    if fmt == "pe":
        return PurePosixPath(entry.replace("\\", "/")).name.lower() == identity.lower()
    return entry == identity


def substitute_origin(directory: str, consumer_dir: str) -> str:
    for token in ("@loader_path", "@executable_path", "${ORIGIN}", "$ORIGIN"):
        directory = directory.replace(token, consumer_dir)
    return os.path.normpath(directory)


def search_dirs(fmt: str, entry: str, paths: dict, consumer_dir: str) -> tuple[list[str], str]:
    """The directories the loader searches for ``entry``, in its order, and the
    path inside each, from the consumer's own load commands only."""
    if fmt.startswith("mach-o"):
        if entry.startswith("@rpath/"):
            return [substitute_origin(item, consumer_dir) for item in paths.get("rpath", [])], entry[7:]
        if entry.startswith(("@loader_path/", "@executable_path/")):
            head, _, rest = entry.partition("/")
            return [substitute_origin(head, consumer_dir)], rest
        return [os.path.dirname(entry)], os.path.basename(entry)
    if fmt == "elf":
        if "/" in entry:
            return [substitute_origin(os.path.dirname(entry), consumer_dir)], os.path.basename(entry)
        # DT_RUNPATH replaces DT_RPATH when both are present.
        dirs = paths.get("runpath") or paths.get("rpath") or []
        return [substitute_origin(item, consumer_dir) for item in dirs], entry
    if fmt == "pe":
        # The application directory is searched first for a DLL that is not a known DLL.
        return [consumer_dir], PurePosixPath(entry.replace("\\", "/")).name
    return [], entry


def consumer_row(consumer: Path | None, ran: bool, note: str, library_member: str,
                 library_sha256: str, library_facts: dict | None = None,
                 ran_evidence: Path | None = None,
                 require_link_proof: bool = False) -> dict[str, object]:
    """The consumer that linked against this library in the job, and what the
    receipt can prove about it.

    ``ran`` is the job's own word that the consumer ran; when ``ran_evidence``
    is given it replaces that word: the consumer counts as run only when the
    file its passing run wrote names this binary.

    ``link_proof`` is ``linked_packaged_bytes`` only when the loader entry
    that names this library, resolved through the consumer's own run-path (or
    the library digest a passing run recorded), is a file whose bytes are the
    packaged library.  Otherwise it is ``linked_unverified`` with the reason,
    and with ``require_link_proof`` the receipt does not prove.
    """
    if consumer is None:
        return {"status": "none", "reason": note}
    data = consumer.read_bytes()
    consumer_sha = hashlib.sha256(data).hexdigest()
    run = "ran in this job" if ran else "not run in this job"
    evidence_library = ""
    if ran_evidence is not None:
        recorded, evidence_library = read_ran_evidence(ran_evidence)
        ran = recorded == consumer_sha
        if not ran:
            evidence_library = ""
        run = (f"ran in this job ({ran_evidence.name} names this binary)" if ran else
               f"not run in this job ({ran_evidence.name} " +
               ("names another binary)" if recorded else "was not written)"))
    facts = binfacts.binary_facts(data)
    fmt = str(facts.get("format", "unknown"))
    needed = [str(item) for item in facts.get("needed", [])]
    if fmt == "mach-o-fat":
        first = (facts.get("slices") or [{}])[0]
        needed = [str(item) for item in first.get("needed", [])]
    identity = library_identity(library_member, library_facts or {})
    entries = [item for item in needed if entry_names_library(item, identity, fmt)]
    consumer_dir = str(consumer.resolve().parent)
    row: dict[str, object] = {
        "status": "linked-and-ran" if ran else "linked",
        "ran": ran,
        "run": run,
        "binary": consumer.name,
        "sha256": consumer_sha,
        "arch": facts.get("arch", "unknown"),
        "format": fmt,
        "needed": needed,
        "library_identity": identity,
        "references_library": bool(entries),
        "link_proof_required": require_link_proof,
        "note": note,
    }
    loader: dict[str, object] = {"entry": entries[0] if entries else "", "search": [], "resolves_to": ""}
    proof, reason = "linked_unverified", ""
    if not entries:
        reason = f"the consumer has no loader entry naming {identity or 'the library'}"
    else:
        dirs, tail = search_dirs(fmt, entries[0], binfacts.load_paths(data), consumer_dir)
        loader["search"] = dirs
        for directory in dirs:
            candidate = os.path.join(directory, tail)
            if os.path.isfile(candidate):
                loader["resolves_to"] = os.path.normpath(candidate)
                loader["resolved_sha256"] = sha256_file(Path(candidate))
                break
        if loader["resolves_to"]:
            loader["how"] = "the consumer's own run-path (no loader environment)"
            if loader["resolved_sha256"] == library_sha256:
                proof = "linked_packaged_bytes"
            else:
                proof = "linked_other_bytes"
                reason = "the consumer's loader entry resolves to other bytes than the packaged library"
        elif evidence_library:
            loader["how"] = f"{ran_evidence.name}: the passing run recorded the library it loaded"
            loader["resolved_sha256"] = evidence_library
            if evidence_library == library_sha256:
                proof = "linked_packaged_bytes"
            else:
                proof = "linked_other_bytes"
                reason = "the run loaded other bytes than the packaged library"
        else:
            reason = (f"{entries[0]} does not resolve through the consumer's own run-path "
                      f"(searched {dirs or 'nothing'}), and no passing run recorded the library it loaded")
    row["loader"] = loader
    row["link_proof"] = proof
    if reason:
        row["link_proof_reason"] = reason
    return row


def runtime_identity(consumer_link: dict) -> dict[str, str]:
    """Whether this cell's V8 identity check ran in the job against the
    packaged bytes: ``ran`` needs a consumer that ran and loaded the packaged
    library; anything less is ``not_run`` with the reason."""
    status = consumer_link.get("status")
    if status == "none":
        return {"status": "not_run", "reason": str(consumer_link.get("reason", "")).strip()}
    if consumer_link.get("ran") is not True:
        return {"status": "not_run", "reason": f"the consumer linked but was {consumer_link.get('run')}: "
                                               f"{consumer_link.get('note', '')}".strip()}
    if consumer_link.get("link_proof") != "linked_packaged_bytes":
        return {"status": "not_run",
                "reason": "the consumer ran, but the receipt cannot prove it loaded the packaged library: "
                          f"{consumer_link.get('link_proof_reason', '')}".strip()}
    return {"status": "ran", "evidence": f"{consumer_link.get('binary')} {consumer_link.get('run')}, "
                                         f"loading {(consumer_link.get('loader') or {}).get('resolves_to') or 'the packaged library'}"}


def build_receipt(archive: Path, platform: str, arch: str, cell: str, out_dir: Path, source_root: Path,
                  probe: Probe | None = None, container_image: str = "", consumer: Path | None = None,
                  consumer_ran: bool = False, consumer_note: str = "",
                  consumer_ran_evidence: Path | None = None,
                  require_link_proof: bool = False) -> dict[str, object]:
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
                                      member_sha, binfacts.binary_facts(data), consumer_ran_evidence,
                                      require_link_proof),
    }
    receipt["runtime_identity"] = runtime_identity(receipt["consumer_link"])
    return receipt


# Cells whose consumer must prove it loaded the packaged bytes.  Android's
# validator runs on a device through LD_LIBRARY_PATH, so without a device run
# its link is recorded linked_unverified; every other consumer's own run-path
# leads to the packaged library.
LINK_PROOF_OPTIONAL = {"android"}


def link_proof_required(platform: str) -> bool:
    return platform not in LINK_PROOF_OPTIONAL


def portable_path(path: str) -> str:
    """A path as written on any host, compared on any host: --check runs on
    Linux for receipts written on Windows, where os.path would not split
    ``D:\\a\\out\\v8.dll``."""
    path = path.replace("\\", "/")
    drive = re.match(r"^[A-Za-z]:", path)
    normal = posixpath.normpath(path)
    return normal.lower() if drive else normal


def link_proof_problems(consumer: dict, library: dict, platform: str) -> list[str]:
    """Recompute what the consumer link claims from the receipt's own fields."""
    problems: list[str] = []
    required = link_proof_required(platform)
    if consumer.get("link_proof_required") is not required:
        problems.append(f"consumer_link.link_proof_required = {consumer.get('link_proof_required')!r}, "
                        f"but a {platform or 'unknown'} cell's is {required!r}")
    proof = consumer.get("link_proof")
    if proof not in LINK_PROOFS:
        problems.append(f"consumer_link.link_proof = {proof!r}: "
                        + str(consumer.get("link_proof_reason", "not one of " + str(sorted(LINK_PROOFS)))))
        return problems
    identity = library_identity(str(library.get("member", "")), library.get("facts") or {})
    if consumer.get("library_identity") != identity:
        problems.append(f"consumer_link.library_identity {consumer.get('library_identity')!r} "
                        f"is not the library's {identity!r}")
    if proof == "linked_unverified":
        if required:
            problems.append("the consumer link must prove the packaged bytes for this cell: "
                            + str(consumer.get("link_proof_reason", "")))
        if not str(consumer.get("link_proof_reason", "")).strip():
            problems.append("consumer_link is linked_unverified without a reason")
        return problems
    loader = consumer.get("loader") or {}
    entry = str(loader.get("entry", ""))
    fmt = str(consumer.get("format", ""))
    if entry not in [str(item) for item in consumer.get("needed", [])]:
        problems.append(f"consumer_link.loader.entry {entry!r} is not one of the consumer's loader entries")
    if not entry_names_library(entry, identity, fmt):
        problems.append(f"consumer_link.loader.entry {entry!r} does not name the library {identity!r}")
    resolved = str(loader.get("resolves_to", ""))
    if resolved:
        _dirs, tail = search_dirs(fmt, entry, {}, "")
        if not any(portable_path(str(d) + "/" + tail) == portable_path(resolved)
                   for d in loader.get("search", []) or []):
            problems.append(f"consumer_link.loader.resolves_to {resolved!r} is not in its search path")
    elif not str(loader.get("how", "")).strip():
        problems.append("consumer_link.loader says neither where the library resolved nor how it was loaded")
    digest = str(loader.get("resolved_sha256", ""))
    if not HEX_64.match(digest):
        problems.append("consumer_link.loader.resolved_sha256 is missing: "
                        "the receipt does not say which bytes the consumer loaded")
    elif digest != library.get("sha256"):
        problems.append("the consumer linked other bytes than the packaged library "
                        f"(loaded {digest[:12]}, packaged {str(library.get('sha256'))[:12]})")
    return problems


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
    problems.extend(post_link_problems(str((receipt.get("target") or {}).get("platform", "")),
                                       receipt.get("post_link")))
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
        if consumer.get("ran") is not (status == "linked-and-ran"):
            problems.append(f"consumer_link.status {status!r} disagrees with ran = {consumer.get('ran')!r}")
        if not str(consumer.get("run", "")).strip():
            problems.append("consumer_link.run does not say whether the consumer ran")
        problems.extend(link_proof_problems(consumer, library, platform))
        if consumer.get("references_library") is not True:
            problems.append("the consumer does not load the library")
        if ARCH_ALIASES.get(str(consumer.get("arch")), consumer.get("arch")) != ARCH_ALIASES.get(arch, arch):
            problems.append(f"the consumer is {consumer.get('arch')!r}, the cell is {arch!r}")
    identity = receipt.get("runtime_identity")
    if not isinstance(identity, dict) or identity.get("status") not in RUNTIME_STATUSES:
        problems.append(f"runtime_identity = {identity!r}")
    else:
        expected = runtime_identity(consumer)
        if identity.get("status") != expected["status"]:
            problems.append(f"runtime_identity says {identity.get('status')!r}, but the consumer link "
                            f"supports {expected['status']!r}")
        if identity.get("status") == "not_run" and not str(identity.get("reason", "")).strip():
            problems.append("runtime_identity is not_run without a reason")
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
    parser.add_argument("--consumer-ran", action="store_true",
                        help="the consumer ran and passed (tests only; jobs pass --consumer-ran-evidence)")
    parser.add_argument("--consumer-ran-evidence", type=Path,
                        help="a file the consumer's run wrote after it passed, holding the consumer's sha256; "
                             "the consumer counts as run only if it names this binary")
    parser.add_argument("--consumer-note", default="")
    parser.add_argument("--require-link-proof", action="store_true",
                        help="the consumer's loader entry must resolve to the packaged bytes. Every "
                             "platform but android requires it whether or not this is passed")
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
                            consumer_note=args.consumer_note,
                            consumer_ran_evidence=args.consumer_ran_evidence,
                            require_link_proof=args.require_link_proof or link_proof_required(args.platform))
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
    summary = {key: receipt[key] for key in ("compiler", "platform_sdk", "consumer_link", "runtime_identity")}
    summary["compiler"] = {side: summary["compiler"][side] for side in ("c", "cxx")}
    print(json.dumps(summary, indent=1)[:4000])
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
