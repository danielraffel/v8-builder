# v8-builder — Build & Validate Runbook

**Status:** Executable plan. Ask me *"build Phase N from the runbook"* and I'll do it.
**Companion to:** `planning/v8-builder-proposal.md` (read that for the *why*).
**Date:** 2026-06-03

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
| P0.3a | Positive control: harness PASSES against libnode | TODO | |
| P0.3b | Negative control A: unsealed V8 → link FAILS (dup ICU) | TODO | |
| P0.3c | Negative control B: substituted engine → FAILS identity | TODO | |
| P0.4 | Settle static-vs-shared spike (D5) + single-lib (§9) | DONE | shared, i18n-on (settled 2026-06-03) |
| P0b.1 | Windows shared-lib slice: build + Pulp link + harness | TODO | cheapest-first proof of product shape |
| P1.1 | Linux x64 shared lib, sealed, i18n-on | TODO | match real Skia STL (libstdc++) |
| P1.2 | Linux validation: forced-collision + identity harness | TODO | |
| P2.1 | macOS shared lib (arm64, x86_64, universal), sealed | TODO | |
| P2.2 | macOS A/B: ours vs Homebrew libnode through Pulp | TODO | identity proves the swap is real |
| P2.3 | Flip Pulp default off libnode (D4) | TODO | only after A/B green |
| P3.1 | Windows i18n-on DLL (export-table seal) | TODO | |
| P4.1 | CI: build-v8.yml + validate-v8.yml, all-3-OS harness | TODO | pinned runners, validate-all gate |
| P4.2 | Release: mNNN-v8-<ver> tag + manifest + pair lockfile | TODO | |
| PUB | Create public GitHub repo + push | BLOCKED | needs explicit user go |

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
