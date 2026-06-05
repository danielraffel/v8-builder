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
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _ccache_args():
    # Big CI win: cache compiled objects across runs so a re-run only recompiles the few
    # TUs that changed (seal/ABI tweaks, the link) — minutes instead of a full ~1h build.
    # Auto-on only when ccache is in PATH (CI installs it); local builds (already fast via
    # the persistent build dir) are unaffected. use_remoteexec=false so cc_wrapper isn't
    # overridden by V8's reclient/RBE detection.
    if shutil.which("ccache"):
        return ['cc_wrapper="ccache"', 'use_remoteexec=false']
    return []

BASE_DIR = Path(__file__).resolve().parent
BUILD_DIR = BASE_DIR / "build"
DEPOT_TOOLS_PATH = BUILD_DIR / "tmp" / "depot_tools"
DEPOT_TOOLS_URL = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
SRC_DIR = BUILD_DIR / "src"
V8_DIR = SRC_DIR / "v8"
SEAL_DIR = BASE_DIR / "seal"

# Default pin: matches the desktop release lanes (mac/linux/win all ship 15.1.27,
# verified to coexist with Dawn once Abseil is sealed). Bump deliberately, aligned to
# the paired Skia milestone (D1/D8).
DEFAULT_V8_TAG = "15.1.27"

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
        # The monolith is destined for a SHARED library (D5 flagship), so build it
        # the way V8 builds its own shared/component lib: this gn arg defines
        # V8_TLS_USED_IN_LIBRARY, which switches V8's hot-path thread_locals
        # (g_current_isolate_, g_current_local_heap_) from the exe-only "local-exec"
        # TLS model to the PIC-safe "local-dynamic" model + a hidden non-inline getter.
        # Without it, linking the monolith into `-shared` fails on ELF/lld with
        # `relocation R_X86_64_TPOFF32 ... cannot be used with -shared` (CI 26965278162).
        # Its ONLY effect (BUILD.gn:1257) is that define — no visibility/export change.
        'v8_monolithic_for_shared_library=true',
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
    ] + _ccache_args()


# Map our arch labels (skia-builder convention) to V8 gn target_cpu values.
# arm/x86 are the 32-bit Android ABIs (armeabi-v7a / x86); arm64/x64 cover the rest.
def gn_cpu(arch):
    return {"x86_64": "x64", "x64": "x64", "arm64": "arm64",
            "arm": "arm", "x86": "x86"}.get(arch, arch)


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
        # D2b REVISED (2026-06-04): build with the PLATFORM C++ ABI (system libstdc++),
        # NOT Chromium's bundled libc++. V8's public API exposes std types
        # (e.g. v8::platform::NewDefaultPlatform(..., std::unique_ptr<...>, ...)); the
        # bundled libc++ mangles std:: with Chromium's `__Cr` ABI namespace
        # (_LIBCPP_ABI_NAMESPACE=__Cr), so a consumer built with system libstdc++ gets
        # undefined-reference at link (verified on a real x86_64 link). That makes the
        # .so non-drop-in. Node ships libnode against the system libstdc++ (RHEL-8
        # baseline, glibc>=2.28) for exactly this reason — Node-API is the C ABI; the
        # direct C++ API uses the platform ABI. So: use the HOST's modern toolchain
        # (use_sysroot=false fixes the old-bullseye-sysroot C++20 gap that drove D2b's
        # earlier bundled-libc++ choice) with standard libstdc++. The export SEAL is
        # unchanged (ICU/zlib/Abseil stay internal). Release portability (old-glibc
        # floor) wants a RHEL/Rocky-8 build image — tracked as a follow-up; ubuntu-24.04
        # CI is fine to validate the ABI is consumable.
        'use_sysroot=false',
        'use_custom_libcxx=false',
        'use_custom_libcxx_for_host=false',
        # use_sysroot=false makes gn resolve system libs via pkg-config; V8 standalone
        # does NOT need glib (it's a Chromium-UI default), so turn it off rather than
        # require libglib2.0-dev on the build host. V8 monolith otherwise needs only
        # libc/libstdc++/libm, present on any modern Linux.
        'use_glib=false',
    ]


