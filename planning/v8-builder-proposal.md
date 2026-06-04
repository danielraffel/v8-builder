# v8-builder — Proposal

**Status:** Draft v1 (pre-review). Will be revised after 2 review passes (Codex + RepoPrompt oracle).
**Author:** Daniel + Claude
**Date:** 2026-06-03
**Audience:** Oli (skia-builder author) + Pulp/iPlug maintainers

---

## 1. Goal

Build **standalone, redistributable V8 (`v8_monolith`) static libraries** for the
platforms/architectures Pulp and iPlug need, on GitHub Actions, triggered the
same way `skia-builder` is, and published as **tagged releases with per-platform /
per-arch artifacts** — so that a from-source V8 can link cleanly *next to Skia
Graphite + Dawn* in a single binary.

Concretely we want to stop depending on **Homebrew Node's `libnode`** as the
macOS V8 provider, and to gain a **validated Linux and Windows V8 provider** that
does not exist today.

Target matrix (v1):

| Platform | Arch(s) | Runner | Notes |
|----------|---------|--------|-------|
| macOS | arm64, x86_64, universal | `macos-15` | replaces `libnode` dependency |
| Linux | x64 (arm64 later) | `ubuntu-latest` | new validated provider |
| Windows | x64 (arm64 later) | `windows-2022` | new validated provider, MSVC `/MT` ABI |

iOS/visionOS are explicitly **out of scope** (Pulp uses JavaScriptCore there; a
from-source iOS V8 is a ~50 GB workspace and not on a ship path — per
`v8-with-skia-dawn-for-oli.md`).

## 2. Why this is *not* just "skia-builder for V8"

`skia-builder` and a V8 builder share ~80% of their machinery (both are
Chromium-lineage: `depot_tools` + `gclient`/`gn`/`ninja`). The build script and
CI matrix can be modeled almost 1:1 on skia-builder. **That part is easy.**

The hard 20% — and the entire reason this repo needs to exist — is captured in
`v8-with-skia-dawn-for-oli.md`:

> "budget the work as *make V8's ICU/zlib invisible and ABI-match Skia* — that,
> not compiling V8, is the actual hard part."

Two failure modes a naïve `v8_monolith` build hits when linked next to Skia:

1. **Duplicate ICU/zlib symbols.** Both V8 and Skia bundle ICU and zlib. Skia
   ships ICU as a static archive with **plain, unversioned** symbols
   (`_ubrk_open`, `_uloc_forLanguageTag`, …). A default `v8_monolith` exports the
   *same* flat names. At final link the linker sees two definitions →
   duplicate-symbol errors, or silent one-wins-at-runtime data mismatches.
2. **ABI mismatch.** libc++ vs libstdc++, `use_custom_libcxx`, RTTI, and
   exception flags must match what Skia was built with, or you get link-time
   undefined symbols or runtime UB.

Homebrew `libnode` dodges (1) for free because its ICU is **sealed** (two-level
namespace / hidden visibility) — `nm -gU libnode.dylib | grep ubrk` returns
**zero**, while ~4,750 `v8::` embedder symbols are public. Our from-source build
must **replicate that sealing ourselves**. This is the core engineering work and
the main thing this proposal is really about.

## 3. Repo layout (mirrors skia-builder)

```
v8-builder/
├── README.md
├── CLAUDE.md                      # build commands + architecture notes
├── LICENSE
├── Makefile                       # local macOS helper targets
├── build-v8.py                    # main build orchestrator (mirrors build-skia.py)
├── build-win.sh                   # Windows helper wrapper
├── seal-symbols.py                # NEW: the "make ICU/zlib invisible" step
├── patches/                       # any V8/gn patches we need to carry
├── validate/                      # NEW: the cross-link validation harness
│   ├── CMakeLists.txt             # links v8_monolith + Skia + Dawn together
│   ├── smoke_v8.cpp               # the ONLY TU that includes <v8.h>
│   ├── smoke_gpu.cpp              # the ONLY TU that includes Skia/Dawn
│   └── main.cpp                   # runs JS eval + Skia draw, asserts output
└── .github/workflows/
    ├── build-v8.yml               # build matrix + release (mirrors build-skia.yml)
    └── validate-v8.yml            # standalone re-validation of a release
```

