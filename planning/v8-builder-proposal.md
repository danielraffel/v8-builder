# v8-builder — Proposal

**Status:** Draft v1 (pre-review). Will be revised after 2 review passes (Codex + RepoPrompt oracle).
**Author:** Daniel + Claude
**Date:** 2026-06-03
**Audience:** Oli (skia-builder author) + Pulp maintainers

---

## 1. Goal

Build **standalone, redistributable V8 (`v8_monolith`) static libraries** for the
platforms/architectures Pulp needs, on GitHub Actions, triggered the
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

## 1b. Premise check — is this even the right boundary? (adversarial pass 3)

Codex's strongest objection: **this may be overbuilt.** macOS already has a working
sealed-ICU provider (`libnode`), and QuickJS is the portable default everywhere. A
static `v8_monolith` is the *hardest* ownership model — Chromium toolchain churn,
V8 security-patch pressure, ICU/zlib sealing, STL/CRT matching, per-linker behavior.
So we should hold this gate honestly:

- **This is justified only if Linux/Windows V8 is a real shipping requirement**, not
  "nice to validate someday." If it isn't, the cheaper paths win: keep `libnode` on
  macOS, ship QuickJS on Linux/Windows, and stop.
- **If V8-on-Linux/Windows IS required, reconsider the boundary** before committing
  to static sealing: a **shared library** (`.so`/`.dll`/`.dylib`) that we build to
  seal ICU/zlib internally — i.e. a `libnode`-style provider we control — may be a
  *more natural* sealing boundary than a static archive, because dynamic libraries
  have a real export table to hide symbols behind (the exact mechanism that makes
  `libnode` work today). Static monolith vs. controlled shared lib is **decision D5,
  and it should be settled in Phase 0**, not assumed. The rest of this doc assumes
  static because that's what the contract asks for, but the shared-lib alternative
  is live and may be less work.

> Bottom line: don't build a general static i18n-on cross-platform V8 because it's
> impressive. Build the *minimum* provider that makes V8 ship on the platforms that
> actually need it — and only after the validation harness can prove a bad build bad.

## 2. Why this is *not* just "skia-builder for V8"

`skia-builder` and a V8 builder share ~80% of their machinery (both are
Chromium-lineage: `depot_tools` + `gclient`/`gn`/`ninja`). The build script and
CI matrix can be modeled almost 1:1 on skia-builder. **That part is easy.**

The hard 20% — and the entire reason this repo needs to exist — is captured in
`v8-with-skia-dawn-for-oli.md`:

> "budget the work as *make V8's ICU/zlib invisible and ABI-match Skia* — that,
> not compiling V8, is the actual hard part."

Two failure modes a naïve `v8_monolith` build hits when linked next to Skia:

1. **Duplicate ICU/zlib/Abseil symbols.** Both V8 and Skia/Dawn bundle ICU, zlib,
   **and Abseil** (Phase-0 finding §12b: a V8↔Dawn Abseil ODR collision aborts the
   binary). Skia ships ICU as a static archive with **plain, unversioned** symbols
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

**Separate repo, shared contract (pass 3).** Keep `v8-builder` separate from
`skia-builder` — V8 sealing, snapshots, i18n, and release cadence are different
enough that folding them in would pollute Oli's working repo with V8-specific
failure modes. But to avoid long-term drift (both Oli and Daniel host/run both),
define a small **shared contract** the two repos agree on: identical artifact
layout, a common `manifest.json` schema, common platform/arch names, and reusable
GitHub Actions snippets. Only merge into a single repo if the *output* ever becomes
one paired "Skia+V8 SDK" release.

**`seal/` is a policy + backends, not one script (pass 3).** Sealing is really a
platform-specific linker pipeline plus an audit policy, so structure it as
`seal/policy.json` (public-symbol policy, deny-list of ICU/zlib prefixes) +
`seal/macho.py` + `seal/elf.py` + `seal/coff_research.md` (the unsolved Windows
spike), not a monolithic `seal-symbols.py`. Keeps audits and per-platform
differences from becoming ad hoc.

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

