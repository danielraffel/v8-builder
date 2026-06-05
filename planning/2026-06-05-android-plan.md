# v8-builder — Android (NDK) lane plan

**Status:** Draft (grounded against real code + a synced V8 15.1.27 checkout on the
Linux VM `192.168.64.7`). Not yet built. Companion to `planning/v8-builder-proposal.md`
(§11 phase 6) and `planning/v8-builder-runbook.md` (P6).
**Date:** 2026-06-05
**Bottom line:** Android arm64 is the **lowest-effort new lane** because it reuses the
already-green Linux **ELF seal** (`seal/elf.py`, unchanged) and the already-required
**x86_64-Linux build host**. It is a *cross-compile* (`target_os="android"`) from the
same host that builds Linux, so it runs **fully in parallel with the iOS (mac) lane** —
different hosts, no shared state.

---

## 1. GN args for Android

Android is `target_os="android"` with `target_cpu` per ABI. Primary `arm64`
(`arm64-v8a`); also `x64` (emulator), `arm` (`armeabi-v7a`), `x86`.

**How V8 gets the NDK — gclient/DEPS, gated on `checkout_android`.** Verified in the
synced checkout's `DEPS`:

```
DEPS:
  vars:  'android_ndk_version': Str('2@30.0.14608247')
  deps:  'third_party/android_toolchain/ndk': {  # cipd package
            'package': 'chromium/third_party/android_toolchain/android_toolchain',
            'version': 'version:' + Var('android_ndk_version'),
            'condition': 'checkout_android',     # <-- the gate
          }
  + third_party/android_sdk/public/* (build-tools/emulator/platform-tools/platforms-37/cmdline-tools), all 'condition': 'checkout_android'
```

`build/config/android/config.gni` then points gn at that fetched NDK — **no
`android_ndk_root` / `default_android_ndk_root` needs to be set by us**; it is
hard-wired to the DEPS-fetched path:

```
config.gni:160:  android_ndk_root = "//third_party/android_toolchain/ndk"
config.gni:448:  android_toolchain_root =
        "$android_ndk_root/toolchains/llvm/prebuilt/${android_host_os}-${android_host_arch}"
```

The current checkout does **not** have `third_party/android_toolchain/ndk`
(`ls third_party/android_*` → "No such file or directory") because it was synced
without the Android gate. So the lane's one *fetch-side* change is to enable
`checkout_android` for the Android sync. Concretely, in `build-v8.py`'s
`sync_v8()` add the gclient custom var when `platform == "android"`:

```python
# android: DEPS gates the NDK + sdk on checkout_android; default sync omits it.
extra = []
if self.args.platform == "android":
    extra = ["--custom-var=checkout_android=True"]
run(["gclient", "sync", "-D", "--force", "--reset",
     f"--revision=src/v8@refs/tags/{self.tag}", *extra], cwd=SRC_DIR, env=self.env)
```

(NDK is `30.0.14608247`, a cipd package — fetched, not vendored. ~3-4 GB added to the
workspace; the SDK pieces beyond the NDK aren't needed for a pure native `.so` but ride
along on the same condition.)

**What changes vs `linux_gn_args` (the diff is small).** Proposed `android_gn_args`:

```gn
# --- common (unchanged from common_gn_args) ---
is_official_build=false
is_debug=false
v8_monolithic=true
v8_monolithic_for_shared_library=true   # local-dynamic TLS — still needed (see §3)
v8_use_external_startup_data=false
v8_enable_i18n_support=true             # Intl ON
use_rtti=false
v8_enable_sandbox=false
v8_enable_pointer_compression=false     # hold the drop-in contract (see note)
treat_warnings_as_errors=false
symbol_level=1
# (+ ccache cc_wrapper when present)

# --- android-specific ---
target_os="android"
target_cpu="arm64"                      # arm64 | x64 | arm | x86
android_ndk_api_level=23                # min SDK; 23 == default_min_sdk_version floor
# (default_min_sdk_version is 29 for the SDK build path, 23 for the NDK-only path —
#  config.gni:72/76; pin 23 explicitly so the .so loads on API 23+ devices)

# --- what we DROP vs linux_gn_args ---
#   use_sysroot=false            -> NOT used: Android MUST use the NDK sysroot.
#   use_custom_libcxx=false      -> NOT used: Android keeps the bundled libc++ (§4).
#   use_custom_libcxx_for_host=false -> harmless but irrelevant on android.
#   use_glib=false               -> irrelevant (no glib on android); drop it.
```

