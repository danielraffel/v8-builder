#!/usr/bin/env python3
"""
build-v8.py — build & seal a standalone, embeddable V8 for use next to Skia/Dawn.

Flagship target: a SHARED library (.dylib/.so/.dll) with Intl ON, whose export table
exposes only the v8::/cppgc:: embedder API while ICU/zlib stay internal (the property
that lets it coexist with Skia's bundled ICU — see planning/v8-builder-proposal.md).

Modeled on skia-builder's build-skia.py (depot_tools + gn + ninja).

STATUS: SKELETON. The build stages are not implemented yet; they print intent and
exit non-zero so nothing here can masquerade as a real build. Logic lands per the
runbook (Phase 1 = Linux, Phase 2 = macOS, Phase 3 = Windows).

Copyright (c) 2026 Daniel Raffel. MIT. Structure inspired by skia-builder (Oli Larkin).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent / "build"
DEPOT_TOOLS_PATH = BASE_DIR / "tmp" / "depot_tools"
DEPOT_TOOLS_URL = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
V8_SRC_DIR = BASE_DIR / "src" / "v8"

# ---------------------------------------------------------------------------
# GN args — the contract. Flagship = shared library, Intl ON, ABI-matched to Skia.
# These MUST be reconciled against a real Skia build before locking (proposal §5).
# ---------------------------------------------------------------------------
BASE_GN_ARGS = """\
is_official_build = true
is_debug = false
v8_use_external_startup_data = false
v8_enable_i18n_support = true
use_rtti = false
symbol_level = 1
treat_warnings_as_errors = false
"""

# Shared-library posture (flagship). For a static "lite" variant we'd instead set
# v8_monolithic = true and v8_enable_i18n_support = false (see proposal §6b / D2).
SHARED_GN_ARGS = """\
is_component_build = true
v8_monolithic = false
"""

# ABI knobs that must equal Skia's (proposal §5):
#   - Linux STL: match the real Skia build (likely libstdc++; Skia doesn't set
#     use_custom_libcxx). Confirm empirically before locking — do NOT assume.
#   - Windows CRT: /MT to match skia-builder.
#   - pointer compression / sandbox (D3): pick one, hold constant, assert in consumer TU.

PLATFORMS = ["mac", "linux", "win", "universal"]


class V8BuildScript:
    def __init__(self):
        self.args = self._parse_args()

    def _parse_args(self):
        p = argparse.ArgumentParser(description="Build & seal standalone V8 for embedding next to Skia/Dawn")
        p.add_argument("platform", choices=PLATFORMS, help="Target platform")
        p.add_argument("-config", choices=["Debug", "Release"], default="Release")
        p.add_argument("-archs", help="Comma-separated archs (e.g. arm64,x86_64 / x64)")
        p.add_argument("-tag", dest="v8_version", help="V8 version/milestone to build (e.g. m149 or 13.6.233.8)")
        p.add_argument("--shallow", action="store_true")
        p.add_argument("--no-seal", action="store_true", help="Skip the symbol-sealing step (debug only)")
        p.add_argument("--lite", action="store_true", help="Static, Intl-off 'lite' variant instead of shared flagship")
        return p.parse_args()

    # --- stages (SKELETON; not implemented) -------------------------------
    def setup_depot_tools(self):
        _todo("setup_depot_tools", "clone depot_tools, `fetch v8` / gclient sync against pinned tag")

    def generate_gn_args(self, arch):
        _todo("generate_gn_args", f"emit args.gn for arch={arch} (shared, Intl on, ABI-matched to Skia)")

    def build(self, arch):
        _todo("build", f"gn gen + ninja v8 (shared) for arch={arch}")

    def seal(self):
        _todo("seal", "run seal/<platform>.py: export only v8::/cppgc::, keep ICU/zlib internal; audit")

    def package(self):
        _todo("package", "copy headers + sealed shared lib + manifest.json (provenance triangle)")

    def run(self):
        print(f"[v8-builder] platform={self.args.platform} archs={self.args.archs} "
              f"variant={'lite-static' if self.args.lite else 'flagship-shared'}")
        self.setup_depot_tools()  # will exit — skeleton


def _todo(stage, what):
    sys.stderr.write(
        f"\n[v8-builder] NOT IMPLEMENTED: {stage}() — {what}.\n"
        f"This is Phase-0 scaffolding; build logic lands per planning/v8-builder-runbook.md.\n")
    sys.exit(2)


if __name__ == "__main__":
    V8BuildScript().run()