def win_gn_args(arch):
    # D3 (pointer compression OFF) exists only to match Homebrew libnode's ABI — which is
    # a mac/linux baseline; there is NO libnode on Windows. V8's non-default
    # non-pointer-compressed Windows matrix is undercovered upstream and MISCOMPILES:
    # JSAtomicsMutex/Condition use an 8-byte ExternalPointerMember (sandbox+compression
    # off) and MSVC's class layout pads the packed V8_OBJECT base 4 bytes past what Torque
    # models, tripping `static_assert(kOwnerThreadIdOffset == offsetof(...))` (CI run
    # 26973680886). Rather than an invasive, version-fragile Torque/struct patch, use V8's
    # SUPPORTED default on Windows: pointer compression ON. The Windows Pulp/choc V8
    # consumer must match (compile with V8_COMPRESS_POINTERS) — there's no libnode to
    # constrain it. So Windows v8.dll has compression ON; mac/linux stay OFF (D3).
    base = [a for a in common_gn_args()
            if not a.startswith("v8_enable_pointer_compression")]
    return base + [
        f'target_cpu="{gn_cpu(arch)}"',         # x64 | arm64
        'target_os="win"',
        'v8_enable_pointer_compression=true',   # Windows-only (see note above)
        # PE/COFF: a DLL exports ONLY dllexport'd / .def / /EXPORT: symbols, so the
        # ICU/zlib/Abseil objects inside the monolith stay internal by construction —
        # no whole-archive symbol-hiding needed (unlike ELF/Mach-O). v8_monolith asserts
        # !is_component_build, so the dllexport lever is v8_expose_public_symbols, which
        # defines BUILDING_V8_SHARED → V8_EXPORT = __declspec(dllexport) for the
        # v8::/cppgc:: surface. The seal target then /WHOLEARCHIVEs the monolith into one
        # v8.dll (+ v8.dll.lib import lib). See seal/coff_research.md.
        'is_component_build=false',
        'v8_expose_public_symbols=true',
        # NOTE — Windows consumer-ABI is an OPEN DECISION (needs Pulp's Windows toolchain).
        # Keep V8's BUNDLED libc++ here: it builds + seals + audits clean (proven, CI run
        # 26975751590 → [2432/2432] v8.dll). Switching to the MSVC STL (use_custom_libcxx=
        # false) for a drop-in MSVC ABI instead re-triggers a Torque-vs-C++ offset break
        # (JSInterceptorMap, CI 26984416635) — the compression-ON × MSVC-STL × non-component
        # matrix is undercovered upstream, fixable only by invasive per-object Torque
        # patches. So Windows v8.dll exposes the Chromium-style libc++ (__Cr) ABI; a
        # consumer (and the coexistence validator) must build with clang-cl + that libc++ —
        # which is how the Chromium ecosystem does Windows. Revisit if Pulp's Windows build
        # uses MSVC cl + MSVC STL. See seal/coff_research.md.
    ]


# Android min-SDK floor. config.gni's default_min_sdk_version is 29 in this checkout
# (23 is a Cronet-only lower floor); 29 (Android 10) is the honest, supported default
# and what stock NDK r-series apps target. Overridable via -ndk-api-level.
ANDROID_DEFAULT_NDK_API_LEVEL = 29


