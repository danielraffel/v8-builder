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
        'is_official_build=true',
        'is_debug=false',
        'chrome_pgo_phase=0',                   # embedder build: no PGO (avoids profile fetch)
        'v8_monolithic=true',
        'v8_use_external_startup_data=false',
        'v8_enable_i18n_support=true',          # D2: Intl ON
        'use_custom_libcxx=false',              # match Skia's system libc++ (macOS)
        'use_rtti=false',                       # match Skia -fno-rtti
        'v8_enable_sandbox=false',              # D3: hold constant; assert in consumer TU
        'treat_warnings_as_errors=false',
        'symbol_level=1',
    ]


def mac_gn_args(arch):
    return common_gn_args() + [
        f'target_cpu="{arch}"',                 # arm64 | x64
        'target_os="mac"',
        'mac_deployment_target="11.0"',         # match Pulp/Skia (macOS 11+)
    ]


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

    def gn_gen(self, arch):
        out = V8_DIR / "out" / f"{self.args.platform}-{arch}"
        args_gn = "\n".join(mac_gn_args(arch)) + "\n"
        out.mkdir(parents=True, exist_ok=True)
        (out / "args.gn").write_text(args_gn)
        say(f"args.gn:\n{args_gn}")
        run(["gn", "gen", str(out)], cwd=V8_DIR, env=self.env)
        return out

    def ninja(self, out):
        run(["ninja", "-C", str(out), "v8_monolith"], cwd=V8_DIR, env=self.env)
        lib = out / "obj" / "libv8_monolith.a"
        if not lib.exists():
            raise SystemExit(f"expected {lib} not produced")
        return lib

    def seal(self, monolith, arch):
        if self.args.no_seal:
            say("--no-seal: skipping seal step", Colors.WARN)
            return monolith
        backend = SEAL_DIR / ("macho.py" if self.args.platform == "mac" else "elf.py")
        out_dylib = BUILD_DIR / f"{self.args.platform}-{arch}" / "lib" / "libv8.dylib"
        out_dylib.parent.mkdir(parents=True, exist_ok=True)
        run([sys.executable, str(backend), "seal",
             "--monolith", str(monolith),
             "--out", str(out_dylib),
             "--policy", str(SEAL_DIR / "policy.json")], env=self.env)
        return out_dylib

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
        if self.args.platform != "mac":
            raise SystemExit("Only the macOS lane is implemented so far (Phase 1). "
                             "Linux/Windows land in later phases.")
        archs = (self.args.archs or "arm64").split(",")
        self.setup_depot_tools()
        self.fetch_v8()
        if not self.args.use_synced:
            self.sync_v8()
        else:
            say("--use-synced: building current checkout (skipping tag sync)", Colors.WARN)
        for arch in archs:
            out = self.gn_gen(arch)
            monolith = self.ninja(out)
            sealed = self.seal(monolith, arch)
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