The Linux lane's defining choice — `use_sysroot=false` + system libstdc++ — is
**Linux-only** and must NOT be carried to Android. Android cross-compiles *only* against
the NDK sysroot + the NDK's libc++; there is no "host system libstdc++" to drop in to
(§4). So `android_gn_args` = `common_gn_args()` **minus** the three Linux ABI escapes,
**plus** `target_os/target_cpu/android_ndk_api_level`.

**Pointer compression note.** `gni/v8.gni:341-344` defaults
`v8_enable_pointer_compression = (v8_current_cpu == "arm64" || "x64" || "loong64")` —
i.e. **ON by default for android-arm64/x64**, exactly like Linux/mac. We hold the
project-wide `=false` (D3) for drop-in parity with the desktop artifacts and the choc
consumer TU, unless a future Android consumer is explicitly built compression-ON.
This is a *deliberate* off-default; android-arm64 with compression OFF is a less-covered
matrix than desktop — flag as a build-bring-up risk (§7), mirroring the Windows finding
where the non-default compression matrix miscompiled.

---

## 2. Build host — cross from x86_64 Linux, no extra host

**Confirmed.** `tools/clang/scripts/update.py:212` `_HOST_OS_URL_MAP`:

```
'linux': 'Linux_x64',  'mac': 'Mac', 'mac-arm64': 'Mac_arm64', 'win': 'Win'
```

There is **no `Linux_arm64`** host clang — V8's bundled clang (llvmorg-23) + Rust for a
Linux host are **x86_64-only**. The NDK toolchain itself is also selected by *host*:
`config.gni:388-405` sets `android_host_arch = "x86_64"` (for host_cpu x64 *or* arm64 —
"despite the x86_64 tag") and `android_host_os = "linux"` from `host_os`. So:

- **Android = cross from an x86_64 Linux host.** `host_cpu="x64"`, `host_os="linux"`,
  `target_os="android"`, `target_cpu="arm64"`. This is exactly how Chromium ships
  Android, and it reuses the **same authoritative build host the Linux lane already
  needs** (GitHub `ubuntu-24.04`, or the Rosetta-for-Linux x86_64 Tart VM on the Mac
  Studio — runbook §"Rosetta-for-Linux").
- **No new host.** The Linux VM at `192.168.64.7` is **arm64** (`uname -a` →
  `aarch64`), so it cannot run the bundled x86_64 clang *natively* — same constraint
  the Linux lane already documented. The Android lane inherits the runbook's settled
  answer: **GitHub `ubuntu-24.04` is authoritative**; the local Rosetta x86_64 Tart VM
  is the fast iteration loop. No additional host is introduced by Android.

The NDK ships its own arm64-target clang inside the toolchain; V8's *host-tool* clang
(torque, mksnapshot host build) stays x86_64. Both are present on an x86_64 Linux host.

---

## 3. The seal — `seal/elf.py` applies UNCHANGED

**Confirmed: Android is ELF, and the version-script seal is byte-identical.** The seal
mechanism is `-Wl,--version-script=v8_embedder_exports.map` keeping only the mangled
`_ZN2v8* / _ZNK2v8* / _ZTVN2v8* … _ZN6cppgc* …` prefixes global and localizing `*`
(`seal/elf.py` VERSION_SCRIPT + EXPORT_ALLOW_MANGLED). Android's `.so` uses the same
Itanium C++ mangling and the same `readelf -sW --dyn-syms` audit columns, so:

- `inject_seal_target()` `else` (ELF) branch in `build-v8.py` already covers Android:
  the `SEAL_TARGET_GN` guard is `(is_mac || is_linux || is_win)` and the ELF `else`
  emits `--version-script` + `-soname,libv8.so`. **Android is `is_android`, not
  `is_linux`** — so the **one required source change is widening that guard** to
  include `is_android` (and the ELF `else` already catches everything that isn't
  mac/win). Same for `LIBNAME`/`SEAL_BACKEND` dicts (`"android": "libv8.so"` /
  `"elf.py"`), `platform_gn_args` dispatch, and the `argparse` choices.
