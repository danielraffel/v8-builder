# v8-builder — Build & Validate Runbook

**Status:** Executable plan. Ask me *"build Phase N from the runbook"* and I'll do it.
**Companion to:** `planning/v8-builder-proposal.md` (read that for the *why*).
**Date:** 2026-06-03 · **Last state update:** 2026-06-04

---

## 🧭 CURRENT STATE & REMAINING OPENS (2026-06-04) — READ FIRST

### Desktop build/seal/validate matrix (6 cells = 3 OS × Intel/ARM)
| Cell | Build | Seal | Coexist + identity validated | Status |
|------|:----:|:----:|:----:|---|
| macOS arm64 | ✅ | ✅ | ✅ | **DONE** — full Pulp threejs demo, identity gate PASS, real GPU |
| macOS x86_64 | ✅ | ✅ | ✅ | **DONE** — sealed dylib + standalone validator PASS under Rosetta (universal = lipo) |
| Linux x64 | ✅ compiles | ❌ **seal fails** | ⚪ | **CI run 26961155381 FAILED at seal link** — faithful build (V8 15.1.27, Temporal/Rust ON) reproduced the `ld.lld` duplicate-symbol bug (monolith pulled twice). Seal target NEEDS a fix (the Temporal-OFF "clean" result didn't hold). #1 open. |
| Linux arm64 | ⚪ | n/a | ⚪ | **OPEN** — cross-compile from x64 (or native on Tart arm64 VM) + validate on arm64 runner |
| Windows x64 | ⚪ | ⚪ | ⚪ | **OPEN** — DLL export-table seal lane NOT yet implemented in build-v8.py (it `SystemExit`s) |
| Windows arm64 | ⚪ | ⚪ | ⚪ | **OPEN** — after the Windows x64 lane exists |

### In flight
- **CI run [26961155381](https://github.com/danielraffel/v8-builder/actions/runs/26961155381)**: linux/x64, **V8 15.1.27** (== the LKGR-pinned v8 SHA in `planning/lkgr-lock.json`), validated against **Skia `chrome/m150`**, identity-anchored, `skip_release=true`. mac/win matrix jobs correctly short-circuit (platforms=linux/x64).

### Proven (don't redo)
- macOS arm64 + x86_64 sealed shared V8 15.1 coexists with Skia Graphite + Dawn (P1d, P2.1).
- Linux: V8 **compiles** on x86_64 CI; the ELF dup-symbol bug is understood (link-line ORDER — synthetic ld.lld 18 repro). BUT the **faithful Temporal-ON build still fails the seal link** (CI run 26961155381) — the Temporal-OFF "clean" result did NOT generalize. **The seal target still needs a fix.** See `seal/coff_research.md` top "CORRECTION (2026-06-04)".

### #1 OPEN — Linux ELF seal fix (faithfully reproduced)
The `v8_sealed_shared` ELF target links the monolith twice (deps plain copy + hand-rolled
`--whole-archive`); with the Rust/Temporal closure ON, lld pulls a member from both → duplicate
symbols. Fix: reference the monolith **exactly once**, whole-archived, while still pulling the
Rust closure — lead = Chromium's `-LinkWrapper,add-whole-archive` (`build/toolchain/whole_archive.py`)
instead of hand-rolled ldflags. **Reproduces on GitHub CI (~1h/iter) and would reproduce on a
Tart x86_64 Linux VM (Mac Studio) for a fast local loop.** This gates the whole Linux lane.

### Hard toolchain constraint to carry forward (the crux for ARM/emulation)
V8 15.1's bundled **clang (llvmorg-23) + Rust are x86_64-Linux-host ONLY** (`tools/clang/scripts/update.py`: `'linux'→'Linux_x64'`). Consequences:
- **Intel Linux/Windows** build natively with the bundled toolchain on x86_64 hosts — easy.
- **ARM Linux/Windows**: either **cross-compile from an x86_64 host** (`target_cpu="arm64"` + arm64 sysroot — how Chromium ships ARM), **or** run the bundled toolchain under **x86_64 emulation** (a Tart x86_64 VM, if emulation works → runs bundled clang/rust natively-in-emulation). On a *native* arm64 Linux host the bundled toolchain can't run (qemu-user segfaulted on it; system clang-18 has clang-23 flag skew → only good for toolchain-agnostic seal-link checks).
- The seal itself is **arch-independent** (symbol-level), already proven on both mac arches.

### Open items, prioritized (the new-session work)
1. **Fix the Linux ELF seal** (see "#1 OPEN" above) — faithfully reproduced on CI run 26961155381. Iterate on a Tart x86_64 Linux VM (Mac Studio) or GitHub CI; then re-run build-v8.yml to land linux/x64 green.
2. **Linux arm64**: add matrix row; cross-compile from x64 (or Tart arm64 VM); validate on arm64 runner/VM.
3. **Windows x64 DLL-export seal**: implement the `win` lane in `build-v8.py` (`.def`/`/EXPORT:` or component build → seal ICU/zlib/absl internal); validate on a Tart Windows VM. See `seal/coff_research.md` (DLL approach). Then **Windows arm64**.
4. **Intel/ARM sweeps**: every (os,arch) cell builds + validates; enforce intra-repo single-SHA alignment (FR1 2-D pairs).
5. **macOS A/B vs Homebrew libnode** (P2.2) → flip Pulp default off libnode (P2.3).
6. **SKIA-M150**: migrate Pulp m149→m150 AFTER V8 integration lands + PRs merge (task #20).
7. **Pairs/LKGR (FR1)**: generate `lkgr-lock.json` + shared release-manifest contract across skia-builder & v8-builder; assert cross-repo AND intra-repo alignment. (See `planning/feature-requests.md`.)
8. **iOS (jitless, threejs)** — after desktop lanes + m150.
9. **Android (NDK)** — after iOS.

### Why move to Mac Studio + Tart
More cores (faster V8 builds) and Tart can host **Windows + Linux VMs** (real Windows-lane iteration without GitHub round-trips) and possibly **x86_64 emulation** (so an emulated Intel VM runs V8's bundled clang/rust natively → unblocks Intel cells + ARM cells via cross/emulation). The QEMU+HVF loop used on the laptop (memory: `linux-vm-build-loop`) is the fallback; on Mac Studio prefer Tart.

---

> **Settled direction (2026-06-03):** flagship = **i18n ON + V8 as a SHARED library**
> (`.dylib`/`.so`/`.dll`) on all 3 platforms (export-table ICU seal, like `libnode`);
> audience = general. Build/seal steps below target that shape. An optional static
> i18n-off "lite" variant can follow.

This is the step-by-step, gated plan to: (1) set up the public `v8-builder` repo,
(2) build sealed V8 per platform, (3) validate it coexists with Skia/Dawn **locally
against `/Users/danielraffel/Code/pulp`** (our V8 vs Homebrew `libnode`), then (4) on
Windows/Linux in CI — with a screenshot harness that **cannot pass by hallucination**.

Grounded in Pulp's real contract (verified, not assumed):
- `core/view/CMakeLists.txt`: `PULP_JS_ENGINE=auto|quickjs|jsc|v8`, `V8_INCLUDE_DIR`,
  `V8_LIB_DIR`, `V8_LIBRARY_PATH`, `V8_LIBRARY_NAME`; auto-search names
  `v8_monolith node.141 node`; sets `PULP_HAS_V8=1`.
- `test/test_js_engine.cpp` + `test/CMakeLists.txt` → `pulp-test-js-engine`
  (`FOR_EACH_ENGINE` parametric suite).
- `examples/threejs-native-demo/` → `pulp-threejs-native-demo --demo cube --capture
  <png>` (real `THREE.WebGPURenderer` on V8 + Dawn) and `capture_test.cmake`.
- `core/view/platform/mac/screenshot_mac.mm` (mac capture exists; **win/linux capture
  is a gap to confirm**).

---

## 📊 Progress tracker (single source of truth — keep in sync with the task list)

Status legend: `TODO` · `WIP` · `DONE` · `BLOCKED` · `SKIP`. Update the **Status** and
**Notes** here whenever a task changes state, and mirror it 1:1 with TaskCreate/TaskUpdate.

| ID | Item | Status | Notes |
|----|------|--------|-------|
| P0.1 | Scaffold local repo (files, skeletons) | DONE | 18 files; build-v8.py CLI parses; stages exit 2 (honest skeleton) |
| P0.2 | Harden Pulp capture test → identity-anchored gate | BLOCKED | v8-builder side DONE (run_validation.cmake, no skip-pass); Pulp-side hook spec'd in planning/pulp-patch-P0.2.md — needs go (Pulp main has user WIP) |
| P0.2 | (Pulp hooks) demo builds+runs w/ V8 14.6 | DONE | required choc-v8.14 patch; identity probe prints clean block |
| P0.3a | Positive control: V8 ⟷ Skia/Dawn coexist, identity-verified | DONE ✅ | **VERIFIED**: hiding absl from exe exports (mimics shared-lib export seal) stops the ODR abort; real Three.js cube renders (1640×1120 PNG), gate PASS: V8 14.6.202.33-node.19 + Metal(hardware). Export-sealing approach (D5) empirically proven. |
| P0.3b | Negative control A: unsealed → fail | DONE ✅ | unsealed (688 absl exports) → PerTableSeed abort (exit 134); sealed (0 exports) → pass. Rig fails on the real collision. |
| P0.3c | Negative control B: wrong/substituted engine → fail | DONE ✅ | gate FAILS on version mismatch (14.6.x != fake). Engine-type!=v8 path verified in logic; full QuickJS-substitution build deferred (another full build). |
| P0.3b | Negative control A: unsealed V8 → link FAILS (dup ICU) | TODO | |
| P0.3c | Negative control B: substituted engine → FAILS identity | TODO | |
| P0.4 | Settle static-vs-shared spike (D5) + single-lib (§9) | DONE | shared, i18n-on (settled 2026-06-03) |
| P1a | build-v8.py pipeline + seal/macho.py | DONE | depot_tools/fetch/sync/gn/ninja + dylib seal (force_load + exported_symbols_list, nm audit) |
| P1b | Build v8_monolith from source (mac arm64, V8 15.1) | DONE ✅ | native 298M Mach-O arm64, 257k syms, contains absl(2645)/icu/zlib to seal. Fix: is_official_build=false (avoids ThinLTO + force-hidden new/delete SDK clash). Codex root-caused. |
| P1c | seal v8_monolith → libv8.dylib (only v8::/cppgc::) | DONE ✅ | gn v8_shared_library target (patches/v8-15.1-sealed-shared-gn.patch) deps :v8_monolith, force_load + -exported_symbols_list, remove dead_strip. **libv8.dylib 76M; nm -gU: 0 absl/icu/zlib internals exported, 68k v8 syms, V8::Initialize present.** Seal verified. |
| P1d | Validate sealed-from-source V8 ⟷ Dawn (no link flag) | DONE ✅✅ | **GOAL PROVEN (macOS arm64):** Pulp demo links OUR @rpath/libv8.dylib (not libnode), no link-flag hack. Identity gate PASS: engine=v8, runtime_version=15.1.0 (our build), Metal hardware, real Three.js cube rendered (1640×1120 PNG). Abseil collision gone by construction; pointer-compression aligned OFF (D3). choc compiles clean vs V8 15.1 with the existing patch. |
| P0b.1 | Windows x64 lane: implement in build-v8.py | TODO | build-v8.py currently `SystemExit`s for `win` (separate lane). Implement the DLL export-table seal (.def/`/EXPORT:` listing only v8::/cppgc:: → ICU/zlib/absl internal; or component build). Iterate on a **Tart Windows VM** (Mac Studio). build-win.sh is a skeleton. See seal/coff_research.md DLL approach. |
| P3.2 | Windows **arm64** lane + validate | TODO | after Windows x64 lane exists; cross from x64 or native on a Tart arm64 Windows VM. |
| P1.1 | Linux x64 sealed shared .so | SEAL FIX NEEDED (CI-reproduced) — **#1 open** | **CI run 26961155381 (faithful x86_64, V8 15.1.27, Temporal/Rust ON) FAILED at seal link** with `ld.lld: duplicate symbol v8::internal::AllowCompilation::...` (monolith `assert-scope.o` pulled twice: once whole-archive, once on-demand from the plain deps copy). A synthetic ld.lld-18 repro showed whole-archive-first is clean ONLY with Temporal OFF; the Rust closure changes the graph and the dup returns. FIX: reference the monolith exactly once, whole-archived, while pulling the Rust closure — lead = Chromium `-LinkWrapper,add-whole-archive` (build/toolchain/whole_archive.py). Iterate on Tart x86_64 VM or CI. See seal/coff_research.md top "CORRECTION (2026-06-04)". |
| P1.2 | Linux x64 validation (forced-collision + identity) | BLOCKED | gated on P1.1 seal fix (CI never reached the validate step — build failed at seal link). |
| P1.3 | Linux **arm64** sealed shared .so + validate | TODO | cross-compile from x64 (target_cpu=arm64 + arm64 sysroot) OR native build on a Tart arm64 Linux VM; validate on arm64 runner/VM. Seal is arch-independent (proven on mac). Add matrix row to build-v8.yml. |
| P2.1 | macOS shared lib (arm64, x86_64, universal), sealed | DONE ✅ | arm64 (P1d full pulp demo). **x86_64: sealed dylib built + audited (0 absl/icu exports) + standalone validator PASS under Rosetta** (V8 15.1 inits, eval=42, Skia ICU coexists). universal=lipo(arm64,x86_64) 149M. Full macOS Intel+ARM matrix proven. Installed Rosetta for the x86_64 run. |
| P2.2 | macOS A/B: ours vs Homebrew libnode through Pulp | TODO | identity proves the swap is real |
| P2.3 | Flip Pulp default off libnode (D4) | TODO | only after A/B green |
| P3.1 | Windows x64 i18n-on DLL validate (forced-collision + identity) | TODO | after P0b.1 lane builds; validate on Tart Windows VM + GitHub windows-2022. |
| ARCH-SWEEP | Intel/ARM sweep: all 6 desktop cells build+validate, single-SHA aligned | TODO | enforce intra-repo single V8 SHA across every (os,arch) artifact in a release (FR1 2-D pairs); no mixed-revision release. |
| PAIRS-FR1 | Generate lkgr-lock + shared manifest contract (skia-builder ⟷ v8-builder) | TODO | both repos emit same manifest fields; Pulp pairs by matching LKGR triple. See planning/feature-requests.md FR1. |
| PUB | Public repo | DONE ✅ | github.com/danielraffel/v8-builder (public, clean: no iPlug, no binaries). |
| P4.0 | Standalone validate/ harness (OS-agnostic) | DONE ✅ | Real V8-init + identity + forced flat-ICU coexistence. **Built+run locally on mac vs our sealed dylib: PASS** (V8 15.1.0 inits, evals 20+22=42, ICU coexists). Dawn-Abseil path via optional SKIA_DAWN_LIB (pulp demo already covers mac). CI-ready. |
| P4.1 | CI: build-v8.yml (GitHub-hosted) | PARTLY VALIDATED | **first real run 26961155381 (2026-06-04):** pipeline works through depot_tools→sync(15.1.27)→gn→ninja (2390 steps incl. Rust closure); FAILED only at the final seal link (P1.1 bug). So the CI harness itself is sound; re-run after the seal fix. win=separate DLL lane (P0b.1). |
| P4.2 | Release: mNNN-v8-<ver> tag + manifest + pair lockfile | TODO | |
| SKIA-M150 | Migrate Pulp m149→m150 (AFTER V8 integration lands+merges) | PLANNED | user 2026-06-04: land V8 integration FIRST; then bump Skia to m150, rebuild pulp, fix breakage (SkRegion::setRects→SkSpan; SkStrikeRef in TextShaper), push followup, verify libs. THEN continue sweeps + iOS/Android. |
| DEPS-PAIR | Investigate co-built Skia+V8 from one Chromium DEPS revision | PLANNED | user 2026-06-04: evidence-gather building BOTH from the same Chromium DEPS rev → truly co-tested pair (one shared absl/icu/zlib/Dawn), may even remove the dual-Abseil seal need. Future option; sequence after desktop lanes + m150. (task #21) |
| P5 | iOS (jitless, threejs) | PLANNED (task) | after desktop lanes + m150. v8.dev/docs/cross-compile-ios; NO JIT for release; threejs is the target (JSC works there today). |
| P6 | Android (NDK) | PLANNED (task) | after iOS. |

---

## ⚠️ Gate 0 — Decisions to lock before I build anything

These are from the proposal (D1–D9). **D2/D5/D7 are now settled with the requester**
(2026-06-03); the rest have recommended defaults — tell me to change any:

| # | Decision | Setting |
|---|----------|---------|
| **D2** | i18n / `Intl` | **ON** — public artifact; others will embed it and expect `Intl` |
| **D5** | static vs shared | **SHARED** (`.dylib`/`.so`/`.dll`) as flagship — clean ICU seal via export table on all 3 OSes incl. Windows; matches how Pulp links libnode today. Optional static i18n-off "lite" later |
| **D7** | audience | **GENERAL** — seal to full V8 public ABI, not a Pulp-narrowed set |
| D1 | V8 version pin | milestone matching skia-builder's default Skia branch (see §9b) |
| D4 | drop libnode on mac | keep as fallback until sealed build passes A/B |
| D3 | pointer-compression/sandbox | pick one, hold constant, assert define-match in Pulp TU |
| D8/D9 | tag format + pair lockfile | `mNNN-v8-<version>` + validated pair lockfile |

**With D2/D5 settled the build is unblocked.** The flagship is **shared, i18n-on**;
the Windows static-`.lib` "unsolved" case is sidestepped (we don't use a static
`.lib` for the flagship). Phase 0 (harness + controls) is safe to build regardless.

---

## Phase 0 — Repo scaffold + validation harness FIRST (no V8 build yet)

> Rationale (adversarial pass 3): *compiling V8 is the easy part and feels like
> progress while proving nothing.* Build the **proof rig before the product**, and
> prove it can fail, before trusting any green.

### 0.1 — Create the public repo
```bash
gh repo create danielraffel/v8-builder --public \
  --description "Build & seal standalone V8 (v8_monolith) for embedding next to Skia/Dawn" \
  --license MIT
```
Scaffold (mirrors skia-builder; LICENSE = MIT, credit pattern like Oli's):
```
README.md  CLAUDE.md  LICENSE  Makefile  .gitignore
build-v8.py            # argparse CLI + build class (skeleton)
build-win.sh
seal/policy.json seal/macho.py seal/elf.py seal/coff_research.md
validate/CMakeLists.txt validate/identity_main.cpp validate/run_validation.cmake
.github/workflows/build-v8.yml .github/workflows/validate-v8.yml
planning/  (copy this runbook + proposal in)
```

### 0.2 — Harden Pulp's capture test into an *identity-anchored* gate
The existing `examples/threejs-native-demo/capture_test.cmake` **skips** when V8 is
absent and only checks "PNG non-empty." That is the exact false-pass to fix. In Pulp
(branch, PR), add a strict validation entrypoint used only by v8-builder:

1. Add `pulp-threejs-native-demo --print-engine-identity` that prints
   `v8::V8::GetVersion()` + `PULP_HAS_V8` + the resolved provider path, and the Dawn
   adapter type + backend after device init.
2. New `validate/run_validation.cmake` (lives in v8-builder, invoked against the
   built demo) that asserts, with **no skip allowed**:
   - `PULP_HAS_V8 == 1` (build a QuickJS/JSC fallback → **link error**, not a silent
     pass: configure the fixture with other engines compiled out);
   - reported `GetVersion()` **==** the version in our artifact `manifest.json`;
   - the capture PNG exists, is > a real-content size threshold, and a
     tolerance-bounded hash of a controlled region is **non-blank** (not a cleared
     framebuffer);
   - GPU adapter type is recorded; **software adapter (SwiftShader/WARP/llvmpipe) =
     explicit "GPU-unverified" status, never silent pass**.
3. Drive JS→render synchronously (the demo already "primes several real frame
   callbacks before screenshotting" — keep/extend that) so the capture isn't a warm-up
   frame.

### 0.3 — Controls (this is the real Phase-0 deliverable)
- **Positive control:** build the demo today against Homebrew libnode
  (`V8_LIBRARY_PATH=/opt/homebrew/opt/node/lib/libnode.<N>.dylib`) → harness PASSES
  (proves the rig works on a known-good provider).
- **Negative control A (unsealed):** a deliberately-unsealed `v8_monolith` linked next
  to Skia → **must fail at link** with duplicate ICU symbols (proves the link audit
  bites).
- **Negative control B (substituted engine):** force QuickJS → **must fail** the
  identity assertion / link (proves a screenshot can't lie about which engine ran).

**Exit criteria:** all three controls behave as stated. Only then is "green" trustworthy.

---

## Phase 0b — Windows i18n-off thin vertical slice (EARLY, de-risk product shape)
Don't wait for mac/linux polish. On a `windows-2022` runner (or your **Tart** runner
if it hosts Windows — D6):
1. `py -3 build-v8.py win -archs x64` with `v8_enable_i18n_support=false`, `/MT`.
2. Configure Pulp's `core/view` V8 provider + build `pulp-threejs-native-demo`.
3. Run the hardened harness. **No `Intl` expected** (i18n off) — assert that's the
   only missing capability, everything else green.

**Exit criteria:** a Pulp-shaped binary links + renders on Windows with our V8. This
is the single biggest unknown about *product shape*; prove it before the hard lanes.

---

## Phase 1 — Linux x64, sealed, i18n-on (first new validated provider)
1. `python3 build-v8.py linux -archs x64`, ABI matched to the **real Skia Linux**
   build (read its resolved STL — likely **libstdc++**; do not assume libc++).
2. `seal/elf.py`: `ld -r` fold → `objcopy --keep-global-symbols=public.txt` (keep-list
   per D7). Audit with `readelf -sW`: zero flat `u_*`/`ubrk_*`/`ucnv_*`/`uloc_*`/zlib
   globals; complete `v8::`/`cppgc::`.
3. Download skia-builder Linux artifact (pin tag + verify SHA256 + provenance — §8).
4. Validation: forced-collision link (whole-archive Skia ICU/shaper/zlib) +
   identity-anchored harness. **Headless GPU:** Dawn is non-gating; record adapter.

**Exit criteria:** harness green on Linux with real Skia; symbol audit clean.

---

## Phase 2 — macOS, sealed, A/B vs Homebrew (then flip Pulp default)
1. `python3 build-v8.py mac -archs arm64` (then x86_64 + `universal`).
2. `seal/macho.py`: `ld -r -exported_symbols_list public.txt` (full mangled
   `v8::`/`v8::platform`/`cppgc::` set, generated — not `v8::*`). Audit `nm -gU`.
3. **A/B against Homebrew libnode** through Pulp's identical contract:
   ```bash
   # OUR sealed V8
   cmake -S . -B build-v8-ours -DPULP_JS_ENGINE=v8 -DPULP_ENABLE_GPU=ON -DPULP_BUILD_TESTS=ON \
     -DV8_INCLUDE_DIR=<unzip>/include -DV8_LIB_DIR=<unzip>/lib \
     -DV8_LIBRARY_PATH=<unzip>/lib/libv8_monolith.a
   cmake --build build-v8-ours --target pulp-test-js-engine pulp-threejs-native-demo -j8
   ctest --test-dir build-v8-ours -R "JsEngine" --output-on-failure
   # then the hardened capture + identity gate

   # HOMEBREW libnode (control, same commands, different provider)
   cmake -S . -B build-v8-brew -DPULP_JS_ENGINE=v8 -DPULP_ENABLE_GPU=ON -DPULP_BUILD_TESTS=ON \
     -DV8_INCLUDE_DIR=/opt/homebrew/opt/node/include/node -DV8_LIB_DIR=/opt/homebrew/opt/node/lib \
     -DV8_LIBRARY_PATH=/opt/homebrew/opt/node/lib/libnode.<N>.dylib
   ```
   Assert: both pass `pulp-test-js-engine`; both render the cube; **engine-identity
   reports OUR version for the `-ours` build** (not libnode's), proving the swap is real.

**Exit criteria:** ours == libnode on behavior, identity proves provenance → flip
Pulp default off libnode (D4).

---

## Phase 3 — Windows i18n-on (only after the spike resolves)
Per §6b, Windows-static-`.lib` + sealed-ICU + `Intl` is **unsolved**. Take whichever
the Phase-0 spike proved viable: **DLL export boundary** (recommended) or ICU symbol
renaming. If neither pans out, **ship Windows i18n-off (Phase 0b) as the supported
lane** and document the limitation — do not fake it.

---

## Phase 4 — CI bring-up + release
- `build-v8.yml`: exact-list platform parsing (not substring), `validate-all`
  aggregation job gates `create-release`, dynamic asset list, **pinned runner
  images**, cache depot_tools+V8 src.
- The Pulp-shaped + identity harness runs **on all three OSes** in CI; archive logs,
  symbol audits, exe SHA256, and the capture PNG every run.
- Release tag `mNNN-v8-<version>`; `manifest.json` carries the full provenance triangle
  + the **exact Skia tag validated against** + a pair lockfile (§9b).

---

## What might need extra Dawn/Skia setup (call out before Phase 1)
The threejs demo needs Skia Graphite + Dawn present and `PULP_ENABLE_GPU=ON`. Before
swapping V8, **first build `pulp-threejs-native-demo` with the default engine** to
confirm Pulp's Skia/Dawn fetch compiles on each OS — so a failure later is isolated to
the V8 swap, not GPU plumbing. On Linux/Windows, confirm Pulp can locate skia-builder
artifacts and that a native Dawn adapter exists (else GPU is recorded non-verified, per
the harness rules).

## Honest unknowns (so we don't ship false confidence)
- macOS/Linux sealing recipes are standard but **unproven in our context** until
  Phase 0/1 green.
- Windows i18n-on sealing is **unsolved** (§6b) — Phase 3 is conditional on a spike.
- Win/Linux **headless GPU** may force Dawn to software → harness records it
  "GPU-unverified"; that is honest, not a pass.
- Win/Linux **screenshot capture** path in Pulp may need adding (only mac
  `screenshot_mac.mm` confirmed) — scope in Phase 0b/1.
