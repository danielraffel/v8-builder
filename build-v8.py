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


def ios_gn_args(arch, env="device", deployment_target="16.4", i18n=False):
    # iOS (jitless) cross-build on the Mac host — exploratory lane (#32).
    #
    # JITLESS IS AUTOMATIC, NOT HAND-SET. target_os=="ios" + target_platform=="iphoneos"
    # + use_blink==false (==!is_ios) drives v8_enable_lite_mode=true (gni/v8.gni:167-174),
    # which drives v8_jitless=true (BUILD.gn:439), which turns OFF TurboFan/Maglev/
    # Sparkplug/WebAssembly. We do NOT hand-set v8_enable_lite_mode / v8_jitless /
    # v8_enable_turbofan — those auto-derive correctly from target_os=ios + iphoneos.
    # v8_enable_webassembly IS set explicitly below, but ONLY to fix a host/target
    # toolchain skew (see the comment at that line), not to change the target posture —
    # the target value is already false via lite mode. Ignition (the bytecode interpreter)
    # stays ON, so the full ECMAScript language runs (enough for three.js; no WASM on iphoneos).
    #
    # INTL/i18n: common_gn_args() HARD-SETS v8_enable_i18n_support=true. Appending
    # `=false` would be a GN DUPLICATE-ASSIGNMENT error (the same reason win_gn_args
    # filters v8_enable_pointer_compression out of the common list before overriding).
    # So we FILTER it out first, then set the iOS value. Gate default is Intl OFF
    # (smaller binary, no ICU to seal, faster bring-up; Pulp uses no `Intl`); pass
    # i18n=True for a general-audience iOS artifact (the seal handles ICU the same way).
    base = [a for a in common_gn_args()
            if not a.startswith("v8_enable_i18n_support")]
    return base + [
        f'target_cpu="{gn_cpu(arch)}"',         # arm64 (device|sim) | x64 (intel sim)
        'target_os="ios"',
        f'target_environment="{env}"',          # REQUIRED — mobile_config.gni asserts
                                                #   it is one of device|simulator|catalyst.
        'target_platform="iphoneos"',           # selects the lite-mode (jitless) branch
        f'ios_deployment_target="{deployment_target}"',  # Pulp's iOS floor (16.4); V8
                                                #   default is 18.0 (ios_sdk_overrides.gni).
        'is_component_build=false',             # iOS doc + monolith both require non-component
        'use_custom_libcxx=false',              # platform libc++ (matches Skia/Dawn-on-iOS)
        f'v8_enable_i18n_support={"true" if i18n else "false"}',
        # HOST/TARGET LITE-MODE SKEW FIX (verified failure 2026-06-05): the iOS TARGET
        # auto-derives v8_enable_lite_mode=true from target_os=="ios" +
        # target_platform=="iphoneos" + use_blink==false (gni/v8.gni:167-174). But the
        # HOST snapshot toolchain (//build/toolchain/...:clang_arm64, which builds
        # `mksnapshot`/`torque` to RUN on the Mac) has target_os==host==mac, so it does
        # NOT auto-derive lite mode and builds a NON-lite, WASM-enabled mksnapshot. That
        # host mksnapshot then bakes a snapshot whose read-only/external-reference layout
        # is WASM-enabled, and the jitless TARGET runtime REJECTS it at deserialize time
        # (`Check failed: magic_number_ == SerializedData::kMagicNumber` in
        # ReadOnlyDeserializer, inside Isolate::New — a snapshot-format mismatch, NOT the
        # Abseil ODR). Setting v8_enable_lite_mode EXPLICITLY (a declare_arg, defaults "")
        # forces lite mode ON for BOTH host and target toolchains, so the snapshot the
        # host generates matches what the jitless target expects. This also drives
        # v8_jitless / turbofan-off / wasm-off consistently across toolchains.
        'v8_enable_lite_mode=true',
        # WASM stays explicitly off too. With lite_mode forced on both toolchains this is
        # now implied, but keep it explicit: it ALSO fixes a Torque host/target skew —
        # the wasm *.tq files (wasm-objects.tq, WasmFuncRef) are excluded from the target
        # Torque source set, and a non-lite host `torque` with -DV8_ENABLE_WEBASSEMBLY
        # would @if(V8_ENABLE_WEBASSEMBLY)==true and abort `cannot find "WasmFuncRef"`
        # (base.tq:1099). Off for both toolchains keeps host torque's @if matching the
        # target's excluded sources.
        'v8_enable_webassembly=false',
        # v8_enable_pointer_compression stays false (inherited from common) — matches the
        # desktop D3 OFF posture and the V8 iOS doc.
        # v8_monolithic_for_shared_library=true (from common) IS correct here: the iOS
        # artifact is a sealed dynamic FRAMEWORK (a shared lib in a bundle), so the
        # TLS-model define it sets is the right one for the -shared link.
    ]