def android_gn_args(arch, ndk_api_level=ANDROID_DEFAULT_NDK_API_LEVEL,
                    bundled_libcxx=True):
    # Android is a CROSS-COMPILE from the x86_64-Linux host (host_cpu=x64,
    # host_os=linux, target_os=android). It reuses the already-green Linux ELF seal
    # (seal/elf.py, unchanged) — Android .so uses the same Itanium mangling + the same
    # --version-script mechanism. So android_gn_args = common_gn_args (TLS-in-library,
    # Intl ON, no-rtti, sandbox/compression OFF for desktop drop-in parity) PLUS the
    # android triple, MINUS the three Linux-only ABI escapes (use_sysroot/
    # use_custom_libcxx/use_glib): Android MUST build against the NDK sysroot, there is
    # no host system libstdc++ to drop in to, and there is no glib on android.
    args = common_gn_args() + [
        'target_os="android"',
        f'target_cpu="{gn_cpu(arch)}"',         # arm64 | x64 | arm | x86
        # Android is ALWAYS cross-compiled from an x86_64-Linux host: V8's bundled host
        # clang + Rust are x86_64-only (tools/clang has no Linux_arm64; the rust-toolchain
        # ships only x86_64-unknown-linux-gnu rustlib), and config.gni hard-codes
        # android_host_arch="x86_64". Pin host_cpu="x64" so gn selects the x86_64 host
        # toolchain even when the build machine is arm64 (the bundled x86_64 host tools
        # then run under Rosetta on an arm64 Mac/VM, or natively on an x86_64 host). An
        # arm64 host_cpu makes gn demand an aarch64-linux-gnu rust host sysroot that the
        # bundled toolchain does not ship → the build fails on libstd.rlib.
        'host_cpu="x64"',
        # config.gni hard-wires android_ndk_root to the DEPS-fetched NDK
        # (//third_party/android_toolchain/ndk), so we set NO ndk path here — only the
        # min-SDK floor, which gates which API the .so loads against.
        f'android_ndk_api_level={ndk_api_level}',
    ]
    # CONSUMER-ABI GATE (the review's #1 risk) — RESOLVED BY EXPERIMENT.
    #
    # V8's public API exposes std:: types (v8::platform::NewDefaultPlatform(...,
    # std::unique_ptr<...>)). V8 defaults to its BUNDLED libc++, which carries the `__Cr`
    # ABI namespace (_LIBCPP_ABI_NAMESPACE=__Cr); a stock-NDK consumer linking the NDK
    # libc++ would then see a __Cr-vs-NDK skew on that std:: surface.
    #
    # We TRIED the NDK-libc++ target FIRST (review addendum #1): use_custom_libcxx=false
    # + use_custom_libcxx_for_host=true, which routes the target toolchain to the NDK
    # libc++ while host tools keep Chromium libc++. It compiled all TUs but FAILED AT THE
    # FINAL LINK on the DEPS-fetched cipd `android_toolchain`, two ways, both root-caused:
    #   (a) default use_custom_libunwind=true emits `--unwindlib=none` and expects
    #       Chromium's bundled libunwind — which is no longer dragged in once the custom
    #       libc++ is dropped — so the solink fails undefined on _Unwind_Backtrace/
    #       _Unwind_GetIP/...; and Chromium's bundled libunwind is visibility-locked to
    #       its own libc++abi, so it can't be added back alongside the NDK libc++.
    #   (b) forcing use_custom_libunwind=false makes clang's Android driver request
    #       `-l:libunwind.a`, which the STRIPPED cipd android_toolchain does not ship
    #       (it carries the NDK sysroot + NDK libc++, but no standalone unwinder).
    # i.e. the cipd build-toolchain has no aarch64-android unwinder usable with NDK libc++.
    #
    # DECISION: default to V8's BUNDLED libc++ (the self-contained path that links on this
    # toolchain — it provides its own complete libc++ / libc++abi / libunwind stack folded
    # statically into libv8.so, no libc++_shared.so runtime dep). The consumer-ABI contract
    # is then the WINDOWS model: the Android consumer (Pulp's NDK build) must compile
    # against a Chromium-style (__Cr) libc++, exactly as the Windows v8.dll requires. The
    # `--android-bundled-libcxx` flag / bundled_libcxx kept as an explicit lever; the
    # NDK-libc++ attempt is re-enabled by passing bundled_libcxx=False once a FULL NDK
    # (with a standalone libunwind.a) is wired in place of the cipd build-toolchain.
    # libcxx_abi_unstable=false would NOT fix the __Cr namespace, so we do not rely on it.
    if not bundled_libcxx:
        args += [
            'use_custom_libcxx=false',           # target -> NDK libc++ (stock-NDK ABI)
            'use_custom_libcxx_for_host=true',    # host tools stay on Chromium libc++
            # NOTE: blocked on the cipd toolchain by the missing aarch64-android unwinder
            # (see the gate writeup above). Only usable with a full NDK present.
        ]
    return args


def platform_gn_args(platform, arch, args=None):
    if platform == "mac":
        return mac_gn_args(arch)
    if platform == "linux":
        return linux_gn_args(arch)
    if platform == "win":
        return win_gn_args(arch)
    if platform == "android":
        level = getattr(args, "ndk_api_level", None) or ANDROID_DEFAULT_NDK_API_LEVEL
        # Default to bundled libc++ (the path that links on the cipd android_toolchain —
        # see android_gn_args). --android-ndk-libcxx opts INTO the NDK-libc++ attempt,
        # which needs a full NDK (standalone libunwind.a) to link.
        bundled = not bool(getattr(args, "android_ndk_libcxx", False))
        return android_gn_args(arch, ndk_api_level=level, bundled_libcxx=bundled)
    raise SystemExit(f"gn args for platform '{platform}' not implemented")


