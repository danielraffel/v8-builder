# v8-builder

Build and **seal** a standalone, embeddable [V8](https://v8.dev/) that coexists with
[Skia](https://skia.org/) and [Dawn](https://dawn.googlesource.com/dawn) (WebGPU) in a
single native binary — macOS, Linux, and Windows, on both Intel and ARM.

## The problem this solves

V8, Skia, and Dawn each vendor their **own** copies of ICU, zlib, and (for the
Chromium-derived libraries) Abseil. Link two or three of them into one binary the naïve
way and you hit duplicate-symbol / **ODR** collisions. The Abseil clash is the nasty
one: two copies of Abseil in a process don't just bloat the binary, they **abort at
runtime** (e.g. duplicate flag/registry singletons), often far from where you linked
them.

`v8-builder` produces V8 as a **shared library** whose export table exposes *only* the
`v8::` / `cppgc::` embedder API and keeps ICU, zlib, and Abseil **internal**. Because
V8's copies are no longer exported, they can't collide with the copies inside Skia or
Dawn — so all three coexist in one binary. (This is the same property that lets, say,
Homebrew's `libnode` sit next to other libraries today.)

## Who this is for

Anyone embedding V8 in a C++/native application that **also** links Skia and/or Dawn,
cross-platform — GPU-accelerated creative and design tools, custom cross-platform UI
frameworks, embedded/alternative browser shells, engine and DCC tooling. If you've ever
seen a duplicate-ICU link error or an Abseil ODR abort while putting a JS engine next to
a GPU stack, this repo is the fix.

It's **not** specific to any one application. See [_How we use it_](#how-we-use-it) for
the project it was originally built for.

## What you get

A shared library (`libv8.dylib` / `libv8.so` / `v8.dll`) that:

- **Exports only `v8::` / `cppgc::`** — ICU, zlib, and Abseil stay internal (verified by
  an export-table audit on every build; zero non-V8 leaks is a hard gate).
- **Has `Intl` on** — full ICU is bundled *inside*, so `Intl`/locale features work
  without exposing ICU's symbols.
- **Is ABI-consumable** — built against the platform C++ runtime where that matters, so a
  normal consumer can link it (see [_Consuming it_](#consuming-it) for the ABI contract).
- Ships with a **manifest** recording the exact V8 revision and the Skia release it was
  validated against (see [_Coexistence_](#coexistence-is-abi--seal-not-version-matching)).

## Status

Each cell below is proven end-to-end: **build → seal (0 ICU/zlib/Abseil exports) →
identity check** (the library initializes V8, evaluates JS, and reports its own version —
no skip-pass), and on the CMake harness a **forced-collision coexistence link** against a
real Skia build.

| Platform | Intel (x64) | ARM (arm64) |
|----------|-------------|-------------|
| macOS    | ✅ released  | ✅ released  |
| Linux    | ✅ released  | ✅ released  |
| Windows  | ✅ released  | ✅ released  |
| Android  | —           | ✅ released  |
| iOS      | —           | ✅ proven ¹  |

All six desktop cells ship in the [latest release](../../releases/latest), built fresh and
**single-SHA gated** in one sweep (no mixed-revision release), each validated for coexistence
against the paired Skia. Android (`arm64-v8a`) ships sealed + identity-checked on a real
emulator.

¹ **iOS** is jitless (lite mode) and ships as a `V8.framework` rather than a plain library —
the **simulator-arm64** slice is proven (the sealed framework prevents the V8↔Dawn Abseil ODR
on iOS: gate showed 725 collision partners resident, V8 init + eval, no abort) and is the one
in the release; the **device-arm64** slice builds with your Apple signing identity. The two
mobile "—" cells aren't targets: Android is arm64-only (`arm64-v8a`), and the Intel iOS
simulator is deferred (all-Apple-silicon fleet).

V8 15.1; Intl on (off on iOS). Pointer compression is **off** on macOS/Linux (for drop-in
parity with `libnode`-style consumers) and **on** on Windows (V8's supported default there);
Windows + Android consumers also link a Chromium-style `__Cr` libc++ — a consumer must match,
see below.

## What a build produces

Every cell runs the same pipeline — `build-v8.py <platform> -archs <arch>` → sync V8 →
`gn`/`ninja` the static `v8_monolith` → `seal/<platform>.py` wraps it into a sealed shared
library → audit (0 ICU/zlib/Abseil exports) → **strip locals/debug (re-audited to prove the
export seal is unchanged)** → package. Each release artifact is a lean, per-platform-correct
`v8-<platform>-<arch>-<ver>.zip` — only the headers, the one stripped binary, and the
manifest; no build-only validator harness, no duplicate copies:

```
mac / linux:
  include/                # the v8::/cppgc:: public headers, *.h/*.inc only (no DEPS/OWNERS/*.md/*.json/*.pdl)
  lib/libv8.{dylib,so}    # the sealed shared library, STRIPPED (locals/debug gone, .dynsym/export trie kept)
  manifest.json
windows:
  include/
  lib/v8.dll + lib/v8.dll.lib   # sealed DLL + import lib (PE carries no embedded debug → no strip)
  manifest.json
android:
  include/
  jniLibs/<abi>/libv8.so  # idiomatic Android layout (the ONLY .so copy; STRIPPED via the NDK llvm-strip)
  manifest.json
ios:
  V8.framework/           # the sealed framework (STRIPPED binary + embedded Headers/ + Info.plist)
  manifest.json
```

The strip keeps the dynamic export table (the seal) byte-for-byte: the packager re-runs
`seal/<platform>.py audit` after stripping and **aborts the build if the export count
changes**, so a leaner artifact can never be a less-sealed one. The build-only `validate/`
harness (the android / win-arm64 cross-built identity validators) rides along in the
*uploaded CI artifact* for the post-build emulator/VM step, but is excluded from the
released zip — a consumer never needs it. `manifest.json` records the exact V8 revision,
arch, ABI flags, the relative path to the shipped binary (`lib`), and the Skia release the
artifact was validated against (the co-validated pair).

A multi-platform release is **single-SHA gated**: every artifact must name the same V8
revision, or the release is rejected — so a downloaded set is never mixed-revision.

## Automatic releases

A scheduled workflow ([`release-watch.yml`](.github/workflows/release-watch.yml)) polls
upstream V8 tags weekly and, when a version newer than the last published release appears,
dispatches the full all-platform sweep (build → seal → single-SHA gate) for that version and
opens a tracking issue. It's **build-and-hold by default** (you review the run, then publish);
set the repo variable `V8_WATCH_AUTOPUBLISH=true` to publish automatically. You can also run it
on demand from the Actions tab, optionally forcing a specific version. Because coexistence is
ABI + seal rather than version-matching (see below), a V8 bump reuses the current Skia pin
(`V8_WATCH_SKIA_TAG`) unchanged. *Note: the macOS and `linux-arm64` cells build on self-hosted
runners, which must be online when a sweep fires.*

## How the seal works

The mechanism is the same idea on every platform — *don't export the bundled
third-party symbols* — but the lever differs:

- **Linux (ELF):** a version script (`{ global: v8::*, cppgc::*; local: *; }`) that hides
  everything else; TLS built in the shared-library model.
- **macOS (Mach-O):** an `-exported_symbols_list` restricting the dylib to the
  `v8::`/`cppgc::` mangled surface.
- **Windows (PE/COFF):** V8's `dllexport` (`V8_EXPORT`) already marks only the public
  surface, so the monolith's ICU/zlib/Abseil objects stay internal by construction; one
  `v8.dll` is produced via whole-archive.
- **Android (ELF):** the **same version-script seal as Linux** — Android `.so` uses
  Itanium C++ mangling and the same `{ global: v8::*, cppgc::*; local: *; }` lever, so
  `seal/elf.py` is reused unchanged. It's a **cross-compile** from an x86_64-Linux host
  (`target_os=android`, `host_cpu=x64`); the build additionally runs a `readelf -d`
  **DT_NEEDED audit** asserting the sealed `libv8.so` pulls no system ICU/zlib/Abseil at
  load time. Artifacts ship in `jniLibs/<abi>/` layout.

Each backend has an **auditor** that re-reads the finished binary's export/symbol table
and fails the build if anything outside `v8::`/`cppgc::` (plus V8's own
`v8_inspector::`/`heap::` and C entry points on Windows) is exported — an allow-list, so
it catches leaks it was never told to look for.

## Consuming it

Link against the library and its headers. The **ABI contract** a consumer must match:

- **C++ runtime:** platform `libc++`/`libstdc++` on macOS/Linux; on Windows and
  **Android**, Chromium's `libc++` (the `__Cr` ABI namespace). On Android the NDK-libc++
  target ABI was attempted first (`use_custom_libcxx=false`) but does not link against the
  DEPS cipd `android_toolchain`, which ships no standalone unwinder — so `libv8.so` is
  built with V8's bundled libc++ (folded statically; self-contained, no `libc++_shared.so`
  dependency), and an Android consumer must compile against a Chromium-style `libc++`,
  exactly the Windows model. RTTI is off.
- **Pointer compression:** define `V8_COMPRESS_POINTERS` to match the build (off on
  macOS/Linux/Android, on on Windows) — a mismatch makes `V8::Initialize` abort with an
  explicit embedder-vs-V8 message.

Beyond that it's ordinary V8: create a platform, an isolate, a context, and run.

## Coexistence is ABI + seal, not version-matching

V8 and Skia/Dawn don't share C++ types — they interoperate through serialized data, and
each keeps its **own** sealed copy of ICU/zlib/Abseil. So whether they coexist depends on
**ABI compatibility at the link boundary** (libc++/STL, RTTI, pointer compression) plus
the symbol seal — **not** on the two being the same upstream version. (Empirically, V8
15.1 coexists fine with Skia from a *different* Chromium milestone.)

Each release therefore records a **validated pair** — the exact V8 build and the exact
Skia release it was tested against, SHA-pinned in the manifest. That's a *proof of
coexistence*, not a claim of an upstream-blessed match. A multi-platform release is gated
so every per-platform artifact names the **same** V8 revision (no mixed-revision
releases). Building both Skia and V8 from one Chromium DEPS revision would yield a truly
co-built pair — a possible future option, not what this does today.

## Layout

```
build-v8.py            # build + seal orchestrator (depot_tools / gn / ninja)
seal/                  # the "keep ICU/zlib/Abseil invisible" policy + per-platform backends
  policy.json          #   public-symbol allow-list + ICU/zlib/Abseil deny prefixes
  elf.py macho.py coff.py   #   per-platform export-list generators + auditors
  coff_research.md     #   Windows PE/COFF sealing notes
validate/              # coexistence proof: V8 + Skia in one binary
  CMakeLists.txt
  identity_main.cpp    #   asserts ENGINE identity (init + eval + version), not pixels
  smoke_gpu.cpp        #   forced-collision link against real Skia
  run_validation.cmake #   strict, no-skip-pass gate
  android/             #   external NDK consumer — the Android libc++-ABI gate
    CMakeLists.txt     #     links packaged headers + sealed libv8.so via the NDK toolchain
    consumer_main.cpp  #     exercises v8::platform's std::unique_ptr surface (the ABI test)
                       #     (must build with a Chromium-style libc++ — the __Cr contract)
tools/                 # release gates (single-SHA, Skia/V8 pair pinning)
.github/workflows/     # per-platform build + seal + validate matrix
```

## How we use it

This was built for **[Pulp](https://github.com/danielraffel/pulp)**, a cross-platform
audio-plugin and application framework that renders JS-scripted GPU UIs via Dawn + Skia
and offers a choice of JS engines (QuickJS / JavaScriptCore / V8). Pulp needed V8 as an
option *without* giving up the Dawn/Skia render stack — exactly the ICU/Abseil
coexistence problem above. Pulp consumes the sealed library through its engine-provider
contract (`-DPULP_JS_ENGINE=v8` plus the include/library paths).

Nothing in `v8-builder` is Pulp-specific, though — if you're putting V8 next to Skia and
Dawn anywhere, it should work for you the same way.

## Appendix: relationship to skia-builder

`v8-builder` is inspired by Oli Larkin's
[skia-builder](https://github.com/olilarkin/skia-builder) — the same idea (a
reproducible, CI-driven builder that publishes prebuilt Chromium-derived libraries)
applied to V8 instead of Skia. The two are intentionally designed to sit alongside each
other:

- **Shared build-system conventions** — a similar build-orchestrator CLI shape, the same
  per-platform artifact naming (`<platform>-<arch>-…-release.zip`), and the same
  gn/arch labels.
- **A common release-pairing contract** — both emit the same manifest schema, so a
  project consuming Skia *and* V8 can check it's using a **co-validated pair**: this repo
  records the exact Skia release each V8 build was validated against (see
  [_Coexistence_](#coexistence-is-abi--seal-not-version-matching)), and the Skia side of
  that pair is precisely what skia-builder produces.

So if you already build Skia with skia-builder, V8 from here should slot in with the same
ergonomics — and the coexistence/pairing checks tie the two together. (We hope folks
already using skia-builder for a Skia/Dawn stack find this a natural companion.)

## License

MIT (see `LICENSE`). V8, ICU, zlib, and Abseil carry their own licenses; a per-release
SBOM is planned. Structure inspired by [skia-builder](https://github.com/olilarkin/skia-builder)
(Oli Larkin).