ABI-matching knobs (**must equal Skia's**). Corrected against the actual
skia-builder `build-skia.py` GN args (Codex review, pass 1):

```gn
use_rtti = false                       # V8's arg is `use_rtti`, NOT `v8_enable_rtti`
                                       # (no v8_enable_rtti exists). Skia is -fno-rtti / GR-.
# exceptions: V8 and Skia both default -fno-exceptions. Keep aligned.
```

> **Correction — libc++/STL.** Skia (`build-skia.py`) does **not** set
> `use_custom_libcxx`, so on **Linux** it uses the default `clang++` STL, which is
> **`libstdc++`, not `libc++`** — our V8 build must match that exact STL or we get
> silent ABI breakage. Do **not** assume "system libc++." Read the resolved STL out
> of a real Skia Linux build and pin V8 to it. On macOS both use libc++; on Windows
> the relevant knob is the CRT, below.

> **Windows CRT.** skia-builder sets `extra_cflags = ["/MT"]` (or `/MTd` Debug) at
> `build-skia.py`. V8 must use the **same static CRT (`/MT`)**. `/MD` vs `/MT`
> mismatch is a classic, silent embed failure — the validation lane inspects per-
> object `/MT` vs `/MD` directives, not just whether the final link succeeds.

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

Per-platform technique (corrected after Codex pass 1 — the naïve per-object
`--localize-symbols` approach in draft v1 was **wrong**):

- **Key correction:** `-fvisibility=hidden` does **not** stop duplicate-symbol
  errors (archive members are still `GLOBAL`), and **per-object localization
  breaks V8's own cross-object references** to ICU/zlib. The credible recipe is a
  **single-object internalization**: partial-link the *entire V8 closure* into one
  relocatable object first, then localize everything that isn't public API:
  - **Linux:** `ld -r` (or `--whole-archive` into a relocatable) to fold
    `libv8_monolith.a` into one `.o`, then `objcopy --keep-global-symbols=public.txt`
    (note: `--keep-global-symbols` keeps the *listed* names global and localizes the
    rest — the inverse of `--localize-symbols`, which the v1 draft had backwards).
    Mind COMDAT/weak sections during the fold (cf. MaskRay's relocatable-linking
    notes). Assert with `readelf -sW`.
  - **macOS:** `ld -r -exported_symbols_list public.txt` single-object prelink.
    **`public.txt` cannot be just `v8::*`** — it must enumerate the *full mangled*
    V8 + `v8::platform` + `cppgc::` ABI surface the embedder actually links
    (generated by scraping the embedder's undefined symbols, not hand-written).
    Assert with `nm -gU`.
  - Audit asserts (both): **zero** global `u_*`/`ubrk_*`/`ucnv_*`/`uloc_*`/`zlib`
    symbols; **non-zero, complete** public V8/cppgc/platform symbols (an incomplete
    whitelist silently drops symbols the embedder needs → link fails downstream).
- **Windows (`.lib`) — NOT solved, treat as a research spike, not a checkbox.**
  A `.lib` has no export boundary, and `/WHOLEARCHIVE` does **not** seal symbols
  (it can make duplicate extraction *worse*). There is no clean COFF analogue to
  `objcopy --keep-global-symbols` out of the box. So for Windows v1 the realistic
  path is **decision D2: build with `v8_enable_i18n_support=false` (no ICU in V8 at
  all)** — this is the *only* credible first Windows lane, not merely
  "de-risking." A sealed-ICU Windows build (COFF single-object internalization, or
  a symbol-prefix/renaming strategy on V8's ICU) is a separate spike to prove
  before claiming Windows i18n parity. `dumpbin /symbols` is only an *audit*, never
  the seal.

The **public-symbol whitelist must be generated, not authored** — scrape the
undefined V8/cppgc symbols from Pulp's `js_v8_engine.cpp` + choc V8 wrapper object
files and use exactly that set, so the seal can never drop something the consumer
needs.

> **Audience decision (pass 2) — D7.** Scraping *only* Pulp's current undefined
> symbols yields a **Pulp-specific** binary: a future choc/Pulp change,
> could need a symbol we localized. If we want a **general** V8 embedder artifact,
> build the keep-list from V8's public ABI surface (`v8::` + `v8::platform` +
> `cppgc::`) and use Pulp's scraped set only as a *completeness check*, not the
> definition. Pick one (see D7); it changes how aggressively we seal.

This file (plus the Windows spike) is the genuinely novel part of the repo.
Everything else is skia-builder mechanics.

### 6b. Honest solution status & confidence per platform

> **RECOMMENDED PATH (settled with requester 2026-06-03): i18n ON + ship V8 as a
> SHARED library on all three platforms.** This makes the seal uniform (export table
> exposes only `v8::`/`cppgc::`, ICU/zlib stay internal — exactly how `libnode`
> works), and it **resolves the one "UNSOLVED" row below** (Windows static `.lib` +
> sealed ICU + Intl) by not using a static `.lib` for the flagship. Pulp already
> links a shared V8 (`libnode.dylib`) today, so this fits its contract. The static
> table below remains the reference for an optional i18n-off "lite" variant.

We do **not** have an equally-proven solution on all three *for the static case*.
Stated bluntly so we don't ship false confidence:

| Platform | i18n | Sealing mechanism | Confidence | Status |
|----------|------|-------------------|-----------|--------|
| **macOS** | on | (a) keep `libnode` (works today) **or** (b) static `v8_monolith` sealed via Mach-O `ld -r -exported_symbols_list` single-object internalization (two-level namespace helps) | **High** | (a) proven now; (b) well-trodden technique, unproven *in our context* |
| **Linux** | on | static `v8_monolith` sealed via ELF `ld -r` + `objcopy --keep-global-symbols` single-object internalization (COMDAT care) | **Medium-high** | real, widely-used technique; **not yet proven for us** |
| **Windows** | **off** | `v8_enable_i18n_support=false` → no ICU in V8 → nothing to seal, no collision | **High** | fully works; **loses `Intl`** |
| **Windows** | **on**, static `.lib` | none clean — `.lib` has no export boundary, `/WHOLEARCHIVE` doesn't seal, COFF has no clean localize-global | **Low / UNSOLVED** | open research spike |
| **Windows** | **on** | **build V8 as a DLL** whose export table exposes only `v8::`/`cppgc::` (a real export boundary — the actual analog to how `libnode` seals) | **Medium** | candidate, **not yet tried**; ties to D5 (static vs shared) |

**So the honest answer to "do we have solutions for all three?":**
- **macOS — yes** (two ways).
- **Linux — yes, pending proof** (standard ELF internalization).
- **Windows — yes *if* we accept one of:** (1) **i18n-off** (no `Intl`), which is a
  guaranteed lane today; or (2) ship V8 **as a DLL** so there's a real export
  boundary to seal behind. A **static `.lib` with sealed ICU and `Intl` on** is the
  one combination that is genuinely **not solved** and must be treated as a spike,
  not a plan.

**Two cross-platform candidates worth a Phase-0 spike** (could simplify *all*
platforms incl. Windows, but are **unverified** — do not assume):
- **ICU symbol renaming instead of localization.** ICU has a built-in
  symbol-suffix/namespace mechanism (`U_DISABLE_RENAMING`, version suffix). If we
  build V8's ICU with a distinct suffix (e.g. `*_v8`) while Skia keeps bare names,
  the symbols simply *don't collide* — a compile-time rename that works on COFF too,
  no linker surgery. **Needs verification that V8's GN/ICU build allows this.**
- **DLL/shared boundary everywhere** (D5): the cleanest seal is an export table;
  static archives are the hard case on every platform, worst on Windows.

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

**CI design corrections (pass 2 — do NOT clone skia-builder's workflow verbatim):**

- skia-builder filters platforms by **substring** (`platforms == *matrix.platform*`)
  and only releases when `platforms == 'all'`, with a **hard-coded** asset list in
  the release job. For a repo where *validation is the product*, copy none of these
  three: parse `platforms` into an **exact list**, generate the release asset list
  **dynamically** from artifacts that passed validation, and gate the release on a
  dedicated **`validate-all` aggregation job** (not on `platforms == 'all'`). Allow
  a partial release only when explicitly requested, and mark the validated target
  set in the release `manifest.json`.
- **Pin runner images** (`ubuntu-24.04`, `macos-15`, `windows-2022` exact, not
  `*-latest`) and record the image version in `manifest.json`; floating images
  drift toolchains silently. Re-run `validate-v8.yml` against published artifacts
  after any runner/toolchain bump.

## 8. Validation lane — "prove it works on Windows/Linux like we think"

This is the **single most important part** of the project, not an afterthought.
The explicit bar from the requester is **"extreme confidence the embedding works
and no issues" on Linux and Windows** — the reference doc marks both as "not
validated" today, and a build is worthless to us until that bit flips honestly.
A release that hasn't passed this lane must not be published.

> **Critical false-pass correction (Codex pass 1):** a trivial "draw a rect" Skia
> smoke can pass *without ever pulling* `libskunicode_icu.a`, `libskshaper.a`, or
> zlib members from the archive — so it would falsely report "no collision." The
> validation must **force the collision paths**: exercise SkParagraph / SkShaper /
> SkUnicode (real ICU/HarfBuzz use) **and** PNG encode/decode (zlib), and link the
> relevant Skia archives under **`--whole-archive` / `/WHOLEARCHIVE`** so those
> object members are actually brought in alongside V8's. Otherwise "validated" is a
> lie.

`validate/` builds a tiny CMake binary that reproduces Pulp's actual structural
guarantee: V8 in one TU, Skia/Dawn in another, joined only at link.

The `validate-v8` CI step (runs on the **same** runner right after each build,
and standalone via `validate-v8.yml` against a published release):

1. **Download the matching Skia artifact** from `skia-builder` releases for the
   same platform/arch (so we link against the *real* Skia, not a stub).
   **ABI-provenance gate (pass 2):** "matching" is underspecified and hides skew.
   Pin `skia_release_tag` as a workflow input, **verify SHA256** of every Skia
   lib/header bundle, and require the Skia artifact to carry a machine-readable
   provenance manifest (exact Skia commit, compiler version, libstdc++/glibc
   baseline on Linux, Windows toolset + CRT directives, Dawn commit). Derive the
   validator's ABI flags from the **Skia + V8 manifests**, and **fail validation if
   any provenance field is unknown** — otherwise we only prove "works with whatever
   CI downloaded today," not "works with the Skia Pulp will actually consume."
   *(This likely requires a small upstream addition to skia-builder's own manifest —
   note as a cross-repo dependency.)*
2. **Symbol audit (static):** assert the sealed V8 archive exports zero flat
   ICU/zlib symbols and a *complete* set of public `v8::`/`cppgc::` symbols
   (`nm`/`readelf`/`dumpbin`). Fail if sealing regressed or the whitelist dropped a
   needed symbol. On Windows also inspect per-object `/MT` vs `/MD` directives.
3. **Link audit (forced collision):** link `smoke_v8.o + smoke_gpu.o +
   libv8_monolith` **with the Skia ICU/shaper/zlib archives under whole-archive**
   into one executable. Duplicate-symbol error = hard fail. This is the test that
   catches the exact bug class the doc warns about — and only counts if the
   collision-bearing members are actually pulled (see correction above).
4. **Runtime smoke (exercises the collision paths):** run the binary. It must:
   - eval JS in V8 (`2+2`, a small ES-module load) and assert the result;
   - if i18n on, exercise `Intl` to prove V8's sealed ICU still resolves its data;
   - render **text via SkParagraph/SkShaper** (forces Skia's ICU+HarfBuzz) and
     **encode a PNG** (forces zlib), asserting a pixel/byte hash — *not* a bare rect;
   - (stretch, **non-gating**) init a Dawn/WebGPU device. Real WebGPU on stock
     Linux/Windows runners is flaky (no GPU/adapter, headless) — keep Dawn init
     optional or use a null/SwiftShader backend; **do not gate the release on it**.
     The deterministic gates are: V8 eval, SkParagraph/SkShaper, PNG encode/decode.

**"No room for hallucination" means asserting IDENTITY, not pixels (pass 3 — the
single most important correction to the test harness).** A screenshot/golden image
on headless CI is *easy to fake*: it can render through SwiftShader / WARP / a null
adapter / llvmpipe / **CPU Skia**, show a cached frame, or — worst of all — be
produced by a **fallback JS engine (QuickJS) while V8 never loaded**, and still
"look loaded." Async JS/module work and GPU fences can also race the capture. So the
harness must make a green result *impossible without the real path*:
   - **Engine identity, unfakeable:** the JS must compute something only V8 can, and
     the binary must assert at runtime that the engine is V8 from *our* artifact —
     e.g. read back `v8::V8::GetVersion()` and assert it equals the version recorded
     in `manifest.json`, and assert the symbol came from our sealed lib (build the
     fixture with the fallback engines compiled out, so a QuickJS pass is a link
     error, not a silent substitution).
   - **GPU backend identity:** if the Dawn path runs, assert the actual adapter /
     backend type (and fail/skip explicitly on software adapters) — never infer
     "GPU works" from pixels. Because backends are flaky on CI, GPU is **non-gating**
     and its real-vs-software status is *recorded*, not assumed.
   - **Collision-path proof:** assert (via the link/symbol audit, not the image)
     that the ICU/shaper/zlib members were actually pulled — the screenshot is the
     *last* signal, never the proof.
   - **Determinism:** drive JS→render synchronously (await module + microtasks +
     GPU fence) before capture; compare a tolerance-bounded hash of a region we
     control, and store the image as an artifact for human spot-check. The hash is a
     change-detector, not the pass criterion; the pass criterion is the identity
     assertions above.

**The release gate compiles the *real consumer path*, not just synthetic TUs (pass
2):** make the primary, gating validation target a **Pulp/choc-shaped compile**
— vendor a minimal fixture derived from `js_v8_engine.cpp` + `choc_javascript_V8.h`
and build it through the *same* CMake variables Pulp uses (`V8_INCLUDE_DIR`,
`V8_LIB_DIR`, `V8_LIBRARY_PATH`). This catches compile-time **define drift**
(sandbox/pointer-compression), header-layout issues, and choc API expectations that
a hand-written `smoke_v8.cpp` would miss. Keep the synthetic forced-collision
binary as an *additional* stress test, not the gate.

**Local-mac proof does NOT transfer to Linux/Windows (pass 3).** "Test against Pulp
on my Mac, then trust win/linux CI" leaves a real gap: different linkers, STL/CRT,
file layouts, plugin packaging, and sandbox/pointer-compression runtime behavior.
The confidence bar must be **"a Pulp-linked binary compiles, links, and runs the
screenshot harness on *each* target OS,"** not "generic validator passed + macOS
Pulp passed." So the Pulp-shaped compile+link+run + screenshot harness runs in CI on
**all three OSes**, and every run archives logs, symbol audits, executable SHA256,
and the screenshot artifact for inspection.
5. **Report + gate:** result goes into the artifact `manifest.json`
   (`validated: true`, platform, runner image, V8 + Skia versions, STL/CRT flavor).
   `create-release` is **gated** on validation passing for every requested target.

Linux specifics: prove **libstdc++-vs-libc++ agreement with the real Skia build**
(not assumed) and `readelf` symbol localization. Windows specifics: `/MT` agreement
at the object level and absence of competing ICU defs at link.

**Where validation runs (per requester):** the lane is designed to run on **stock
GitHub-hosted runners** (`ubuntu-latest`, `windows-2022`) *and* on a **self-hosted
Tart CI runner** — same `validate/` CMake harness either way, selected by a
`runs-on` label input. Stock runners give reproducible, public-CI confidence;
the Tart runner lets us validate on a controlled image / arch (e.g. Apple-silicon
Linux VMs, or a pinned Windows image) when we want belt-and-suspenders confidence.
We will run **both** before declaring a platform validated. *(Open: confirm the
Tart runner's exact OS/arch coverage and whether it can host Windows — see D6.)*

> Net: a green `build-v8.yml` run *means* "this V8 links and runs next to a real
> Skia (text + PNG paths exercised) on this platform/arch," because the workflow
> refuses to release otherwise. That is the only honest basis for "extreme
> confidence" on Linux/Windows.

## 9. Artifact + consumer contract (Pulp)

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

**Single-library-path trap (pass 2):** the `V8_LIBRARY_PATH` contract assumes one
archive is enough, but embedders need `v8::platform::*` (and `cppgc`), and
depending on V8's build layout `v8_libplatform` may **not** be folded into
`v8_monolith`. If it stays separate, a single path is insufficient. **Phase 0 must
prove one of:** (1) our `v8_monolith` already contains all required
`v8::platform`/`cppgc` symbols (assert in the symbol audit); (2) we post-link a
*combined* static lib that preserves the single-path contract; or (3) we extend the
provider contract to accept a `V8_LIBRARIES` list. Decide before building Linux.

## 9b. Versioning & release tags (the `m149`-equivalent)

We want the same "pin a human-meaningful tag and trust it" ergonomics skia-builder
gives via `chrome/m149` — but for V8, and aligned so Pulp can pair the two with
confidence.

**The pairing reality (corrected after P1d).** There is **no upstream-blessed Skia+V8
match here, and we don't need one.** V8 and Skia/Dawn share **no C++ types** (they talk
via serialized `choc::value`) and each bundles its **own sealed** ICU/zlib/Abseil — so
coexistence depends on the ABI boundary (libc++/STL, RTTI, pointer compression) + the
seal, **not** on version matching. Empirically: we shipped **V8 15.1.0** against Skia
**`chrome/m149`** (≠ the same Chromium revision — Chrome 149's V8 was ~13/14.x) and they
coexist cleanly. So a release records a **validated pair** (exact V8 build + the exact
Skia release it was tested against, SHA-pinned in the manifest) — a *proof of
coexistence*, not a milestone-match claim. A truly co-tested pair would require building
**both** Skia and V8 from the *same Chromium DEPS revision* — a possible future option,
not what skia-builder/v8-builder do today. The tag stays human-meaningful but the
manifest's validated-pair fields are the truth (D8/D9).

**Tag scheme (proposed).** V8 has no `mNNN` branch names; it uses version numbers
(e.g. `13.x.y.z`) and git tags, while Chromium milestones (`M149`) map to a V8
version via chromiumdash. So a v8-builder release tag should encode **both** for
traceability:

```
release tag:  m149-v8-13.6.233.8
              ^^^^                 Chromium milestone (matches the Skia branch Pulp pairs with)
                   ^^^^^^^^^^^^^^  exact upstream V8 version we built (reproducible)
```
(Example version string only — the real M149↔V8 mapping is resolved at build time.)

- `build-v8.yml` input `v8_version` accepts either a milestone (`m149`, resolved to
  the canonical V8 version via a pinned chromiumdash lookup recorded in the repo) or
  an exact V8 tag. Default = the milestone matching skia-builder's current default.
- Release body links the **upstream V8 release notes** (v8.dev / chromiumdash entry
  for that milestone) the way skia-builder links Skia's `RELEASE_NOTES.md`.
- `manifest.json` records: Chromium milestone, exact V8 version + git commit, the
  **exact Skia release tag the artifact was validated against**, and the resolved
  ICU version on both sides. "Validated against `chrome/m149`" is a first-class
  field, so Pulp knows which Skia this V8 is *proven* to coexist with.

**The tag is ergonomics; the manifest is the truth (pass 3).** Milestone + Skia
branch + V8 tag are *not* a perfect triangle — V8 carries branch-head cherry-picks
and security patches, and "Skia chrome/m149" can be rebuilt later with a different
compiler/Dawn revision under the same branch name. So the tag is for humans, and the
manifest is authoritative and must pin **exact revisions, not just milestone
labels**:

```
chromium_milestone        v8_version_tag        icu_version_v8
chromium_deps_revision     v8_git_sha            icu_version_skia
skia_release_tag           skia_git_sha          dawn_git_sha
skia_artifact_sha256       v8_artifact_sha256    validator_commit
```

If milestone-pairing is the safety story, **resolve V8 from a pinned Chromium DEPS
revision**, not from milestone metadata alone.

**Pulp consumption — a pair lockfile, not two independent tags (pass 3).** Pulp
pins the v8-builder tag for ergonomics, but the real contract is a **validated pair
lockfile** recording `{skia_artifact_sha256, v8_artifact_sha256, validated_pair_id}`.
That turns the pin into a reproducible, proven pairing rather than a vague
"chrome/m149 ↔ some V8" compatibility claim that ages badly when either side is
rebuilt. Bumping is still a one-line change, but it changes the *pair*, and the pair
is what validation certified. *(Decisions D8 + new D9 below.)*

## 9c. Embedding operations (don't mistake "first green link" for "shippable")

Pass-3 reviewers both flagged that from-source V8 efforts stall *after* the first
successful link, on operational details. Plan these up front:

- **Snapshot / startup data:** we embed (`v8_use_external_startup_data=false`), so no
  loose `.bin` — validate the embedded snapshot actually initializes.
- **Runtime init order:** the consumer must init in the right order — `v8::Platform`
  (libplatform) → ICU (if i18n-on) → `V8::Initialize` → `ArrayBuffer::Allocator` →
  isolate. Document it; the validation fixture must exercise exactly this order.
- **Pointer-compression / sandbox cage:** `V8_COMPRESS_POINTERS` / `V8_ENABLE_SANDBOX`
  defines **must match** between our build and the consumer TU or it's silent UB
  (D3). The Pulp-shaped compile check enforces this.
- **Binary size budget:** record per-platform artifact + linked-size delta in
  `manifest.json`; V8 is ~1 MB+ and i18n adds ~10 MB ICU data. Set a budget so a
  regression is visible.
- **Debug symbols:** decide whether to ship stripped + a separate symbol artifact
  (`.dSYM`/`.pdb`/split-debug) for crash triage.
- **License / SBOM:** V8, ICU, zlib, and depot_tools deps have obligations —
  generate an SBOM + bundled license file per release.
- **Security / update cadence:** V8 ships security fixes frequently. Define a bump
  policy (how often we re-pin, how fast we can cut a patched release) so we're not
  stuck on a vulnerable V8.

## 10. Decisions to settle (flag for Oli / review)

- **D1 — V8 version pin. → PIN MODERN V8 14.x (Codex pass-3 + Phase-0 finding).**
  Node 26's libnode is **V8 `14.6.202.33-node.19`** (not "~13.x"). **Phase-0 finding
  (2026-06-03):** Pulp's choc `choc_javascript_V8.h` does **not compile** against
  V8 14.6 — it uses removed APIs (`v8::Context::GetIsolate()`, `String::Utf8Length`,
  `String::WriteUtf8`). Decision: do **not** retreat to old V8 to satisfy stock choc
  (that makes v8-builder a compatibility relic and ships a stale/insecure engine).
  Instead **pin an exact modern V8 tag/commit** (V8 is at **15.1.27** as of
  2026-06-03 and ships *many releases per day* — so this is a deliberate pinned bump
  aligned to the paired Skia milestone, never a floating "latest") and **maintain a
  `choc` modern-V8 patch as part of the consumer-integration gate** (the patch is the deliverable in
  `patches/choc-v8.14.patch`; replacements: `Isolate::GetCurrent()`, `Utf8LengthV2`,
  `WriteUtf8V2(...,WriteFlags::kReplaceInvalidUtf8)`; watch dynamic-import-callback
  signature, `ScriptOrigin`, ArrayBuffer `BackingStore`). "Stock choc compiles" is no
  longer the target; "modern V8 compiles + runs Pulp" is.
- **D2 — i18n / ICU on or off. → RECOMMEND ON (general-artifact decision).** Pulp
  *itself* uses no `Intl` (grep 2026-06-03: zero `Intl`/locale usage in Pulp runtime
  JS or bundled `three.webgpu.js`; the one `localeCompare` is build-time Node
  tooling). **But this is a public artifact Oli and others will embed (D7 = general),
  and a V8 without `Intl` is a crippled V8 most embedders reject.** So keep i18n
  **on** for the flagship build. That *requires* solving the ICU seal — which is why
  D2 drives D5: **the clean way to seal ICU with i18n on, on every platform incl.
  Windows, is a shared library** (export-table boundary). Optionally also publish a
  static, i18n-off "lite" variant for size-sensitive embedders.
- **D2b — Linux STL.** Match V8 to whatever STL the real Skia Linux build resolves
  to (likely **libstdc++**, since Skia doesn't set `use_custom_libcxx`). Confirm
  empirically and pin; do not assume libc++.
- **D3 — `v8_enable_sandbox` / pointer compression.** ABI- and embedding-relevant;
  pick a setting, hold it constant across platforms, and **validate the matching
  defines in the consumer TU** (choc/Pulp must compile with the same
  `V8_ENABLE_SANDBOX`/`V8_COMPRESS_POINTERS` defines or it's silent UB).
- **D4 — Drop `libnode` on macOS immediately, or run both lanes in parallel?**
  Recommend: keep `libnode` as fallback until the sealed monolith passes
  validation on macOS, then flip Pulp's default.
- **D5 — Static vs shared. → RECOMMEND SHARED (`.dylib`/`.so`/`.dll`) as the
  flagship.** With i18n **on** (D2), a shared library is the *clean, uniform* seal on
  all three platforms: the export table exposes only `v8::`/`cppgc::` and ICU/zlib
  stay internal — **exactly how Homebrew `libnode` already seals ICU on macOS today.**
  Decisive point: **Pulp's current macOS V8 lane already links a shared lib
  (`libnode.dylib`), so shared is the proven shape for Pulp's `V8_LIBRARY_PATH`
  contract — not a new burden.** It also *solves Windows-with-`Intl`* (the one
  unsolved static case in §6b) because a DLL has a real export boundary. Cost:
  consumers ship + load the dynamic lib (`@loader_path`/`$ORIGIN`/DLL-search) — but
  audio plugins already bundle dylibs/DLLs, and Pulp already does this for libnode.
  *(Keep static i18n-off as an optional secondary artifact.)*
- **D6 — Tart CI runner coverage.** Confirm the self-hosted Tart runner's exact
  OS/arch (Apple-silicon Linux VM? Windows-capable?) so we know which validation
  targets it can host vs. which must stay on stock GitHub runners. Validation runs
  on **both** before a platform is declared validated.
- **D9 — Pair lockfile vs independent tags.** Does Pulp consume a validated
  `{skia_sha256, v8_sha256, validated_pair_id}` lockfile (reproducible, proven
  pairing) or two independent tags (simpler, but a rebuilt-Skia-same-branch can
  silently break the pairing)? Recommend the lockfile.
- **D8 — Exact release-tag format & milestone source of truth.** Lock the tag
  string (proposed `mNNN-v8-<version>`), and decide where the canonical
  `mNNN → V8 version` mapping lives (pinned chromiumdash lookup committed to the
  repo vs. resolved live at build time). Must stay parseable by Pulp's dependency
  pin and aligned with the Skia branch Pulp uses.
- **D7 — Artifact audience. → GENERAL (confirmed by requester 2026-06-03):** "folks
  in other places will want to use" it. So seal to V8's **full public ABI**
  (`v8::`+`v8::platform`+`cppgc::`), using any Pulp scrape only as a completeness
  check — never narrow the export set to just Pulp's current needs.

## 11. Phased rollout

Reordered after pass 2: the requester's bar is **Linux/Windows** confidence, so we
prove the cross-link and the new platforms early rather than spending two phases on
macOS first.

0. **Phase 0 — build the validation harness + positive/negative controls FIRST**,
   settle static-vs-shared (D5) and the single-library question (§9), and spike the
   ICU-rename candidate (§6b). The harness must **fail on an unsealed V8** (negative
   control) and on a **QuickJS-substituted** binary (engine-identity control), and
   **pass on a known-good combo** — *before* any green is trusted. macOS arm64 is the
   cheapest place to debug the sealing recipe, but treat compiling V8 as the easy
   part, not as progress.
0b. **Phase 0b — Windows i18n-off thin vertical slice, EARLY.** Don't wait for
   mac/linux polish: build V8 (i18n-off), compile Pulp's V8 TU, link a minimal
   Pulp-shaped app, run the identity-anchored harness on Windows. Windows is the most
   likely place the *product shape* (APIs, flags, packaging, CRT) bites — prove it
   can carry the product before investing in the harder lanes.
2. **Phase 1 — Linux x64, i18n-on, sealed.** First *new* validated provider; the
   single-object internalization recipe gets proven on the platform we care about.
3. **Phase 2 — macOS arm64 + x86_64 + universal**, then flip Pulp's default off
   `libnode` (D4) once validation is green.
4. **Phase 3 — Windows x64, i18n-off, `/MT`.** Bring up the ABI/CRT lane *without*
   waiting on the unsolved ICU-sealing spike — get a validated Windows V8 sooner.
5. **Phase 4 — Windows sealed-ICU spike** (COFF internalization / symbol prefix),
   then arm64 Linux/Windows (uncomment matrix rows) and other variants.
6. **Phase 5 — iOS (deferred; requester 2026-06-03).** Only after *all* desktop
   lanes are verified and PRs landed. iOS V8 must be **jitless** (no JIT allowed in
   App Store builds) — `v8_enable_jitless=true` / `--jitless`, per
   <https://v8.dev/docs/cross-compile-ios>. Today Apple uses JavaScriptCore and it
   works fine; iOS-V8 is exploratory, not a blocker. Out of scope until then.
7. **Phase 6 — Android (deferred; after iOS).** NDK cross-build of the shared lib;
   reuses the sealing + validation machinery. Scope after iOS proves out.

## 12. Open risks

- Sealing technique may differ subtly per linker/toolchain version; the
  validation lane is what de-risks this — treat red validation as "sealing
  recipe needs work," not "ship anyway."
- V8 build is heavy (depot_tools, large workspace, long builds). Caching + shallow
  sync mitigate, but expect longer CI than skia-builder.
- Skia's exact ABI flags must be read out of skia-builder and matched; a drift
  there is silent until runtime. Pin both and record in `manifest.json`.
- **Public-symbol whitelist incompleteness** — if the generated keep-list misses a
  symbol the embedder links, the seal silently drops it and downstream link fails.
  Generate the list from the real consumer objects; re-check on every V8 bump.
- **V8 header/define drift vs choc/Pulp** — `choc_javascript_V8.h` expects a
  specific V8 API surface and header layout (libnode ships headers under
  `include/node/`). Confirm choc compiles against clean `v8_monolith` headers, and
  that sandbox/pointer-compression defines match in the consumer TU.

## 12b. Phase-0 findings (empirical, 2026-06-03)

Built Pulp's real `threejs-native-demo` with V8 against Homebrew libnode in a Pulp
worktree. Two concrete findings that change the plan:

1. **choc doesn't compile against modern V8.** `choc_javascript_V8.h` uses
   `v8::Context::GetIsolate()`, `String::Utf8Length`, `String::WriteUtf8` — all
   removed by V8 14.6. Fixed with a 3-hunk patch (`patches/choc-v8.14.patch`:
   `Isolate::GetCurrent()`, `Utf8LengthV2`, `WriteUtf8V2`); demo then builds + links.
   → D1: pin exact modern V8 + carry a choc patch as the integration gate.

2. **Abseil ODR collision between V8 and Dawn (the big one).** With the demo built,
   it **aborts at runtime** in `absl::container_internal raw_hash_set PerTableSeed`
   *inside libnode*, right after Skia/Dawn init, before any render. Root cause,
   confirmed by `nm`: **libnode flat-exports 558 `absl` symbols** and
   **`libdawn_combined.a` carries 3212**; the executable flat-exports **688** — two
   different Abseil copies in one binary → swisstable seed/layout ODR → abort.
   - This is the **ICU-collision story repeated for Abseil**, and the reference doc
     never checked it (it only `nm`'d ICU/v8).
   - **`libnode` is therefore NOT a clean coexistence provider with Dawn** — it
     doesn't seal Abseil. The "proven macOS V8 lane" predates this Dawn/absl combo.
   - **Sealing policy must cover `absl::` (and likely `protobuf`), not just
     ICU/zlib** (`seal/policy.json` updated). The from-source **shared lib that
     exports only `v8::`/`cppgc::`** is exactly the fix — Abseil stays internal and
     cannot interpose Dawn's.
   - Consequence for the rig: the libnode "positive control" can't be a clean green
     (libnode+Dawn genuinely crashes). The **harness behaved correctly — it refused
     to fake-pass** and surfaced a real abort.

3. **APPROACH VERIFIED (2026-06-03).** Hiding `absl` from the linked binary's export
   table (`-Wl,-unexported_symbols_list` on the exe — the consumer-side equivalent of
   what a from-source **shared V8 exporting only `v8::`/`cppgc::`** does by
   construction) **eliminates the abort**: the real Three.js cube renders
   (1640×1120 PNG) with **V8 14.6.202.33-node.19 + Metal/Dawn/Skia Graphite coexisting
   in one binary**, and the identity-anchored gate **PASSES** (engine + version +
   hardware-GPU asserted). Negative controls confirm honesty: unsealed (688 absl
   exports) → abort; wrong version → gate FAIL. **This empirically proves the D5
   export-table sealing approach resolves V8↔Skia/Dawn coexistence** — the remaining
   work is producing that sealed shared lib *from source* so we don't depend on
   libnode + a consumer link flag. The collision class (absl/ICU/zlib) and its fix
   (export only the V8 ABI) are now demonstrated end-to-end, not hypothesized.

## 13. Review log

- **Pass 1 — Codex (`gpt-5.5`, xhigh), 2026-06-03.** Confirmed the core thesis;
  corrected the sealing recipe (single-object internalization, not per-object
  `--localize-symbols`; `--keep-global-symbols` semantics; full mangled macOS
  export list); flagged **Windows sealing as unsolved → i18n-off is the only v1
  lane**; corrected Skia ABI facts (`use_rtti` not `v8_enable_rtti`; Skia uses
  default STL = libstdc++ on Linux, not libc++; `/MT`); and caught the
  **draw-rect false-pass** in the validation lane. All folded into §5/§6/§8/§10.
- **Pass 2 — RepoPrompt oracle (review mode), 2026-06-03.** Additive critique
  folded in: don't clone skia-builder's substring filtering / `==all` release gate
  / hard-coded asset list (§7); pin + SHA256-verify + provenance-check the Skia
  artifact used for validation, deriving validator ABI flags from manifests (§8);
  make the **release gate compile the real Pulp/choc V8 path**, not just synthetic
  smoke (§8); the `v8_libplatform`/`cppgc` single-library trap → prove or extend to
  `V8_LIBRARIES` (§9); Dawn runtime is non-gating/flaky (§8); reorder phases so
  **Linux and Windows-i18n-off come early** (§11); decide Pulp-specific vs general
  artifact (D7); pin runner images (§7).

- **Pass 3 — adversarial (Codex `gpt-5.5` + RepoPrompt oracle), 2026-06-03.**
  Codex: (1) **premise may be overbuilt** — static V8 only justified if Linux/Windows
  V8 truly ships; reconsider a controlled shared-lib/libnode-style boundary (§1b);
  (2) **screenshots can lie** (SwiftShader/null-adapter/CPU-Skia/QuickJS-fallback) →
  harness must assert engine + backend **identity**, not pixels (§8); (3) §9b
  milestone pairing is **traceability, not a technical guarantee** — sealing is the
  real fix; (4) local-mac proof is the *least* representative; (5) biggest waste =
  treating "compiled V8" as progress. Oracle: keep repos separate but share a
  contract (§3); manifest must pin exact DEPS/SHAs + a pair-lockfile (§9b); run the
  Pulp-shaped + screenshot harness on **all three OSes** (§8); add **Embedding
  operations** (§9c); move a **Windows thin slice early** (§11); `seal/` as
  policy+backends (§3). Added §6b honest per-platform solution status.

---

*Revised after Codex (pass 1), RepoPrompt oracle (pass 2), and an adversarial pass 3
(both). Open decisions D1–D9 need owner input before Phase 0. Companion runbook:
`planning/v8-builder-runbook.md`.*