## 4. `build-v8.py` design

Same shape as `build-skia.py`: an argparse CLI + a build class that sets up
`depot_tools`, fetches V8, generates `args.gn`, runs ninja, then packages.

```
build-v8.py [-h] [-config {Debug,Release}] [-archs ARCHS]
            [-tag V8_VERSION] [--shallow] [--seal/--no-seal]
            {mac,win,linux,universal}
```

Stages:

1. **setup_depot_tools()** — clone `depot_tools`, `fetch v8` (or
   `gclient sync` against a pinned tag), like skia's `setup_depot_tools()`.
   Pin via a V8 version tag (e.g. `12.x` matching the Node/V8 we currently
   link) so the API surface `choc_javascript_V8.h` expects stays stable.
2. **generate_gn_args(arch)** — emit `args.gn` (see §5). Constants live at module
   top like skia-builder's `RELEASE_GN_ARGS` / `PLATFORM_GN_ARGS`.
3. **build()** — `gn gen` + `ninja -C out/<arch> v8_monolith`.
4. **seal_symbols()** — invoke `seal-symbols.py` on the resulting archive (§6).
   This is the step skia-builder has no equivalent for.
5. **package()** — copy headers (`include/`), the sealed `libv8_monolith.a` /
   `v8_monolith.lib`, the snapshot blob if external, and a `manifest.json`
   recording V8 version + exact gn args + ABI flags into `build/<platform>-<arch>/`.

## 5. GN args (the contract)

Baseline (all platforms), chosen to match Skia and to embed everything:

```gn
is_official_build = true
is_debug = false
v8_monolithic = true
v8_use_external_startup_data = false   # embed snapshot, no loose .bin
v8_enable_i18n_support = true          # keep Intl (libnode parity) — see decision D2
use_thin_lto = false
symbol_level = 0
v8_enable_sandbox = <decision D3>
treat_warnings_as_errors = false
```

ABI-matching knobs (**must equal Skia's**, verify against skia-builder
`build-skia.py` GN args before locking):

```gn
use_custom_libcxx = false              # use system libc++ / match Skia
v8_enable_rtti = <match Skia -frtti?>  # Skia builds -fno-rtti by default
# exceptions: V8 is -fno-exceptions by default; Skia too. Keep aligned.
```

Per-platform:

- **macOS:** `target_cpu = "arm64"|"x64"`, deployment target matching Pulp
  (Skia targets macOS 11+). `universal` = build both, `lipo -create`.
- **Linux:** `target_cpu = "x64"`. Use the same clang/libc++ posture as Skia's
  Linux build. Install matching toolchain (LLVM 19 like skia-builder).
- **Windows:** `target_cpu = "x64"`, **`/MT` (static CRT)** to match skia-builder's
  Windows `extra_cflags = ["/MT"]`. Mismatched CRT (`/MD` vs `/MT`) is a classic
  embed failure → call this out loudly.

## 6. The hard part: `seal-symbols.py`

Goal: produce an archive where the **only** externally-visible symbols are the
V8 embedder API (`v8::*`), and ICU/zlib/etc. are **localized** (present but not
globally visible), replicating `libnode`'s sealed-ICU property. This is what
prevents the duplicate-symbol clash with Skia's flat ICU.

Per-platform technique (to be validated empirically — §8):

- **macOS / Linux (static `.a`):** Visibility flags alone (`-fvisibility=hidden`)
  do **not** stop duplicate-symbol errors, because symbols in a static archive's
  object files are still `GLOBAL`. We must **localize** them post-build:
  - Linux: `objcopy --localize-symbols=keep.txt` (or `--keep-global-symbol`
    restricted to `v8*`) per object, or a partial link
    `ld -r --version-script` then `objcopy`.
  - macOS: `ld -r -exported_symbols_list v8_only.txt` partial link, or
    `nmedit`/`strip -s` with a retained-symbols file.
  - Then **assert** with `nm -gU` (mac) / `readelf -sW` (linux): zero global
    `u_*`/`ubrk_*`/`ucnv_*`/`uloc_*`/zlib symbols; non-zero `v8::` symbols.