# Injected into V8's BUILD.gn. Proven on macOS (P1c); the ELF branch is the Linux
# analog (version-script + --whole-archive), validated on CI (unprovable on macOS).
SEAL_TARGET_GN = '''\
# >>> v8-builder sealed-shared target (injected)
if ((is_mac || is_linux || is_win || is_android) && v8_monolithic) {
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
    } else if (is_win) {
      # PE/COFF: nothing is exported unless dllexport'd, so ICU/zlib/absl stay internal
      # by construction (v8_expose_public_symbols made V8_EXPORT=dllexport for v8::).
      # Hiding is free; INCLUSION is not — /WHOLEARCHIVE pulls every monolith object
      # into v8.dll (the leaf .dll has no undefined refs into the monolith otherwise).
      # /IMPLIB names the import lib the consumer links (v8.dll.lib), which references
      # only the exported v8:: surface → no ICU collision with Skia at link time.
      ldflags = [
        "/WHOLEARCHIVE:" + rebase_path("$root_build_dir/obj/v8_monolith.lib", root_build_dir),
        "/IMPLIB:" + rebase_path("$root_out_dir/v8.dll.lib", root_build_dir),
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
    if os.name == "nt" and isinstance(cmd, list):
        # depot_tools ships fetch/gclient/gn as .bat; Windows CreateProcess won't
        # resolve a bare name to its .bat (PATHEXT is a shell feature). Run through the
        # shell so cmd.exe resolves it against PATH (which includes depot_tools).
        subprocess.run(subprocess.list2cmdline([str(c) for c in cmd]),
                       cwd=cwd, env=env, check=True, shell=True)
    else:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)


class V8Build:
    def __init__(self, args):
        self.args = args
        self.tag = args.v8_version or DEFAULT_V8_TAG
        self.env = dict(os.environ)
        self.env["PATH"] = f"{DEPOT_TOOLS_PATH}{os.pathsep}{self.env.get('PATH','')}"
        self.env["DEPOT_TOOLS_UPDATE"] = "1"
        if os.name == "nt":
            # Use the runner's local Visual Studio + Windows SDK, not Google's internal
            # win toolchain (which external builders can't fetch). windows-2022 has VS2022.
            self.env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"

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

    def _enable_android_checkout(self):
        # Android: V8's DEPS gates the NDK (+ SDK) on the `checkout_android` custom var
        # (a cipd package, ~3-4 GB, fetched not vendored). A default sync omits it, so
        # third_party/android_toolchain/ndk is absent and gn can't resolve the android
        # toolchain. The depot_tools `gclient sync` CLI in this pin does NOT accept
        # `--custom-var` (verified: "no such option"); the gate must live in the
        # .gclient solution's `custom_vars` (+ a top-level `target_os = ["android"]`).
        # Idempotently rewrite .gclient to carry both, preserving the solution list.
        gclient_file = SRC_DIR / ".gclient"
        if not gclient_file.exists():
            return
        text = gclient_file.read_text(encoding="utf-8")
        if '"checkout_android": True' in text and 'target_os' in text:
            return
        ns = {}
        exec(text, ns)  # .gclient is a python file: solutions = [...], target_os = [...]
        sols = ns.get("solutions", [])
        for s in sols:
            s.setdefault("custom_vars", {})["checkout_android"] = True
        tos = sorted(set(ns.get("target_os", [])) | {"android"})
        new = ("solutions = " + repr(sols) + "\n"
               + "target_os = " + repr(tos) + "\n")
        gclient_file.write_text(new, encoding="utf-8")
        say("enabled checkout_android in .gclient (custom_vars + target_os)")

    def sync_v8(self):
        # Pin to the exact tag, then sync deps for that revision.
        run(["git", "fetch", "--tags", "--depth", "1", "origin", f"refs/tags/{self.tag}"],
            cwd=V8_DIR, env=self.env)
        run(["git", "checkout", f"refs/tags/{self.tag}"], cwd=V8_DIR, env=self.env)
        if self.args.platform == "android":
            self._enable_android_checkout()
        run(["gclient", "sync", "-D", "--force", "--reset",
             f"--revision=src/v8@refs/tags/{self.tag}"], cwd=SRC_DIR, env=self.env)

    # The seal is an IN-TREE gn shared_library target (proven on macOS, P1c): it deps
    # :v8_monolith and lets gn compute V8 15.1's full Rust-Temporal + system link
    # closure, emitting a dylib/so that exports ONLY v8::/cppgc:: (force_load monolith +
    # -exported_symbols_list on Mach-O / --version-script on ELF). The earlier
    # standalone-clang seal could NOT do this (it can't reconstruct the Rust closure).
    SEAL_MARKER = "# >>> v8-builder sealed-shared target (injected)"

    # V8 gates BUILDING_V8_PLATFORM_SHARED (which makes v8::platform::NewDefaultPlatform
    # export via V8_PLATFORM_EXPORT) on is_component_build only. We build the Windows DLL
    # non-component with v8_expose_public_symbols, so widen the gate or v8::platform
    # symbols go MISSING from v8.dll. See seal/coff_research.md (Windows lane, biggest risk).
    _WIN_PLATFORM_GATE = ('if (is_component_build) {\n'
                          '    defines = [ "BUILDING_V8_PLATFORM_SHARED" ]')
    _WIN_PLATFORM_GATE_FIXED = ('if (is_component_build || v8_expose_public_symbols) {\n'
                                '    defines = [ "BUILDING_V8_PLATFORM_SHARED" ]')

    def inject_seal_target(self):
        build_gn = V8_DIR / "BUILD.gn"
        # Force UTF-8: V8's BUILD.gn contains non-ASCII (e.g. U+2192 "→") and Windows
        # defaults Path.read_text/write_text to cp1252, which can't encode it.
        text = build_gn.read_text(encoding="utf-8")
        if self.SEAL_MARKER in text:
            say("seal target already injected")
            return
        if self.args.platform == "win":
            # PE/COFF seal is dllexport-based: no export-list file needed. Patch the
            # libplatform export gate so v8::platform is exported in the non-component
            # shared build.
            if self._WIN_PLATFORM_GATE in text:
                text = text.replace(self._WIN_PLATFORM_GATE,
                                    self._WIN_PLATFORM_GATE_FIXED, 1)
                say("patched v8_libplatform export gate (non-component shared)")
            else:
                say("WARN: BUILDING_V8_PLATFORM_SHARED gate not found — "
                    "v8::platform exports may be missing", Colors.WARN)
        else:
            # export lists in V8 root (Mach-O patterns + ELF version script)
            (V8_DIR / "v8_embedder_exports.txt").write_text(
                "\n".join(["__ZN2v8*", "__ZNK2v8*", "__ZTVN2v8*", "__ZTIN2v8*",
                           "__ZTSN2v8*", "__ZN6cppgc*", "__ZNK6cppgc*"]) + "\n",
                encoding="utf-8")
            run([sys.executable, str(SEAL_DIR / "elf.py"), "version-script",
                 "--out", str(V8_DIR / "v8_embedder_exports.map")], env=self.env)
        build_gn.write_text(text + "\n" + SEAL_TARGET_GN + "\n", encoding="utf-8")
        say("injected v8_sealed_shared gn target")

    def gn_gen(self, arch):
        out = V8_DIR / "out" / f"{self.args.platform}-{arch}"
        args_gn = "\n".join(platform_gn_args(self.args.platform, arch, self.args)) + "\n"
        out.mkdir(parents=True, exist_ok=True)
        (out / "args.gn").write_text(args_gn, encoding="utf-8")
        say(f"args.gn:\n{args_gn}")
        run(["gn", "gen", str(out)], cwd=V8_DIR, env=self.env)
        return out

    LIBNAME = {"mac": "libv8.dylib", "linux": "libv8.so", "win": "v8.dll",
               "android": "libv8.so"}
    SEAL_BACKEND = {"mac": "macho.py", "linux": "elf.py", "win": "coff.py",
                    "android": "elf.py"}

    def build_sealed(self, out, arch):
        run(["ninja", "-C", str(out), "v8_sealed_shared"], cwd=V8_DIR, env=self.env)
        libname = self.LIBNAME[self.args.platform]
        lib = out / libname
        if not lib.exists():
            raise SystemExit(f"expected sealed {lib} not produced")
        dest = BUILD_DIR / f"{self.args.platform}-{arch}" / "lib"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib, dest / libname)
        if self.args.platform == "win":
            # ship the import lib the consumer links against (v8.dll.lib)
            implib = out / "v8.dll.lib"
            if implib.exists():
                shutil.copy2(implib, dest / "v8.dll.lib")
            else:
                raise SystemExit(f"expected import lib {implib} not produced")
        # audit: assert 0 absl/icu/zlib internals exported
        backend = SEAL_DIR / self.SEAL_BACKEND[self.args.platform]
        run([sys.executable, str(backend), "audit", "--lib", str(dest / libname),
             "--policy", str(SEAL_DIR / "policy.json")], env=self.env)
        if self.args.platform == "android":
            self._android_dt_needed_audit(dest / libname)
        return dest / libname

    # The export seal proves nothing LEAKS OUT; DT_NEEDED proves nothing UNWANTED is
    # required at load time. An android drop-in libv8.so should depend only on the
    # platform runtime (libc/libm/libdl + libc++/libc++abi when not folded static).
    # A stray libicu*/libz dependency would mean V8 pulled the system copy instead of
    # internalizing its own — the load-time analog of an export leak. Report it loudly;
    # treat a SYSTEM icu/zlib NEEDED as a hard fail.
    def _android_dt_needed_audit(self, lib):
        out = subprocess.run(["readelf", "-d", str(lib)], capture_output=True,
                             text=True).stdout
        needed = re.findall(r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]", out)
        say(f"DT_NEEDED: {needed}")
        bad = [n for n in needed
               if re.search(r"(libicu|libz\.so|libabsl)", n, re.IGNORECASE)]
        if bad:
            say(f"DT_NEEDED AUDIT FAIL — sealed libv8.so requires system "
                f"icu/zlib/absl: {bad}", Colors.FAIL)
            raise SystemExit(1)
        say(f"DT_NEEDED AUDIT OK — {len(needed)} deps, no system icu/zlib/absl")

    def _built_v8_sha(self):
        try:
            return subprocess.run(["git", "rev-parse", "HEAD"], cwd=V8_DIR,
                                  capture_output=True, text=True).stdout.strip() or None
        except Exception:
            return None

    def _lkgr_contract(self):
        # FR1 shared release-manifest contract: skia-builder AND v8-builder emit the SAME
        # fields naming the co-tested LKGR triple, so Pulp pairs the two releases by
        # matching skia/v8/dawn SHAs — a machine-checkable guarantee, not a naming
        # convention. We build V8 by TAG; `built_revision` is the SHA we actually built
        # (== the LKGR v8 SHA when the tag resolves to it). See planning/feature-requests.md FR1.
        lock = BASE_DIR / "planning" / "lkgr-lock.json"
        c = {"source": "chromium-lkgr-deps", "this_artifact": "v8",
             "built_revision": self._built_v8_sha()}
        if lock.exists():
            d = json.loads(lock.read_text())
            for k in ("source", "skia", "v8", "dawn", "chromium_deps_blob"):
                if k in d:
                    c[k] = d[k]
            c["source"] = d.get("source", c["source"])
        return c

    def package(self, sealed, arch):
        dest = BUILD_DIR / f"{self.args.platform}-{arch}"
        inc = dest / "include"
        # V8 public headers (shutil, not rsync — rsync isn't on Windows runners)
        if inc.exists():
            shutil.rmtree(inc)
        shutil.copytree(V8_DIR / "include", inc)
        manifest = {
            "v8_version": self.tag,
            "platform": self.args.platform,
            "arch": arch,
            "i18n": True,
            "shared": True,
            "sealed": not self.args.no_seal,
            "lib": str(Path(sealed).name),
            # FR1 pairing contract (LKGR triple + this_artifact + built_revision):
            "pair": self._lkgr_contract(),
        }
        if self.args.platform == "android":
            # No skia-builder Android asset exists (skia-builder publishes no
            # skia-build-android-*); Pulp builds Android Skia locally. So the Android
            # lane validates IDENTITY only (V8 init + eval + version) and treats the
            # seal audit + DT_NEEDED audit as the coexistence guarantee, exactly as the
            # Windows lane records its identity-only status. Full V8<->Skia/Dawn
            # coexistence on Android lands when a skia-builder android asset or an
            # on-device Pulp render exists.
            manifest["coexistence"] = "identity-only"
            manifest["coexistence_note"] = ("no skia-builder android artifact; seal + "
                                            "DT_NEEDED audits are the coexistence proof")
            manifest["ndk_api_level"] = (self.args.ndk_api_level
                                         or ANDROID_DEFAULT_NDK_API_LEVEL)
            manifest["libcxx"] = ("ndk"
                                  if getattr(self.args, "android_ndk_libcxx", False)
                                  else "bundled-chromium-__Cr")
            manifest["abi"] = {"arm64": "arm64-v8a", "x64": "x86_64",
                               "arm": "armeabi-v7a", "x86": "x86"}.get(arch, arch)
            # Lay the .so out as an Android app expects it (jniLibs/<abi>/libv8.so) so a
            # consumer can drop the artifact straight into src/main/jniLibs.
            jni = dest / "jniLibs" / manifest["abi"]
            jni.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sealed, jni / Path(sealed).name)
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        say(f"packaged {dest}")

    # Windows identity validator, built AS A GN TARGET so it inherits V8's exact
    # toolchain (clang-cl + the bundled libc++ __Cr ABI) and links the sealed v8.dll
    # import lib. This is how a Windows consumer must build (the decided contract:
    # clang-cl + libc++, Skia/Dawn-aligned), so it doubles as the consumable-ABI proof.
    # Identity-only here (the Skia-ICU coexistence partner is stubbed); coexistence on
    # Windows is added once Skia's win archive is wired the same way. The injected
    # target deps :v8_sealed_shared, so v8.dll.lib resolves v8::/v8::platform exports.
    WIN_VALIDATOR_MARKER = "# >>> v8-builder win identity validator (injected)"

    def inject_win_validator(self):
        if self.args.platform != "win":
            return
        build_gn = V8_DIR / "BUILD.gn"
        text = build_gn.read_text(encoding="utf-8")
        if self.WIN_VALIDATOR_MARKER in text:
            return
        # sources in the V8 root (gn sources are relative to //)
        shutil.copy2(SEAL_DIR.parent / "validate" / "identity_main.cpp",
                     V8_DIR / "v8_identity_main.cc")
        (V8_DIR / "v8_identity_stub.cc").write_text(
            '// stub: no Skia on the win validator yet (identity-only)\n'
            'extern "C" int v8builder_force_collision_partners() { return 0; }\n',
            encoding="utf-8")
        gn = (f'{self.WIN_VALIDATOR_MARKER}\n'
              'if (is_win && v8_monolithic) {\n'
              '  executable("v8_identity_validator") {\n'
              '    sources = [ "v8_identity_main.cc", "v8_identity_stub.cc" ]\n'
              '    include_dirs = [ "include" ]\n'
              '    deps = [ ":v8_sealed_shared" ]\n'
              # :external_config is V8\'s embedder-facing public config — it applies the
              # SAME feature defines V8 was built with (notably V8_COMPRESS_POINTERS, ON
              # for Windows). Without it the consumer compiles compression-OFF and
              # V8::Initialize aborts with an "embedder-vs-V8 mismatch" (CI 26995426061).
              '    configs += [ ":external_config" ]\n'
              f'    defines = [ "EXPECTED_V8_VERSION=\\"{self.tag}\\"" ]\n'
              '  }\n'
              '}\n')
        build_gn.write_text(text + "\n" + gn + "\n", encoding="utf-8")
        say("injected v8_identity_validator gn target")

    def validate_win_identity(self, out, arch):
        run(["ninja", "-C", str(out), "v8_identity_validator"], cwd=V8_DIR, env=self.env)
        exe = out / "v8_identity_validator.exe"
        if not exe.exists():
            raise SystemExit(f"expected validator {exe} not produced")
        # A cross-built arm64 validator can't RUN on the x64 CI runner. Build + seal still
        # happen here (the coff audit is static); the arm64 identity run happens on a native
        # arm64 Windows host (the local QEMU arm64 golden — see the win-local task / memory).
        host = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        if gn_cpu(arch) != host:
            # Bundle the cross-built validator + its co-located v8.dll into the uploaded
            # artifact (build/<platform>-<arch>/validate/) so a native arm64 Windows host
            # (the local QEMU golden) can run identity offline.
            vdir = BUILD_DIR / f"{self.args.platform}-{arch}" / "validate"
            vdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exe, vdir / exe.name)
            shutil.copy2(out / "v8.dll", vdir / "v8.dll")
            say(f"cross-build (target {gn_cpu(arch)} != host {host}): validator + v8.dll "
                f"bundled into {vdir.name}/ — run on a native {gn_cpu(arch)} Windows host",
                Colors.WARN)
            return
        # v8.dll is co-located in `out`, so the exe finds it on the default DLL search.
        say("running Windows identity validator (V8 init + eval + version)")
        run([str(exe)], cwd=out, env=self.env)

    # Android identity validator, built AS A GN TARGET (the inject_win_validator ELF
    # analog): an executable("v8_identity_validator") deps :v8_sealed_shared and compiles
    # with :external_config so it inherits V8's exact feature defines (compression OFF,
    # sandbox OFF) — otherwise V8::Initialize aborts on an embedder-vs-V8 mismatch.
    #
    # IMPORTANT (review addendum #2): a GN-target validator proves a *Chromium-toolchain*
    # consumer, NOT a stock-NDK/Pulp one — it is the SMOKE TEST, not the libc++-ABI gate.
    # The real ABI gate is the EXTERNAL NDK CMake consumer (validate/android/), which links
    # the PACKAGED headers + libv8.so with a stock NDK toolchain. This in-tree target just
    # proves V8 inits + evals on android-arm64 over an adb shell.
    ANDROID_VALIDATOR_MARKER = "# >>> v8-builder android identity validator (injected)"

    def inject_android_validator(self):
        if self.args.platform != "android":
            return
        build_gn = V8_DIR / "BUILD.gn"
        text = build_gn.read_text(encoding="utf-8")
        if self.ANDROID_VALIDATOR_MARKER in text:
            return
        shutil.copy2(SEAL_DIR.parent / "validate" / "identity_main.cpp",
                     V8_DIR / "v8_identity_main.cc")
        (V8_DIR / "v8_identity_stub.cc").write_text(
            '// stub: no Skia on the android validator (identity-only — no skia-builder\n'
            '// android asset). The seal + DT_NEEDED audits are the coexistence proof.\n'
            'extern "C" int v8builder_force_collision_partners() { return 0; }\n',
            encoding="utf-8")
        gn = (f'{self.ANDROID_VALIDATOR_MARKER}\n'
              'if (is_android && v8_monolithic) {\n'
              '  executable("v8_identity_validator") {\n'
              '    sources = [ "v8_identity_main.cc", "v8_identity_stub.cc" ]\n'
              '    include_dirs = [ "include" ]\n'
              '    deps = [ ":v8_sealed_shared" ]\n'
              '    configs += [ ":external_config" ]\n'
              f'    defines = [ "EXPECTED_V8_VERSION=\\"{self.tag}\\"" ]\n'
              '  }\n'
              '}\n')
        build_gn.write_text(text + "\n" + gn + "\n", encoding="utf-8")
        say("injected v8_identity_validator gn target (android)")

    def validate_android_identity(self, out, arch):
        run(["ninja", "-C", str(out), "v8_identity_validator"], cwd=V8_DIR, env=self.env)
        exe = out / "v8_identity_validator"
        if not exe.exists():
            raise SystemExit(f"expected validator {exe} not produced")
        libname = self.LIBNAME[self.args.platform]
        lib = out / libname
        # Always bundle the cross-built exe + co-located libv8.so into the artifact so an
        # arm64 emulator/device (Mac Studio) can run identity offline (the win-arm64-cross
        # pattern). An android-arm64 ELF can't run on the x86_64 build host.
        vdir = BUILD_DIR / f"{self.args.platform}-{arch}" / "validate"
        vdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, vdir / exe.name)
        shutil.copy2(lib, vdir / libname)
        # If an adb device/emulator matching the target abi is reachable, run it now.
        if not shutil.which("adb"):
            say(f"no adb in PATH: validator + {libname} bundled into {vdir.name}/ — "
                f"run on an arm64 android emulator/device", Colors.WARN)
            return
        abi_out = subprocess.run(
            ["adb", "shell", "getprop", "ro.product.cpu.abi"],
            capture_output=True, text=True)
        dev_abi = abi_out.stdout.strip()
        want_abi = {"arm64": "arm64-v8a", "x64": "x86_64",
                    "arm": "armeabi-v7a", "x86": "x86"}.get(arch, arch)
        if abi_out.returncode != 0 or not dev_abi:
            say(f"no adb device reachable: validator + {libname} bundled into "
                f"{vdir.name}/ — run on an arm64 android emulator/device", Colors.WARN)
            return
        if dev_abi != want_abi:
            say(f"adb device abi '{dev_abi}' != target '{want_abi}': cannot run "
                f"{want_abi} validator here; bundled into {vdir.name}/", Colors.WARN)
            return
        tmp = "/data/local/tmp/v8val"
        say(f"running android identity validator on device (abi {dev_abi})")
        run(["adb", "shell", f"rm -rf {tmp} && mkdir -p {tmp}"], env=self.env)
        run(["adb", "push", str(vdir / exe.name), f"{tmp}/{exe.name}"], env=self.env)
        run(["adb", "push", str(vdir / libname), f"{tmp}/{libname}"], env=self.env)
        run(["adb", "shell",
             f"cd {tmp} && chmod +x {exe.name} && "
             f"LD_LIBRARY_PATH={tmp} ./{exe.name}"], env=self.env)

    def run_all(self):
        # mac is proven (P1d). linux validates on a Linux CI runner; the Windows DLL lane
        # (dllexport seal + /WHOLEARCHIVE) validates on a Windows runner — both unprovable
        # on a macOS host. Do not report a lane validated until its OS workflow runs green.
        default_arch = {"mac": "arm64", "linux": "x64", "win": "x64",
                        "android": "arm64"}[self.args.platform]
        archs = (self.args.archs or default_arch).split(",")
        self.setup_depot_tools()
        self.fetch_v8()
        if not self.args.use_synced:
            self.sync_v8()
        else:
            say("--use-synced: building current checkout (skipping tag sync)", Colors.WARN)
        self.inject_seal_target()
        self.inject_win_validator()
        self.inject_android_validator()
        for arch in archs:
            out = self.gn_gen(arch)
            sealed = self.build_sealed(out, arch)
            self.package(sealed, arch)
            if self.args.platform == "win":
                self.validate_win_identity(out, arch)
            if self.args.platform == "android":
                self.validate_android_identity(out, arch)
        say("done", Colors.OK)


def main():
    p = argparse.ArgumentParser(description="Build & seal standalone V8 for embedding next to Skia/Dawn")
    p.add_argument("platform", choices=["mac", "linux", "win", "android"],
                   help="Target platform")
    p.add_argument("-archs", help="Comma-separated archs (e.g. arm64,x86_64 / x64; "
                                   "android: arm64,x64,arm,x86)")
    p.add_argument("-tag", dest="v8_version", help=f"V8 version tag (default {DEFAULT_V8_TAG})")
    p.add_argument("--no-seal", action="store_true", help="Skip sealing (debug only)")
    p.add_argument("--use-synced", action="store_true",
                   help="Build the currently-synced checkout instead of syncing to -tag")
    p.add_argument("--fetch-only", action="store_true", help="Only setup depot_tools + fetch/sync V8")
    p.add_argument("-ndk-api-level", dest="ndk_api_level", type=int, default=None,
                   help=f"Android min-SDK floor (default {ANDROID_DEFAULT_NDK_API_LEVEL})")
    p.add_argument("--android-ndk-libcxx", action="store_true",
                   help="Android: attempt the NDK-libc++ target ABI (use_custom_libcxx=false) "
                        "instead of the default bundled (__Cr) libc++. Needs a FULL NDK with a "
                        "standalone libunwind.a — the DEPS cipd android_toolchain lacks it.")
    args = p.parse_args()
    b = V8Build(args)
    if args.fetch_only:
        b.setup_depot_tools(); b.fetch_v8(); b.sync_v8(); say("fetch-only complete")
        return
    b.run_all()


if __name__ == "__main__":
    main()