def linux_gn_args(arch):
    return common_gn_args() + [
        f'target_cpu="{gn_cpu(arch)}"',         # x64 | arm64
        'target_os="linux"',
        # V8's bundled Linux host toolchain (clang + Rust) is x86_64-only — there is no
        # aarch64-unknown-linux-gnu rustlib in third_party/rust-toolchain. On an arm64 build
        # MACHINE (the Rosetta VM that hosts the linux/arm64 CI cell) gn would default
        # host_cpu=arm64 and try to build arm64 Rust host tools, which fails. Pin host_cpu=x64
        # so gn uses the x86_64 host toolchain (run under Rosetta on the arm64 VM); target_cpu
        # still cross-compiles to arm64. On a native x86_64 runner this is a no-op. Same fix
        # as android_gn_args.
        'host_cpu="x64"',
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


def platform_gn_args(platform, arch, args=None, ios_env="device",
                     ios_deployment_target="16.4", ios_i18n=False):
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
    if platform == "ios":
        return ios_gn_args(arch, env=ios_env,
                           deployment_target=ios_deployment_target, i18n=ios_i18n)
    raise SystemExit(f"gn args for platform '{platform}' not implemented")


# Injected into V8's BUILD.gn. Proven on macOS (P1c); the ELF branch is the Linux
# analog (version-script + --whole-archive), validated on CI (unprovable on macOS).
SEAL_TARGET_GN = '''\
# >>> v8-builder sealed-shared target (injected)
if ((is_mac || is_ios || is_linux || is_win || is_android) && v8_monolithic) {
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
    if (is_mac || is_ios) {
      # iOS Mach-O seals IDENTICALLY to macOS Mach-O: the export-table seal is
      # -exported_symbols_list (only v8::/cppgc:: leave the dynamic export table;
      # ICU/zlib/Abseil pulled via -force_load stay INTERNAL). This is the WHOLE point
      # of the framework route over a static .a (which has no export table at all and
      # would re-merge V8's Abseil into the final app link, recreating the ODR abort).
      # The .dylib emitted here is wrapped into a sealed .framework bundle at package
      # time (package_ios_framework). @rpath/V8.framework/V8 is the framework-shaped
      # install name; the plain @rpath/libv8.dylib name is kept for the macOS dylib.
      inputs = [ "v8_embedder_exports.txt" ]
      ldflags = [
        "-Wl,-exported_symbols_list," + rebase_path("v8_embedder_exports.txt", root_build_dir),
        "-Wl,-force_load," + rebase_path("$root_build_dir/obj/libv8_monolith.a", root_build_dir),
      ]
      if (is_ios) {
        ldflags += [ "-Wl,-install_name,@rpath/V8.framework/V8" ]
      } else {
        ldflags += [ "-Wl,-install_name,@rpath/libv8.dylib" ]
      }
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


# >>> v8-builder Windows __Cr libc++ static lib (task #17) -----------------------
# A Windows consumer that uses iostreams (e.g. choc's V8 wrapper, which Pulp's
# js_v8_engine.cpp uses) needs an out-of-line libc++ runtime, because the sealed
# v8.dll exports ZERO libc++ (no std::cout, no basic_string/ostream ctors, no
# operator new — they're all internal to the DLL). The official Windows LLVM
# package ships no __Cr-ABI libc++, so the consumer would have to hand-build one.
#
# V8 ALREADY compiles its bundled libc++ from //buildtools/third_party/libc++
# (and libc++abi) with the exact __Cr ABI (`_LIBCPP_ABI_NAMESPACE=__Cr`), the
# exact clang-cl toolchain, and the exact flags v8.dll was built with —
# including the _LIBCPP_HAS_NO_... posture and the vcruntime C++ ABI. Those
# object files already exist in the gn out dir after the normal v8_monolith
# build (the same objects folded into v8.dll). So instead of running a SECOND,
# drift-prone CMake build of llvm-project's `runtimes` by hand (the Pulp #27
# recipe: LIBCXX_ABI_NAMESPACE=__Cr, LIBCXX_CXX_ABI=vcruntime, RTTI + exceptions
# ON, static), we archive V8's OWN already-built `.obj` files directly into a
# `libc++.lib` with V8's bundled `llvm-lib`. This is byte-for-byte the same ABI
# as v8.dll.lib's `...@__Cr@std@@` exports BY CONSTRUCTION — same compiler
# invocation, no second-toolchain skew to maintain.
#
# An earlier approach injected a top-level GN static_library that deps
# `//buildtools/third_party/libc++`, but GN visibility rejects it: libc++ only
# permits deps from a restricted list, not an injected top-level target
# (BUILDCONFIG.gn:571 "Dependency not allowed"). Archiving the already-compiled
# objects sidesteps GN entirely — no new target, no visibility constraint.
#
# The objects link against `msvcprt.lib` and were compiled with
# `_CRT_STDIO_ISO_WIDE_SPECIFIERS=1` (V8's own libc++ config sets it), so a
# consumer that links the same marker dodges any lld-link /FAILIFMISMATCH.

# gn out-dir subtrees holding the already-compiled __Cr libc++/libc++abi objects.
# libc++abi carries the ABI runtime (typeinfo, __cxa_*, vtable thunks) an
# iostreams consumer also pulls in, so we archive both.
WIN_LIBCXX_OBJ_SUBDIRS = (
    "obj/buildtools/third_party/libc++",
    "obj/buildtools/third_party/libc++abi",
)


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
        gn_blocks = "\n" + SEAL_TARGET_GN + "\n"
        build_gn.write_text(text + gn_blocks, encoding="utf-8")
        say("injected v8_sealed_shared gn target")

    def _cell_id(self, arch):
        # iOS has TWO axes (env + arch) so its output/dist dir is ios-<env>-<arch>
        # (e.g. ios-simulator-arm64). Other platforms keep <platform>-<arch>.
        if self.args.platform == "ios":
            return f"ios-{self.args.ios_env}-{arch}"
        return f"{self.args.platform}-{arch}"

    def gn_gen(self, arch):
        out = V8_DIR / "out" / self._cell_id(arch)
        args_gn = "\n".join(platform_gn_args(
            self.args.platform, arch, self.args,
            ios_env=getattr(self.args, "ios_env", "device"),
            ios_deployment_target=getattr(self.args, "ios_deployment_target", "16.4"),
            ios_i18n=getattr(self.args, "ios_i18n", False))) + "\n"
        out.mkdir(parents=True, exist_ok=True)
        (out / "args.gn").write_text(args_gn, encoding="utf-8")
        say(f"args.gn:\n{args_gn}")
        run(["gn", "gen", str(out)], cwd=V8_DIR, env=self.env)
        return out

    # iOS emits a dylib (output_name="v8" → libv8.dylib) that we wrap into V8.framework.
    LIBNAME = {"mac": "libv8.dylib", "linux": "libv8.so", "win": "v8.dll",
               "android": "libv8.so", "ios": "libv8.dylib"}
    SEAL_BACKEND = {"mac": "macho.py", "linux": "elf.py", "win": "coff.py",
                    "android": "elf.py", "ios": "macho.py"}

    def build_sealed(self, out, arch):
        run(["ninja", "-C", str(out), "v8_sealed_shared"], cwd=V8_DIR, env=self.env)
        libname = self.LIBNAME[self.args.platform]
        lib = out / libname
        if not lib.exists():
            raise SystemExit(f"expected sealed {lib} not produced")
        dest = BUILD_DIR / self._cell_id(arch) / "lib"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib, dest / libname)
        if self.args.platform == "win":
            # ship the import lib the consumer links against (v8.dll.lib)
            implib = out / "v8.dll.lib"
            if implib.exists():
                shutil.copy2(implib, dest / "v8.dll.lib")
            else:
                raise SystemExit(f"expected import lib {implib} not produced")
        produced = dest / libname
        # audit: assert 0 absl/icu/zlib internals exported. The seal is an export-table
        # property of the dylib itself, so we audit the dylib (the framework just rehouses
        # this exact binary as V8.framework/V8 — same Mach-O, same export table). Capture
        # the export count as the pre-strip baseline for the strip-preserves-the-seal gate.
        pre_strip_exports = self._seal_audit(produced, label="pre-strip")
        # Strip debug + local symbols (symbol_level=1 leaves a large .symtab + minimal debug).
        # THE SEAL PROPERTY IS "0 ICU/zlib/Abseil exported", not "export count frozen": the
        # post-strip _seal_audit() re-asserts 0 leaks + v8-exports-present (it hard-fails
        # otherwise), and THAT is the seal gate. The export COUNT is allowed to DROP, because
        # strip prunes unreferenced dynamic-table entries: on ELF the version script exports
        # every `v8::*` global, but ~half are dead `v8::internal::` symbols no external
        # consumer relocates against; strip removes them, landing linux at ~66k exports — the
        # SAME public surface Mach-O's linker dead-strips to (mac ≈ 68k). A real ABI break
        # (a *needed* export removed) is caught downstream by the identity/coexistence
        # validation, which links + inits + evals against the stripped binary. We only abort
        # on an INCREASE (strip can never add exports → corruption) or a leak (audit above).
        self._strip_lib(produced)
        post_strip_exports = self._seal_audit(produced, label="post-strip")
        if post_strip_exports > pre_strip_exports:
            say(f"STRIP CORRUPTED THE EXPORT TABLE — count rose {pre_strip_exports} -> "
                f"{post_strip_exports}; strip can only remove symbols. Aborting.", Colors.FAIL)
            raise SystemExit(1)
        if post_strip_exports < pre_strip_exports:
            say(f"strip pruned {pre_strip_exports - post_strip_exports} unreferenced dynamic "
                f"exports ({pre_strip_exports} -> {post_strip_exports}); seal intact "
                f"(0 leaks, v8 exports present) — {produced.name}", Colors.OK)
        else:
            say(f"strip preserved the export seal: {post_strip_exports} exports "
                f"unchanged ({produced.name})", Colors.OK)
        if self.args.platform == "android":
            self._android_dt_needed_audit(produced)
        if self.args.platform == "ios":
            produced = self.wrap_ios_framework(dest, produced, arch)
        return produced

    # Build + stage + verify the __Cr-ABI libc++.lib (task #17). No-op off Windows.
    #
    # The sealed v8.dll exports ZERO out-of-line libc++ runtime, so a Windows consumer
    # using iostreams (choc's V8 wrapper) must supply its own __Cr-ABI libc++ — which
    # the official LLVM Windows package does not ship. We archive V8's OWN bundled
    # libc++/libc++abi OBJECT FILES (same clang-cl, same _LIBCPP_ABI_NAMESPACE=__Cr,
    # same flags v8.dll used) into lib/libc++.lib, so the ABI matches v8.dll.lib's
    # `...@__Cr@std@@` exports BY CONSTRUCTION. Staged next to v8.dll + v8.dll.lib.
    #
    # No GN target is injected: a top-level static_library that deps
    # //buildtools/third_party/libc++ is rejected by GN visibility (libc++'s
    # visibility list excludes injected targets). The v8_monolith build already
    # compiled these objects into the out dir, so we glob and archive THOSE directly.
    def build_win_libcxx(self, out, arch):
        if self.args.platform != "win":
            return None
        objs = self._find_win_libcxx_objs(out)
        if not objs:
            raise SystemExit(
                "expected compiled __Cr libc++/libc++abi .obj files under "
                f"{out}/obj/buildtools/third_party/ (libc++, libc++abi) — found none. "
                "The v8_monolith build should have compiled them.")
        say(f"found {len(objs)} __Cr libc++/libc++abi object(s) under {out}/obj")
        dest = BUILD_DIR / self._cell_id(arch) / "lib"
        dest.mkdir(parents=True, exist_ok=True)
        staged = dest / "libc++.lib"
        self._archive_objs(objs, staged)
        self._verify_cr_libcxx(staged, arch)
        say(f"staged __Cr libc++.lib ({len(objs)} objs) -> {staged}")
        return staged

    # Glob the already-compiled libc++ + libc++abi .obj files V8 produced for the
    # v8_monolith build. Primary path is out/obj/buildtools/third_party/{libc++,
    # libc++abi}/**/*.obj (the native-toolchain layout). gn nests the objects one
    # level deeper (.../libc++/libc++/*.obj) and a SECONDARY-toolchain build lands
    # them under out/<toolchain>/obj/... (observed on the cross iOS lane), so if the
    # primary path is empty, fall back to an rglob over the WHOLE out dir for any
    # obj/buildtools/third_party/{libc++,libc++abi} subtree.
    def _find_win_libcxx_objs(self, out):
        objs = []
        for sub in WIN_LIBCXX_OBJ_SUBDIRS:
            base = out / sub
            if base.is_dir():
                objs.extend(base.rglob("*.obj"))
        if not objs:
            # Secondary-toolchain or deeper layout: scan the whole out tree for any
            # libc++/libc++abi dir that sits under an obj/buildtools/third_party path.
            for d in out.rglob("*"):
                if (d.is_dir() and d.name in ("libc++", "libc++abi")
                        and "buildtools" in d.parts and "third_party" in d.parts
                        and "obj" in d.parts):
                    objs.extend(d.rglob("*.obj"))
        # Dedupe (nested rglob can double-count) and order deterministically.
        return sorted(set(objs))

    # Archive the object files into a TRUE static lib with V8's bundled llvm-lib
    # (so the archive carries the SAME __Cr objects v8.dll was built from). Prefer
    # the V8-bundled llvm-lib.exe; fall back to a PATH-resolved llvm-lib / lib.exe.
    def _archive_objs(self, objs, staged):
        if staged.exists():
            staged.unlink()
        archiver = self._win_archiver()
        # llvm-lib / lib.exe share the MSVC librarian CLI: /OUT:<lib> <objs...>.
        cmd = [archiver, f"/OUT:{staged}"] + [str(o) for o in objs]
        run(cmd, cwd=V8_DIR, env=self.env)
        if not staged.exists():
            raise SystemExit(f"archiver {archiver} did not produce {staged}")

    # Locate the MSVC-style librarian. V8 bundles llvm-lib at
    # third_party/llvm-build/Release+Asserts/bin/llvm-lib.exe (same toolchain that
    # compiled the objects). Fall back to a PATH llvm-lib or MSVC lib.exe.
    def _win_archiver(self):
        bundled = V8_DIR / "third_party/llvm-build/Release+Asserts/bin/llvm-lib.exe"
        if bundled.exists():
            return str(bundled)
        alt = V8_DIR / "third_party/llvm-build/Release+Asserts/bin/llvm-lib"
        if alt.exists():
            return str(alt)
        for name in ("llvm-lib", "lib.exe", "lib"):
            found = shutil.which(name)
            if found:
                say(f"using PATH archiver {found} (no bundled llvm-lib)", Colors.WARN)
                return found
        raise SystemExit(
            "no archiver found: bundled llvm-lib.exe absent and no llvm-lib/lib.exe "
            "on PATH — cannot build libc++.lib")

    # Hard-gate: the archive MUST carry the __Cr ABI namespace (mangled `@__Cr@std@@`),
    # matching v8.dll.lib's exports. A plain MSVC-STL or stock-LLVM libc++ would carry
    # `@std@@` WITHOUT the __Cr inline-namespace tag and silently mis-link against v8.dll.
    # We grep the archive's symbol table. Prefer llvm-nm/llvm-ar (V8-bundled, reads COFF
    # archives on any host, so this gate runs even on a cross-built arm64 .lib produced on
    # the x64 runner); fall back to `dumpbin /SYMBOLS` when only MSVC tools are present.
    def _verify_cr_libcxx(self, lib, arch):
        text = self._archive_symbols(lib)
        if "@__Cr@std@@" not in text:
            # Surface a sample of std:: symbols to make a regression diagnosable.
            sample = "\n".join(l for l in text.splitlines()
                               if "std@@" in l or "basic_string" in l)[:1500]
            say(f"FAIL: {lib.name} carries NO __Cr-ABI symbols (@__Cr@std@@). It does "
                f"NOT match v8.dll.lib's ABI — a consumer would mis-link.\n"
                f"sample std symbols:\n{sample}", Colors.FAIL)
            raise SystemExit(1)
        say(f"verified {lib.name} carries the __Cr ABI (@__Cr@std@@ present) — "
            f"matches v8.dll.lib", Colors.OK)

    # Dump an archive's symbol table to text. llvm-nm reads COFF archives on any host
    # (so an arm64 cross-built .lib audits on the x64 runner); dumpbin is the MSVC
    # fallback. Returns the combined stdout; raises if neither tool can read the archive.
    def _archive_symbols(self, lib):
        llvm_nm = V8_DIR / "third_party/llvm-build/Release+Asserts/bin/llvm-nm.exe"
        if not llvm_nm.exists():
            alt = V8_DIR / "third_party/llvm-build/Release+Asserts/bin/llvm-nm"
            llvm_nm = alt if alt.exists() else None
        if llvm_nm is not None:
            proc = subprocess.run([str(llvm_nm), str(lib)],
                                  capture_output=True, text=True, env=self.env)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
            say(f"llvm-nm could not read {lib.name} ({proc.stderr.strip()}); "
                f"trying dumpbin", Colors.WARN)
        # MSVC dumpbin fallback (resolved via the shell so PATH/.bat semantics apply).
        proc = subprocess.run("dumpbin /SYMBOLS " + subprocess.list2cmdline([str(lib)]),
                              capture_output=True, text=True, env=self.env, shell=True)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        raise SystemExit(
            f"could not read symbols from {lib} (no usable llvm-nm or dumpbin): "
            f"{proc.stderr.strip()}")

    # Run the seal backend's `audit` over `lib` and return the integer export count it
    # reports ("AUDIT OK — <N> exports ..."). A non-zero exit (any seal leak / no exports)
    # propagates as a hard failure via run()'s check=True semantics, so reaching the parse
    # already means the seal passed. The count is the strip-didn't-touch-.dynsym witness.
    def _seal_audit(self, lib, label=""):
        backend = SEAL_DIR / self.SEAL_BACKEND[self.args.platform]
        cmd = [sys.executable, str(backend), "audit", "--lib", str(lib),
               "--policy", str(SEAL_DIR / "policy.json")]
        say("$ " + " ".join(map(str, cmd)) + (f"   [{label}]" if label else ""),
            Colors.WARN)
        proc = subprocess.run(cmd, env=self.env, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            raise SystemExit(f"seal audit failed for {lib} ({label})")
        m = re.search(r"AUDIT OK — (\d+) exports", proc.stdout)
        if not m:
            raise SystemExit(f"could not parse export count from seal audit of {lib}")
        return int(m.group(1))

    # Strip LOCAL + debug symbols, preserving the DYNAMIC export table (= the seal).
    #   Mach-O (mac/ios): `strip -x` removes non-global (local) symbols, keeps external/
    #     exported. The codesignature (if any) is invalidated by the edit, so re-sign
    #     ad-hoc (`codesign -f -s -`) — required before a sealed dylib/framework loads.
    #   ELF (linux/android): plain `llvm-strip` (default = strip-all) drops .symtab + debug
    #     but CANNOT remove the *allocated* .dynsym (the export table the dynamic linker
    #     needs at runtime) — so the seal is preserved byte-for-byte. Do NOT use
    #     `--strip-unneeded`: it PRUNES .dynsym (drops exports it thinks are internally
    #     unused), which on libv8.so cut 136414 -> 68207 exports and tripped the seal gate
    #     (CI 27055798398). Android uses the NDK llvm-strip from the DEPS NDK.
    #   Windows (PE): the .dll carries no embedded debug (PDB is separate and unshipped),
    #     so there is nothing to strip — no-op.
    def _strip_lib(self, lib):
        plat = self.args.platform
        before = lib.stat().st_size
        if plat in ("mac", "ios"):
            run(["strip", "-x", str(lib)], env=self.env)
            # Re-sign ad-hoc: `strip` rewrites the Mach-O and invalidates any existing
            # (incl. linker-applied ad-hoc) signature; an unsigned/invalid sealed dylib
            # won't load on recent macOS/iOS. `-f` forces, `-s -` is the ad-hoc identity.
            cs = subprocess.run(["codesign", "-f", "-s", "-", str(lib)],
                                env=self.env, capture_output=True, text=True)
            if cs.returncode != 0:
                say(f"codesign re-sign after strip failed: {cs.stderr.strip()}",
                    Colors.FAIL)
                raise SystemExit(1)
        elif plat in ("linux", "android"):
            stripper = self._llvm_strip()
            run([stripper, str(lib)], env=self.env)  # default strip-all: drops .symtab+debug, keeps allocated .dynsym
        elif plat == "win":
            say("win: PE dll carries no embedded debug (PDB is separate, unshipped) — "
                "no strip needed", Colors.WARN)
            return
        after = lib.stat().st_size
        say(f"stripped {lib.name}: {before/1e6:.1f} MB -> {after/1e6:.1f} MB "
            f"({100*(before-after)/before:.0f}% smaller)", Colors.OK)

    # Resolve a stripper that can handle the target ELF. The host GNU `strip` on the x86_64
    # CI runner CANNOT strip the cross-compiled android arm64 .so ("Unable to recognise the
    # format of the input file"), so prefer an llvm strip that handles every arch:
    #   1. the V8-BUNDLED llvm-strip (third_party/llvm-build/...) — always present in the
    #      checkout and is the exact toolchain that built the lib, so it reads any target;
    #   2. the DEPS NDK's llvm-strip (android);
    #   3. PATH llvm-strip;
    #   4. host GNU `strip` — native linux ONLY (it can't do the cross android .so).
    def _llvm_strip(self):
        bundled = V8_DIR / "third_party/llvm-build/Release+Asserts/bin/llvm-strip"
        if bundled.exists():
            return str(bundled)
        if self.args.platform == "android":
            ndk = V8_DIR / "third_party" / "android_toolchain" / "ndk"
            hits = sorted(ndk.glob(
                "toolchains/llvm/prebuilt/*/bin/llvm-strip")) if ndk.exists() else []
            if hits:
                return str(hits[0])
            say("bundled + NDK llvm-strip not found; falling back to PATH llvm-strip "
                "(host GNU strip cannot strip the android arm64 .so)", Colors.WARN)
        cands = ("llvm-strip",) if self.args.platform == "android" else ("llvm-strip", "strip")
        for cand in cands:
            if shutil.which(cand):
                return cand
        raise SystemExit("no arch-capable llvm-strip found for the ELF strip")

    def wrap_ios_framework(self, dest, dylib, arch):
        # Wrap the sealed dylib into a flat (iOS) V8.framework bundle:
        #   V8.framework/V8            (the sealed Mach-O — install_name @rpath/V8.framework/V8)
        #   V8.framework/Headers/      (V8 public headers)
        #   V8.framework/Info.plist    (minimal CFBundle keys the loader/codesign expect)
        # Flat layout (binary at the bundle root, no Versions/ symlink dir) is the iOS/
        # tvOS convention; Versions/A is macOS-only. A loose .dylib is awkward to embed and
        # codesign on iOS; a framework bundle is the blessed embeddable dynamic shape.
        fw = dest.parent / "V8.framework"
        if fw.exists():
            shutil.rmtree(fw)
        fw.mkdir(parents=True)
        shutil.copy2(dylib, fw / "V8")
        # Headers (filtered: *.h/*.inc only — the framework's Headers/ is the consumer's
        # public include root, so the same DEPS/OWNERS/*.md/*.json/*.pdl cruft filter as
        # the loose include/ applies).
        self._copy_headers(V8_DIR / "include", fw / "Headers")
        # Minimal Info.plist
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>CFBundleIdentifier</key><string>org.v8.V8</string>\n'
            '  <key>CFBundleName</key><string>V8</string>\n'
            '  <key>CFBundleExecutable</key><string>V8</string>\n'
            '  <key>CFBundlePackageType</key><string>FMWK</string>\n'
            f'  <key>MinimumOSVersion</key>'
            f'<string>{getattr(self.args, "ios_deployment_target", "16.4")}</string>\n'
            '  <key>CFBundleSupportedPlatforms</key><array><string>'
            + ('iPhoneSimulator' if self.args.ios_env == 'simulator' else 'iPhoneOS')
            + '</string></array>\n'
            '</dict></plist>\n')
        (fw / "Info.plist").write_text(plist, encoding="utf-8")
        # Re-audit the framework binary (same already-stripped Mach-O re-housed as
        # V8.framework/V8 — same export table; cheap belt-and-suspenders).
        self._seal_audit(fw / "V8", label="framework")
        say(f"wrapped sealed dylib into {fw}")
        return fw

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

    def _built_v8_version(self):
        # The manifest's v8_version must reflect what was ACTUALLY built, not the
        # requested --tag / DEFAULT_V8_TAG (which can drift from the checkout — e.g.
        # the iOS lane builds the LKGR 15.1.27 checkout while the desktop default pin
        # is still 14.6). Resolve the exact tag at the built SHA; fall back to the
        # requested tag only if the checkout has no matching tag.
        try:
            tag = subprocess.run(
                ["git", "describe", "--tags", "--exact-match", "HEAD"],
                cwd=V8_DIR, capture_output=True, text=True).stdout.strip()
            if tag:
                return tag
        except Exception:
            pass
        return self.tag

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

    # Copy V8 public headers into `dst`, filtering out non-header repo metadata. Only
    # *.h / *.inc are part of the consumable include surface; DEPS / OWNERS / DIR_METADATA
    # / *.md / *.json / *.pdl are V8-repo bookkeeping that a downstream embedder never
    # includes. (shutil, not rsync — rsync isn't on Windows runners.)
    def _copy_headers(self, src, dst):
        if dst.exists():
            shutil.rmtree(dst)
        kept = 0
        for f in src.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".h", ".inc"):
                continue
            rel = f.relative_to(src)
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            kept += 1
        say(f"copied {kept} headers (*.h/*.inc only) into {dst}")

    def package(self, sealed, arch):
        dest = BUILD_DIR / self._cell_id(arch)
        is_ios = self.args.platform == "ios"
        # Headers: every platform EXCEPT iOS ships a top-level include/. iOS embeds its
        # headers inside V8.framework/Headers/ (done in wrap_ios_framework), so a duplicate
        # top-level include/ would be dead weight — skip it.
        if not is_ios:
            self._copy_headers(V8_DIR / "include", dest / "include")
        manifest = {
            "v8_version": self._built_v8_version(),
            "platform": self.args.platform,
            "arch": arch,
            # On iOS, Intl defaults OFF for the gate (no ICU to seal); other platforms
            # build Intl ON. Reflect the real build posture, not a hardcoded true.
            "i18n": bool(getattr(self.args, "ios_i18n", False)) if is_ios else True,
            "shared": True,
            "sealed": not self.args.no_seal,
            # Relative path (from the artifact root) to the primary consumable binary.
            # Android lays the .so out the idiomatic way (jniLibs/<abi>/libv8.so) and ships
            # no lib/ duplicate; iOS ships the framework; the rest ship lib/<name>.
            "lib": self._manifest_lib_path(sealed, arch),
            # FR1 pairing contract (LKGR triple + this_artifact + built_revision):
            "pair": self._lkgr_contract(),
        }
        if self.args.platform == "win":
            # v8.dll exports zero out-of-line libc++; a consumer using iostreams must link
            # the __Cr-ABI libc++.lib we ship alongside (task #17). Record it + the ABI so
            # a consumer knows which STL/ABI to match. import_lib is the existing
            # v8::-surface import library.
            manifest["libcxx"] = "bundled-chromium-__Cr"
            manifest["import_lib"] = "lib/v8.dll.lib"
            cxx = dest / "lib" / "libc++.lib"
            if cxx.exists():
                manifest["libcxx_lib"] = "lib/libc++.lib"
                manifest["libcxx_note"] = (
                    "static __Cr-ABI libc++ for iostream/std consumers (e.g. choc's V8 "
                    "wrapper); link alongside msvcprt.lib. v8.dll exports no out-of-line "
                    "libc++ runtime. ABI matches v8.dll.lib (@__Cr@std@@).")
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
            # consumer can drop the artifact straight into src/main/jniLibs. This is the
            # ONLY copy of the .so we ship — drop the lib/ duplicate build_sealed staged
            # (the .so was identical, so the released artifact carried it twice).
            jni = dest / "jniLibs" / manifest["abi"]
            jni.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sealed, jni / Path(sealed).name)
            lib_dir = dest / "lib"
            if lib_dir.exists():
                shutil.rmtree(lib_dir)
        elif is_ios:
            # iOS-specific build-shape fields (review addendum point 5/6): a consumer must
            # know it's a jitless, no-WASM, sealed framework before it links.
            manifest.update({
                "ios_environment": self.args.ios_env,      # device | simulator
                "deployment_target": getattr(self.args, "ios_deployment_target", "16.4"),
                "jitless": True,         # auto-derived from target_os=ios + iphoneos
                "wasm": False,           # no JIT ⇒ no WebAssembly on iphoneos
                "form": "framework",     # V8.framework (sealed dynamic), not static .a
            })
            # The shipped form is V8.framework (binary + embedded Headers/). Drop the loose
            # lib/ that build_sealed staged the dylib into before wrapping — the framework
            # already embeds that exact sealed Mach-O as V8.framework/V8.
            lib_dir = dest / "lib"
            if lib_dir.exists():
                shutil.rmtree(lib_dir)
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        say(f"packaged {dest}")

    # Relative path (from the artifact root) to the primary consumable binary, recorded in
    # the manifest so a pairing consumer can locate the lib without guessing the layout.
    def _manifest_lib_path(self, sealed, arch):
        plat = self.args.platform
        if plat == "android":
            abi = {"arm64": "arm64-v8a", "x64": "x86_64",
                   "arm": "armeabi-v7a", "x86": "x86"}.get(arch, arch)
            return f"jniLibs/{abi}/{Path(sealed).name}"
        if plat == "ios":
            return "V8.framework/V8"
        return f"lib/{Path(sealed).name}"

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

    def _link_android_consumer(self, out, arch):
        # Drive validate/android/link_consumer.sh: link the EXTERNAL consumer against the
        # packaged sealed libv8.so + a Chromium-style __Cr libc++. A non-zero exit here is
        # a hard ABI-gate FAIL (no skip-pass). Only arm64 today; other ABIs add a sysroot
        # multilib path the script already parameterizes by target triple.
        script = SEAL_DIR.parent / "validate" / "android" / "link_consumer.sh"
        if not script.exists():
            say("WARN: validate/android/link_consumer.sh missing — skipping ABI gate",
                Colors.WARN)
            return
        pkg = BUILD_DIR / f"{self.args.platform}-{arch}"
        level = str(self.args.ndk_api_level or ANDROID_DEFAULT_NDK_API_LEVEL)
        say("android libc++-ABI gate: linking external __Cr consumer vs packaged libv8.so")
        run(["bash", str(script), str(V8_DIR), str(out), str(pkg), level], env=self.env)

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
        # The libc++-ABI GATE (review addendum #2): build the EXTERNAL consumer against
        # the PACKAGED libv8.so with a Chromium-style __Cr libc++ (the Windows-model
        # contract, since libv8.so ships V8's bundled __Cr libc++). A clean link proves
        # the std:: public surface (v8::platform's std::unique_ptr) is consumable; the
        # in-tree gn validator above is only the Chromium-toolchain smoke test.
        self._link_android_consumer(out, arch)
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
                        "android": "arm64", "ios": "arm64"}[self.args.platform]
        archs = (self.args.archs or default_arch).split(",")
        self.setup_depot_tools()
        self.fetch_v8()
        if not self.args.use_synced:
            self.sync_v8()
        elif self.args.platform == "android":
            # Android's NDK is gated on checkout_android, which only sync pulls. A bare
            # --use-synced (e.g. CI building the fetched tip without a -tag) would leave
            # third_party/android_toolchain absent and gn can't resolve the android
            # toolchain. So on android we ALWAYS enable the gate + sync deps even under
            # --use-synced (we just don't re-checkout a tag — sync the current HEAD's deps).
            say("--use-synced + android: syncing deps with checkout_android (NDK gate)",
                Colors.WARN)
            self._enable_android_checkout()
            run(["gclient", "sync", "-D", "--force", "--reset"],
                cwd=SRC_DIR, env=self.env)
        else:
            say("--use-synced: building current checkout (skipping tag sync)", Colors.WARN)
        self.inject_seal_target()
        self.inject_win_validator()
        self.inject_android_validator()
        for arch in archs:
            out = self.gn_gen(arch)
            sealed = self.build_sealed(out, arch)
            # Windows: stage the __Cr libc++.lib into lib/ BEFORE package() so the
            # manifest records it (task #17). No-op off Windows.
            self.build_win_libcxx(out, arch)
            self.package(sealed, arch)
            if self.args.platform == "win":
                self.validate_win_identity(out, arch)
            if self.args.platform == "android":
                self.validate_android_identity(out, arch)
        say("done", Colors.OK)


def main():
    p = argparse.ArgumentParser(description="Build & seal standalone V8 for embedding next to Skia/Dawn")
    p.add_argument("platform", choices=["mac", "linux", "win", "android", "ios"],
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
    # iOS-only knobs (exploratory jitless lane, #32).
    p.add_argument("--ios-env", choices=["device", "simulator"], default="device",
                   help="iOS target_environment (device|simulator). Default device.")
    p.add_argument("--ios-deployment-target", default="16.4",
                   help="iOS minimum OS (Pulp floor 16.4; V8 default is 18.0)")
    p.add_argument("--ios-i18n", action="store_true",
                   help="Build iOS with Intl/ICU ON (default OFF for the gate)")
    args = p.parse_args()
    b = V8Build(args)
    if args.fetch_only:
        b.setup_depot_tools(); b.fetch_v8(); b.sync_v8(); say("fetch-only complete")
        return
    b.run_all()


if __name__ == "__main__":
    main()