- **Windows (`.lib`):** Static libs don't "export"; the collision is duplicate
  definitions at final link. Options: link V8 ICU into a sealed object via
  `/WHOLEARCHIVE` discipline, or prefer **decision D2** (drop ICU) on Windows
  first. Validate with `dumpbin /symbols`.

This file is the genuinely novel part of the repo. Everything else is
skia-builder mechanics.

## 7. CI workflow (`build-v8.yml`)

Modeled directly on `build-skia.yml`:

- `workflow_dispatch` inputs: `v8_version` (default pinned tag), `platforms`
  (`all` or comma list), `skip_release`, `test_mode`, **`run_validation`**
  (default `true`).
- `concurrency` group keyed on workflow+ref+version+platforms (same idea as skia).
- `permissions: contents: write` for releases.
- **matrix** include entries: `mac/arm64`, `mac/x86_64`, `mac/universal`,
  `linux/x64`, `win/x64` (+ commented-out `linux/arm64`, `win/arm64` for later,
  exactly as skia-builder stages future arches).
- Per-job steps mirror skia: free disk space (Linux), install ninja, setup
  python, install LLVM 19 (non-mac), **cache `depot_tools` + V8 src** keyed on
  version + `hashFiles('build-v8.py')`, build, **seal**, **validate** (§8),
  package, `upload-artifact`.
- `create-release` job: `softprops/action-gh-release`, `tag_name` = V8 version
  tag (e.g. `v8-12.4.254`), attaching each per-platform/arch zip. Mirrors skia's
  release job. No XCFramework job (no Apple-bundle target here).

Caching note: V8 source + build is large; reuse skia-builder's disk-freeing step
on Linux and shallow-sync where possible.

## 8. Validation lane — "prove it works on Windows/Linux like we think"

This is a first-class requirement, not an afterthought. The reference doc marks
Linux/Windows V8 as **"not validated"** today — this repo's job is to *change
that bit to validated*, automatically, on every build.

`validate/` builds a tiny CMake binary that reproduces Pulp's actual structural
guarantee: V8 in one TU, Skia/Dawn in another, joined only at link.

The `validate-v8` CI step (runs on the **same** runner right after each build,
and standalone via `validate-v8.yml` against a published release):

1. **Download the matching Skia artifact** from `skia-builder` releases for the
   same platform/arch (so we link against the *real* Skia, not a stub).
2. **Symbol audit (static):** assert the sealed V8 archive exports zero flat
   ICU/zlib symbols and non-zero `v8::` symbols (`nm`/`readelf`/`dumpbin`).
   Fail the build if sealing regressed.
3. **Link audit:** actually link `smoke_v8.o + smoke_gpu.o + libv8_monolith +
   libSkia` into one executable. A duplicate-symbol error here = hard fail.
   This is the test that catches the exact class of bug the doc warns about.
4. **Runtime smoke:** run the binary. It must:
   - eval JS in V8 (`2+2`, a small Three.js-ish module load) and assert result;
   - if i18n on, exercise `Intl` to prove V8's sealed ICU still has its data;
   - draw a rect with Skia to a surface and assert a pixel/PNG hash;
   - (stretch) init Dawn/WebGPU device to prove the GPU side coexists.
5. **Report:** validation result goes into the artifact `manifest.json`
   (`validated: true`, platform, runner image, V8 + Skia versions). A release is
   only published if validation passed (gate `create-release` on it).

Windows specifics to validate explicitly: `/MT` vs `/MD` CRT agreement, and that
`dumpbin` shows no competing ICU defs. Linux specifics: libc++ vs libstdc++
agreement with Skia, and `readelf` symbol localization.

