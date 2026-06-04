#!/usr/bin/env python3
"""
build-v8.py — build & seal a standalone, embeddable V8 for use next to Skia/Dawn.

Flagship: a SHARED library (.dylib/.so/.dll) with Intl ON, exporting only the
v8::/cppgc:: embedder API while ICU/zlib/Abseil stay INTERNAL — the property that
lets it coexist with Skia/Dawn's own bundled ICU/zlib/Abseil (Phase-0 finding:
duplicate Abseil between V8 and Dawn aborts the process unless one side's copy is
not exported). See planning/v8-builder-proposal.md.

Pipeline (macOS arm64 first — cheapest to debug the seal):
  setup_depot_tools -> sync_v8(tag) -> gn gen + ninja v8_monolith (static)
  -> seal/<platform>.py wraps it into a sealed shared lib -> package + manifest.

Copyright (c) 2026 Daniel Raffel. MIT. Structure inspired by skia-builder (Oli Larkin).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BUILD_DIR = BASE_DIR / "build"
DEPOT_TOOLS_PATH = BUILD_DIR / "tmp" / "depot_tools"
DEPOT_TOOLS_URL = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
SRC_DIR = BUILD_DIR / "src"
V8_DIR = SRC_DIR / "v8"
SEAL_DIR = BASE_DIR / "seal"

# Default pin: matches Homebrew libnode's V8 (verified to coexist with Dawn once
# Abseil is sealed). Bump deliberately, aligned to the paired Skia milestone (D1/D8).
DEFAULT_V8_TAG = "14.6.202.33"

# --- GN args (monolith static; sealed into a shared lib afterward) -----------
# ABI MUST match the real Skia build (proposal §5): system libc++ on macOS,
# -fno-rtti, Intl on. v8_monolithic bundles ICU/zlib/Abseil into one static lib,
# which seal/macho.py then wraps into a dylib exporting only v8::/cppgc::.
def common_gn_args():
    return [
        # is_official_build=false ON PURPOSE: official build forces ThinLTO (→ bitcode
        # archives that nm can't audit and that complicate sealing) AND applies
        # -fvisibility-global-new-delete=force-hidden, which clashes with the macOS 26.5
        # SDK libc++ (_LIBCPP_OVERRIDABLE_FUNC_VIS). A plain release sidesteps both and
        # still gives -O3/NDEBUG. The C++ ABI that must match Skia (libc++, rtti,
        # exceptions) is unaffected by this toggle.
        'is_official_build=false',
        'is_debug=false',
        'v8_monolithic=true',
        'v8_use_external_startup_data=false',
        'v8_enable_i18n_support=true',          # D2: Intl ON
        'use_rtti=false',                       # match Skia -fno-rtti
        'v8_enable_sandbox=false',              # D3: hold constant; assert in consumer TU
        # D3 (verified via a V8 runtime fatal-check): the embedder (choc/Pulp, and
        # libnode) compiles with pointer compression OFF, so the provider MUST match
        # or V8::Initialize aborts with an embedder-vs-V8 mismatch. Keep OFF for
        # drop-in parity. (V8 defaults this ON for 64-bit.)
        'v8_enable_pointer_compression=false',
        'treat_warnings_as_errors=false',
        'symbol_level=1',
    ]


# Map our arch labels (skia-builder convention) to V8 gn target_cpu values.
def gn_cpu(arch):
    return {"x86_64": "x64", "x64": "x64", "arm64": "arm64"}.get(arch, arch)


def mac_gn_args(arch):
    return common_gn_args() + [
        f'target_cpu="{gn_cpu(arch)}"',         # arm64 | x64 (x86_64 -> x64)
        'target_os="mac"',
        'mac_deployment_target="11.0"',         # match Pulp/Skia (macOS 11+)
        # macOS system libc++ is modern (C++20-complete) AND matches Skia's STL,
        # so use it directly. (Proven: P1d.)
        'use_custom_libcxx=false',
    ]


def linux_gn_args(arch):
    return common_gn_args() + [
        f'target_cpu="{gn_cpu(arch)}"',         # x64 | arm64
        'target_os="linux"',
        # D2b RESOLVED (CI finding 2026-06-04): do NOT force use_custom_libcxx=false
        # on Linux — Chromium's bundled debian-bullseye sysroot libstdc++ is too old
        # for C++20 (no std::bit_cast / <source_location>) and V8 fails to compile.
        # In the sealed-SHARED model V8 shares no C++ types with Skia (serialized
        # boundary) and its libc++ is hidden by the seal, so V8 uses its OWN bundled
        # libc++ (the default) — exactly like it bundles its own absl/icu. Skia keeps
        # libstdc++; the two coexist because nothing STL crosses the boundary.
    ]


def platform_gn_args(platform, arch):
    if platform == "mac":
        return mac_gn_args(arch)
    if platform == "linux":
        return linux_gn_args(arch)
    raise SystemExit(f"gn args for platform '{platform}' not implemented "
                     "(Windows DLL seal is a separate lane — see seal/coff_research.md)")


# Injected into V8's BUILD.gn. Proven on macOS (P1c); the ELF branch is the Linux
# analog (version-script + --whole-archive), validated on CI (unprovable on macOS).
SEAL_TARGET_GN = '''\
# >>> v8-builder sealed-shared target (injected)
if ((is_mac || is_linux) && v8_monolithic) {
  v8_shared_library("v8_sealed_shared") {
    output_name = "v8"
    sources = []
    deps = [ ":v8_monolith" ]
    configs = [ ":internal_config_base" ]
    if (v8_force_optimize_speed ||
        (((is_posix && !is_android) || is_win) && !using_sanitizer)) {
      remove_configs = [ "//build/config/compiler:optimize_speed" ]
    } else if (is_debug && !v8_optimized_debug) {
      remove_configs = [ "//build/config/compiler:no_optimize" ]
    } else {
      remove_configs = [ "//build/config/compiler:optimize_max" ]
    }
    if (is_mac) {
      inputs = [ "v8_embedder_exports.txt" ]
      ldflags = [
        "-Wl,-exported_symbols_list," + rebase_path("v8_embedder_exports.txt", root_build_dir),
        "-Wl,-install_name,@rpath/libv8.dylib",
        "-Wl,-force_load," + rebase_path("$root_build_dir/obj/libv8_monolith.a", root_build_dir),
      ]
    } else {
      # ELF: do NOT hand-roll --whole-archive on the monolith. Chromium's `solink`
      # rule (build/toolchain/gcc_toolchain.gni) ALREADY wraps {{inputs}} in
      # `-Wl,--whole-archive {{inputs}} -Wl,--no-whole-archive` on Linux (the non-aix,
      # non-mipsel branch). Since `deps=[:v8_monolith]` puts libv8_monolith.a in
      # {{inputs}}, the rule whole-archives it ONCE, in place — and appends the Rust
      # closure as {{rlibs}} after. A second hand-rolled --whole-archive of the same
      # archive makes lld include every member TWICE (it does not dedup) → the
      # duplicate-symbol failure seen in CI run 26961155381. So: just deps + the
      # version-script. (Mach-O differs: ld64 needs explicit -force_load and dedups.)
      inputs = [ "v8_embedder_exports.map" ]
      ldflags = [
        "-Wl,--version-script=" + rebase_path("v8_embedder_exports.map", root_build_dir),
        "-Wl,-soname,libv8.so",
      ]
    }
  }
}
'''


class Colors:
    OK = '\033[92m'; WARN = '\033[93m'; FAIL = '\033[91m'; END = '\033[0m'


def say(msg, c=Colors.OK):
    print(f"{c}[v8-builder] {msg}{Colors.END}", flush=True)


def run(cmd, cwd=None, env=None):
    say("$ " + (" ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)), Colors.WARN)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


class V8Build:
    def __init__(self, args):
        self.args = args
        self.tag = args.v8_version or DEFAULT_V8_TAG
        self.env = dict(os.environ)
        self.env["PATH"] = f"{DEPOT_TOOLS_PATH}{os.pathsep}{self.env.get('PATH','')}"
        self.env["DEPOT_TOOLS_UPDATE"] = "1"

    def setup_depot_tools(self):
        if not DEPOT_TOOLS_PATH.exists():
            DEPOT_TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--depth", "1", DEPOT_TOOLS_URL, str(DEPOT_TOOLS_PATH)])
        else:
            say("depot_tools present")

    def fetch_v8(self):
        SRC_DIR.mkdir(parents=True, exist_ok=True)
        if not V8_DIR.exists():
            run(["fetch", "--no-history", "v8"], cwd=SRC_DIR, env=self.env)
        else:
            say("v8 checkout present")

    def sync_v8(self):
        # Pin to the exact tag, then sync deps for that revision.
        run(["git", "fetch", "--tags", "--depth", "1", "origin", f"refs/tags/{self.tag}"],
            cwd=V8_DIR, env=self.env)
        run(["git", "checkout", f"refs/tags/{self.tag}"], cwd=V8_DIR, env=self.env)
        run(["gclient", "sync", "-D", "--force", "--reset",
             f"--revision=src/v8@refs/tags/{self.tag}"], cwd=SRC_DIR, env=self.env)

    # The seal is an IN-TREE gn shared_library target (proven on macOS, P1c): it deps
    # :v8_monolith and lets gn compute V8 15.1's full Rust-Temporal + system link
    # closure, emitting a dylib/so that exports ONLY v8::/cppgc:: (force_load monolith +
    # -exported_symbols_list on Mach-O / --version-script on ELF). The earlier
    # standalone-clang seal could NOT do this (it can't reconstruct the Rust closure).
    SEAL_MARKER = "# >>> v8-builder sealed-shared target (injected)"

    def inject_seal_target(self):
        build_gn = V8_DIR / "BUILD.gn"
        text = build_gn.read_text()
        if self.SEAL_MARKER in text:
            say("seal target already injected")
        else:
            # export lists in V8 root (Mach-O patterns + ELF version script)
            (V8_DIR / "v8_embedder_exports.txt").write_text(
                "\n".join(["__ZN2v8*", "__ZNK2v8*", "__ZTVN2v8*", "__ZTIN2v8*",
                           "__ZTSN2v8*", "__ZN6cppgc*", "__ZNK6cppgc*"]) + "\n")
            run([sys.executable, str(SEAL_DIR / "elf.py"), "version-script",
                 "--out", str(V8_DIR / "v8_embedder_exports.map")], env=self.env)
            build_gn.write_text(text + "\n" + SEAL_TARGET_GN + "\n")
            say("injected v8_sealed_shared gn target")

    def gn_gen(self, arch):
        out = V8_DIR / "out" / f"{self.args.platform}-{arch}"
        args_gn = "\n".join(platform_gn_args(self.args.platform, arch)) + "\n"
        out.mkdir(parents=True, exist_ok=True)
        (out / "args.gn").write_text(args_gn)
        say(f"args.gn:\n{args_gn}")
        run(["gn", "gen", str(out)], cwd=V8_DIR, env=self.env)
        return out

    def build_sealed(self, out, arch):
        run(["ninja", "-C", str(out), "v8_sealed_shared"], cwd=V8_DIR, env=self.env)
        libname = "libv8.dylib" if self.args.platform == "mac" else "libv8.so"
        lib = out / libname
        if not lib.exists():
            raise SystemExit(f"expected sealed {lib} not produced")
        dest = BUILD_DIR / f"{self.args.platform}-{arch}" / "lib"
        dest.mkdir(parents=True, exist_ok=True)
        run(["cp", str(lib), str(dest / libname)])
        # audit: assert 0 absl/icu/zlib internals exported
        backend = SEAL_DIR / ("macho.py" if self.args.platform == "mac" else "elf.py")
        run([sys.executable, str(backend), "audit", "--lib", str(dest / libname),
             "--policy", str(SEAL_DIR / "policy.json")], env=self.env)
        return dest / libname

    def package(self, sealed, arch):
        dest = BUILD_DIR / f"{self.args.platform}-{arch}"
        inc = dest / "include"
        inc.mkdir(parents=True, exist_ok=True)
        # V8 public headers
        run(["rsync", "-a", "--delete",
             str(V8_DIR / "include") + "/", str(inc) + "/"])
        manifest = {
            "v8_version": self.tag,
            "platform": self.args.platform,
            "arch": arch,
            "i18n": True,
            "shared": True,
            "sealed": not self.args.no_seal,
            "lib": str(Path(sealed).name),
        }
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
        say(f"packaged {dest}")

    def run_all(self):
        # mac is proven (P1d). linux is implemented and validates on a Linux CI runner
        # (unprovable on a macOS host). Windows DLL seal is a separate lane (coff).
        if self.args.platform == "win":
            raise SystemExit("Windows DLL-export seal is a separate lane — see "
                             "seal/coff_research.md; not wired into build-v8.py yet.")
        default_arch = {"mac": "arm64", "linux": "x64"}[self.args.platform]
        archs = (self.args.archs or default_arch).split(",")
        self.setup_depot_tools()
        self.fetch_v8()
        if not self.args.use_synced:
            self.sync_v8()
        else:
            say("--use-synced: building current checkout (skipping tag sync)", Colors.WARN)
        self.inject_seal_target()
        for arch in archs:
            out = self.gn_gen(arch)
            sealed = self.build_sealed(out, arch)
            self.package(sealed, arch)
        say("done", Colors.OK)


def main():
    p = argparse.ArgumentParser(description="Build & seal standalone V8 for embedding next to Skia/Dawn")
    p.add_argument("platform", choices=["mac", "linux", "win"], help="Target platform")
    p.add_argument("-archs", help="Comma-separated archs (e.g. arm64,x86_64 / x64)")
    p.add_argument("-tag", dest="v8_version", help=f"V8 version tag (default {DEFAULT_V8_TAG})")
    p.add_argument("--no-seal", action="store_true", help="Skip sealing (debug only)")
    p.add_argument("--use-synced", action="store_true",
                   help="Build the currently-synced checkout instead of syncing to -tag")
    p.add_argument("--fetch-only", action="store_true", help="Only setup depot_tools + fetch/sync V8")
    args = p.parse_args()
    b = V8Build(args)
    if args.fetch_only:
        b.setup_depot_tools(); b.fetch_v8(); b.sync_v8(); say("fetch-only complete")
        return
    b.run_all()


if __name__ == "__main__":
    main()