- The `--whole-archive` lesson holds identically: Chromium's `solink` rule
  whole-archives `{{inputs}}` once on the non-aix/non-mipsel ELF branch (Android is in
  that branch), so we still rely on `deps=[:v8_monolith]` + the version script and do
  **not** hand-roll a second `--whole-archive` (the Linux dup-symbol bug, runbook #1).
- `seal/elf.py` audit (allow-list + deny-list + readelf) needs **zero changes** —
  symbol-level, arch/OS-independent (already proven on both mac arches and Linux x64).

**Android-specific seal concerns (flagged, not blockers):**

- **TLS model — still need `v8_monolithic_for_shared_library=true`.** That arg's only
  effect (BUILD.gn:1257) is to define `V8_TLS_USED_IN_LIBRARY`, switching V8's hot
  thread-locals from local-exec to the PIC-safe **local-dynamic** model. android-arm64
  is `-shared`/PIC like Linux, so the same `R_*_TPOFF`-class reloc-in-shared-object
  failure applies → keep the arg ON. (arm64 TLS uses TPREL/TLSDESC relocs rather than
  x86's `R_X86_64_TPOFF32`, but the local-dynamic switch is the correct fix on both;
  this is the single thing most likely to surface a first-build link error if it were
  ever dropped.)
- **Symbol visibility / the bundled libc++.** Because Android keeps the **bundled
  Chromium libc++** (§4), its libc++ symbols are already hidden/internalized by the
  build (not in V8's public ABI), and the version script localizes everything that
  isn't `_ZN2v8/_ZN6cppgc`. So libc++ symbols cannot leak through the seal — the
  allow-list audit (which failed-closed on icu/absl template instantiations on Linux)
  covers this for free.
- **`libc++_shared.so` runtime dependency.** Static-libc++ (default for a non-component
  build, `c++.gni:75 libcxx_is_shared = use_custom_libcxx && is_component_build` → false
  here) folds libc++ into `libv8.so`. So our sealed `libv8.so` is **self-contained
  w.r.t. libc++** and does not require shipping a separate `libc++_shared.so` — good for
  a drop-in artifact, and it keeps the ABI namespace internal.

---

## 4. Consumer ABI — the decisive Android difference

This is the one place Android genuinely diverges from Linux, and it must be called out
loudly. On Linux the lane deliberately uses **system libstdc++**
(`use_sysroot=false` + `use_custom_libcxx=false`) so the `.so` is drop-in against a
RHEL/Ubuntu host toolchain, because V8's public API exposes `std::` types
(`v8::platform::NewDefaultPlatform(..., std::unique_ptr<...>, ...)`) and the bundled
`__Cr`-namespaced libc++ would give the consumer undefined-references at link.

**On Android there is no "system libstdc++" escape hatch.** Android's platform C++ is
the **NDK's own libc++** (`c++_shared` / `c++_static`) — there is no GNU libstdc++ on
the NDK target. V8's `use_custom_libcxx` defaults **true** (`c++.gni:17`) and on a
cross-target toolchain `use_custom_libcxx_for_host` doesn't relax it
(`c++.gni:65: use_custom_libcxx || (use_custom_libcxx_for_host && !is_a_target_toolchain)`).
So **android `libv8.so` is built with Chromium's bundled libc++**, which carries the
`__Cr` ABI namespace (`_LIBCPP_ABI_NAMESPACE=__Cr`) and unstable-ABI layout
(`libcxx_abi_unstable`).

**What a consumer must match (the analog of the Linux libstdc++/`__Cr` decision):**

- This is the **same situation as the Windows lane**, not the Linux lane: the consumer
  (Pulp on Android) **must compile against the same Chromium-style libc++** that V8 was
  built with — i.e. consume the V8 `__Cr` libc++ ABI, exactly as Windows decided
  (clang-cl + V8's bundled libc++). The NDK's *default* `c++_shared` is the standard
  unstable-ABI libc++ from the same LLVM lineage, so in practice the consumer links the
  **NDK `c++_shared`** and the ABIs line up *as long as the std-type surface
  (`v8::platform`, `std::unique_ptr`, `std::string`) is exercised through matching libc++
  headers*. The risk is the `__Cr` namespace + `libcxx_abi_unstable` skew between
  V8's bundled libc++ and the NDK's libc++ revision.
- **Decision to lock (mirrors Windows D-decision):** for Android, **do not try the
  Linux system-STL trick**. Either (a) ship `libv8.so` with **static libc++ folded in**
  (current default) and validate that the public `v8::platform` std-type surface links
  clean against an NDK-`c++_shared` consumer; or (b) if a `__Cr`-vs-NDK-libc++ mismatch
  appears at link, build V8's libc++ with `libcxx_abi_unstable=false` (stable ABI) so a
  stock-NDK consumer can link it. **`use_custom_libcxx` DOES matter here** — it is the
  crux of the Android consumer ABI, and unlike Linux we cannot set it false.
- Pulp's Android build (`tools/cmake/PulpAndroid.cmake` + the NDK toolchain file) uses
  the NDK default STL; the validator (§6) must compile through *that* STL to prove the
  consumable ABI, the same way the Windows validator compiles through clang-cl + V8
  libc++. **This is the highest-information thing the Android validator proves.**

---

## 5. Skia/Dawn pairing — validate with a stub (like Windows)

`gh release view chrome/m150 --repo danielraffel/skia-builder --json assets` lists:

```
skia-build-ios-device-arm64-gpu-release.zip
skia-build-ios-simulator-arm64-x86_64-gpu-release.zip
skia-build-linux-x64-gpu-release.zip
skia-build-mac-arm64-gpu-release.zip
skia-build-mac-universal-gpu-release.zip
skia-build-mac-x86_64-gpu-release.zip
skia-build-visionos-device-arm64-gpu-release.zip
skia-build-visionos-simulator-arm64-gpu-release.zip
skia-build-wasm-wasm32-gpu-release.zip
skia-build-win-x64-gpu-debug.zip
skia-build-win-x64-gpu-release.zip
Skia.xcframework.zip
```

**There is NO `skia-build-android-*` asset.** skia-builder does not (yet) publish an
Android Skia archive. Pulp builds Android Skia **locally** via
`tools/build-skia-android.sh` (arm64 / x86_64 ABIs, NDK cross-build) — not from a
skia-builder release.

**Consequence:** the Android coexistence validation in CI **cannot download a matching
Skia artifact**, so it follows the **Windows model**: validate **identity only** against
a **stub** collision partner (the `v8_identity_stub.cc` pattern already in `build-v8.py`
`inject_win_validator`), and treat the **seal audit (0 absl/icu/zlib exports) as the
coexistence guarantee**. Full V8↔Skia/Dawn coexistence on Android is provable later
either by (a) adding an `android` asset to skia-builder, or (b) running the Pulp
`threejs-native-demo` on an Android device/emulator (Pulp already cross-builds Android
Skia + Dawn — see the `android` skill). Record this as a known coexistence gap in the
Android `manifest.json` (`coexistence: "identity-only (no skia android artifact)"`),
exactly as Windows records its identity-only status.

---

## 6. Validation — `adb push` a native executable + run on emulator

The minimal **no-skip-pass identity check** is the same three asserts as every lane:
**V8 init + eval + `v8::V8::GetVersion()` == `manifest.json`**. On Android the wrinkle is
*where it runs*: a cross-built android-arm64 executable can't run on the x86_64 build
host, so (exactly like the Windows arm64 cross case) **build + seal-audit happen on the
host; the identity run happens on an Android target**.

**Recommended: native executable via `adb push` (not JNI).**

1. Inject an `executable("v8_identity_validator")` gn target (the `inject_win_validator`
   analog, ELF branch): sources `v8_identity_main.cc` (reuse `validate/identity_main.cpp`)
   + `v8_identity_stub.cc`, `deps=[":v8_sealed_shared"]`, `configs += [":external_config"]`
   (so it compiles with V8's exact feature defines — compression OFF, sandbox OFF — or
   `V8::Initialize` aborts on an embedder-vs-V8 mismatch, the same trap the Windows lane
   hit).
2. ninja the target → `v8_identity_validator` (android-arm64 ELF) + co-located `libv8.so`.
3. On an **Android emulator (arm64 system image) or device**:
   ```
   adb push v8_identity_validator libv8.so /data/local/tmp/v8val/
   adb shell 'cd /data/local/tmp/v8val && chmod +x v8_identity_validator && \
              LD_LIBRARY_PATH=. ./v8_identity_validator'
   ```
   Assert exit 0 + the printed `GetVersion()` equals the manifest version. No JNI app,
   no APK packaging needed for the identity gate — a plain `adb shell` exec is the
   minimal honest proof V8 inits + evals on android-arm64.
   - JNI/`UnitTestActivity` is heavier and only needed if we later want the *Skia/Dawn
     coexistence* render proof (which needs an Activity + surface). The android skill's
     emulator workflow is the reference if/when that lands.

**Emulator-on-Mac options.** Build is x86_64-Linux (CI), but the **run** target is
arm64 — so the emulator should be an **arm64 system image**:
- On the **Mac Studio (Apple silicon)**, `emulator -avd <arm64-v8a image>` runs
  *natively* fast (no nested virt), and `adb` over USB/TCP reaches it. This is the
  cheapest place to run the android-arm64 identity exe.
- In **CI**, GitHub's `reactivecircus/android-emulator-runner` runs an emulator on the
  ubuntu-x86_64 runner — but that's an **x86_64** image, which can't run our
  **arm64** validator without ARM translation (slow/flaky). So the clean split:
  **build+seal in CI** (ubuntu-24.04), **run the arm64 identity exe on the Mac Studio's
  arm64 emulator/device** (the same "cross-build in CI, run on a native VM/host"
  pattern the Windows arm64 lane uses). For an `x86_64` Android ABI cell, the CI
  emulator can run it inline.
- Physical arm64 device via `adb` is equally valid and faster than any emulator.

---

## 7. Effort, risks, parallelism

**Effort: LOWEST of any new lane — ~0.5-1 day of bring-up + 1 build/validate cycle.**
Rationale, all grounded:
- **Seal: $0** — `seal/elf.py` unchanged; the ELF branch in `inject_seal_target` already
  exists. One-line guard widen (`|| is_android`) + the `LIBNAME`/`SEAL_BACKEND`/
  `platform_gn_args`/argparse-choices plumbing.
- **Build host: $0** — reuses the x86_64-Linux host the Linux lane already requires;
  cross-compile only changes `target_os/target_cpu`.
- **gn args: small** — `android_gn_args` = `common_gn_args` minus 3 Linux escapes plus 3
  android lines (§1); enable `checkout_android` in the sync.
- **Validator: small** — clone the `inject_win_validator` ELF-side analog + an
  `adb push` run step (the only genuinely new CI plumbing).
Compare: Windows needed a whole new COFF seal backend + the libplatform-export-gate
patch + compression-ON matrix debugging. Android needs none of that.

**Parallelism: fully parallel with the iOS lane — confirmed.**
- Android builds on the **x86_64 Linux host** (CI `ubuntu-24.04` / Rosetta x86_64 Tart
  VM). iOS builds on a **mac** host (Xcode/clang, jitless). Different OS hosts, different
  toolchains, **no shared `out/` dir, no shared workspace state** → they can run at the
  same time on the same Mac Studio (one in a Linux VM, one on the mac) or on separate CI
  runners. The intra-repo single-SHA gate (`tools/check_single_sha.py`) still applies if
  both ship in one sweep.

**Top risks (ranked):**
1. **Consumer libc++ ABI (§4) — the real one.** Android can't use the Linux
   system-STL drop-in trick; the `__Cr` / `libcxx_abi_unstable` bundled libc++ vs the
   NDK consumer's libc++ is the most likely link-time surprise. Mitigation: validate
   through the NDK-default STL; fall back to `libcxx_abi_unstable=false` if it skews.
2. **Pointer-compression OFF on android-arm64 is an off-default matrix.** Same class of
   risk that miscompiled Windows non-default compression. arm64 PC-off is more common
   upstream than Windows's case, but watch for a Torque/offset static_assert on the
   first build. Mitigation: if it breaks, the escape is compression-ON for Android (no
   libnode pins Android, so the consumer just matches — like Windows).
3. **NDK fetch weight / `checkout_android` sync.** ~3-4 GB cipd NDK + SDK added to an
   already-large workspace; first sync is slow. Mitigation: cache `third_party/
   android_toolchain` keyed on `android_ndk_version`.
4. **TLS reloc on arm64** if `v8_monolithic_for_shared_library` were ever dropped — keep
   it ON (§3).
5. **Coexistence is identity-only** until a skia-builder Android asset or an on-device
   Pulp render exists (§5) — honest gap, not a failure, but don't claim full V8↔Skia
   coexistence on Android from a green identity run.

---

## Appendix — minimal `build-v8.py` change surface

1. `argparse` `platform` choices: add `"android"`.
2. `gn_cpu`: add `"arm"` / `"x86"` maps if building those ABIs (`arm64`/`x64` already map).
3. `android_gn_args(arch)` (§1) + `platform_gn_args` dispatch.
4. `sync_v8()`: `--custom-var=checkout_android=True` when android.
5. `SEAL_TARGET_GN` guard: `(is_mac || is_linux || is_win || is_android)`.
6. `LIBNAME["android"]="libv8.so"`, `SEAL_BACKEND["android"]="elf.py"`.
7. Inject an ELF `v8_identity_validator` target (clone of `inject_win_validator`) +
   `validate_android_identity()` doing `adb push` + `adb shell` run when a device/
   emulator is reachable; otherwise bundle exe+`libv8.so` into the artifact for an
   offline run on the Mac Studio arm64 emulator (the Windows-arm64-cross pattern).
8. `run_all()` default arch for android = `arm64`; run the android identity step like the
   `win` branch.
9. CI: add matrix rows `{ os: ubuntu-24.04, platform: android, arch: arm64, build:
   'android -archs arm64' }` (build+seal in CI); arm64 identity run on the Mac Studio /
   device, x86_64 ABI cell can run inline on the CI emulator.

---

## Biggest open question

**Does our bundled-libc++ android `libv8.so` link cleanly against a stock-NDK
`c++_shared` consumer, or do we need `libcxx_abi_unstable=false`?** This is the single
unknown that decides whether Android is a half-day lane or needs a libc++-ABI spike —
it's the Android analog of the Windows "MSVC-STL vs V8 libc++" decision, and it can only
be answered by actually compiling the §6 validator (which exercises the `v8::platform`
`std::unique_ptr` surface) through the NDK toolchain and seeing if it links.

## Review addendum — RepoPrompt + Codex (2026-06-05, both concur)

Two independent reviews agreed on the substance. Corrections to fold in before building:

1. **The libc++/ABI path is OPEN, not "the Windows model".** Codex: V8's `c++.gni`
   supports `use_custom_libcxx=false` **with** `use_custom_libcxx_for_host=true` for cross
   builds — i.e. build the *target* against NDK libc++ while host tools stay on Chromium
   libc++. **Try this FIRST** — it may avoid the `__Cr` mismatch entirely and gives the
   clean stock-NDK consumer story. Note: `libcxx_abi_unstable=false` does **not** fix the
   `__Cr` namespace mismatch (separate concern) — don't rely on it.
2. **The adb/GN-injected validator is a SMOKE TEST, not the ABI gate.** A validator built
   as a GN target inside the V8 tree (with `:external_config`) proves a *Chromium-toolchain*
   consumer, not a stock-NDK/Pulp one. **Add an external NDK CMake consumer** that links the
   *packaged* headers + `libv8.so` exactly as Pulp would (exercising
   `v8::platform::NewDefaultPlatform`'s `std::` surface). That is the real libc++ gate.
3. `android_ndk_api_level` default in this checkout is **29** (23 is Cronet-only) — 23 is a
   deliberate lower floor; justify it or use 29.
4. **Version pin:** `build-v8.py`'s `DEFAULT_V8_TAG` is still `14.6.202.33`; the desktop
   release is **15.1.27** — pin 15.1.27 for parity.
5. **Plumbing beyond gn args** (both reviewers): `LIBNAME["android"]`,
   `SEAL_BACKEND["android"]`, `default_arch`, `platform_gn_args` dispatch, argparse choice;
   CI must **skip** the non-Windows Skia-download + CMake validator (no Android Skia); add a
   `readelf -d` DT_NEEDED audit (the seal only checks exports, not deps); manifest field
   `coexistence = identity-only`; ABI artifact layout (`jniLibs/arm64-v8a/`); and verify
   `.gclient` `target_os=["android"]` / the generated build config actually settles after
   `--custom-var=checkout_android=True` (don't assert it).
6. Seal reuse (`seal/elf.py` unchanged, one-line `is_android` guard widen) — **confirmed** by
   both. Caveat: also verify Android `solink` whole-archives `:v8_monolith` exactly once, as
   the Linux rule does.