> Net: a green `build-v8.yml` run *means* "this V8 links and runs next to Skia on
> this platform/arch," because the workflow refuses to release otherwise.

## 9. Artifact + consumer contract (Pulp / iPlug)

Each release asset `v8-build-<platform>-<arch>-release.zip` contains:

```
include/                 # v8.h, libplatform/, cppgc/, v8config.h, ...
lib/  libv8_monolith.a   # (or v8_monolith.lib on Windows), sealed
manifest.json            # v8 version, gn args, ABI flags, validated:true
```

Pulp consumes it through the **existing** CMake provider contract documented in
`v8-with-skia-dawn-for-oli.md` (`core/view/CMakeLists.txt`):

- `V8_INCLUDE_DIR` → unzipped `include/`
- `V8_LIB_DIR` → unzipped `lib/`
- `V8_LIBRARY_PATH` → full path to `libv8_monolith.a`
  (the auto-search order already includes `v8_monolith`, so on macOS/Linux this
  may "just work" without `V8_LIBRARY_PATH`).

This means Pulp's `js_v8_engine.cpp` / choc `choc_javascript_V8.h` layer is
**unchanged** — we're swapping the *provider blob*, not the integration. One thing
to verify during review: that choc's V8 wrapper compiles against `v8_monolith`
headers the same way it does against `libnode`'s headers (header layout differs:
node ships V8 headers under `include/node/`).

## 10. Decisions to settle (flag for Oli / review)

- **D1 — V8 version pin.** Match the V8 inside the `libnode` we use today
  (Node 26 → V8 ~13.x?) for API parity, or pin to a clean upstream V8 tag and
  re-validate choc against it? Recommend: pin to a V8 tag and confirm choc builds.
- **D2 — i18n / ICU on or off.** *On* = `Intl` works, libnode parity, but requires
  the sealing step and ~10 MB duplicate ICU data at runtime (same as libnode does
  today). *Off* (`v8_enable_i18n_support=false`) = no ICU in V8 at all → zero
  collision risk, smaller, but loses `Intl`. Recommend: **on** for mac/linux
  (parity), consider **off** for the first Windows bring-up to de-risk.
- **D3 — `v8_enable_sandbox`.** V8 sandbox/pointer-compression has ABI and
  embedding implications; pick a setting and hold it constant across platforms.
- **D4 — Drop `libnode` on macOS immediately, or run both lanes in parallel?**
  Recommend: keep `libnode` as fallback until the sealed monolith passes
  validation on macOS, then flip Pulp's default.
- **D5 — Do we also need a shared lib (`.dylib`/`.so`/`.dll`)** or is the static
  monolith sufficient for Pulp/iPlug? (Static assumed in v1.)

## 11. Phased rollout

1. **Phase 0 — macOS arm64, i18n-on, sealed.** Reproduce `libnode`'s sealed-ICU
   property from source and pass the validation lane against a real Skia artifact.
   This proves the hard part once.
2. **Phase 1 — macOS x86_64 + universal**, flip Pulp default off `libnode` (D4).
3. **Phase 2 — Linux x64**, full validation. First *new* platform.
4. **Phase 3 — Windows x64**, `/MT`, full validation. Highest-risk ABI surface.
5. **Phase 4 — arm64 Linux/Windows** (uncomment matrix rows), CPU/other variants.

## 12. Open risks

- Sealing technique may differ subtly per linker/toolchain version; the
  validation lane is what de-risks this — treat red validation as "sealing
  recipe needs work," not "ship anyway."
- V8 build is heavy (depot_tools, large workspace, long builds). Caching + shallow
  sync mitigate, but expect longer CI than skia-builder.
- Skia's exact ABI flags must be read out of skia-builder and matched; a drift
  there is silent until runtime. Pin both and record in `manifest.json`.

---

*This is a pre-review draft. Next: Codex review (pass 1), then RepoPrompt oracle
review (pass 2), then revise and discuss.*
