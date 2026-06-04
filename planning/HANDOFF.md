# v8-builder — handoff & remaining-work checklist (2026-06-04)

**This is the single doc to finish the project from.** Work the checklist top-to-bottom.
Companion detail lives in `planning/v8-builder-runbook.md` (CURRENT STATE section +
tracker), `planning/v8-builder-proposal.md` (the why / decisions), `seal/coff_research.md`
(seal mechanics), `planning/feature-requests.md` (FR1/FR2). Memory `linux-vm-build-loop`
records how to stand up a local Linux VM.

Settled (do NOT relitigate): **D2 i18n ON · D5 ship V8 as a SHARED lib (.dylib/.so/.dll)
flagship · D7 general-audience seal (full v8::/cppgc:: ABI; ICU/zlib/Abseil internal).**

Honesty bar (carry forward): the validator asserts engine + GPU-backend **identity**, never
pixels; a skip is never a pass; if something is unproven, say so.

---

## Where we are (honest)

Desktop matrix — **2 of 6 cells validated**:

| | macOS | Linux | Windows |
|---|---|---|---|
| **Intel (x86_64/x64)** | ✅ DONE | 🔧 fix staged, validating | ⚪ lane not implemented |
| **ARM (arm64)** | ✅ DONE | ⚪ open | ⚪ open |

- **macOS arm64 + x86_64: DONE** — sealed shared V8 15.1 coexists with Skia Graphite + Dawn;
  identity gate PASS (arm64 full Pulp threejs demo on real Metal; x86_64 standalone validator
  under Rosetta). universal = lipo. Don't redo.
- **Linux x64: root cause found + fix staged (not yet confirmed).** See next section.
- **Linux arm64, Windows x64, Windows arm64: open.** Windows `SystemExit`s in build-v8.py.

## The Linux seal fix — staged on branch `linux-seal-fix` (CONFIRM, then merge)

**Root cause (definitive, from reading `build/toolchain/gcc_toolchain.gni` `tool("solink")`):**
on Linux the solink rule's rspfile is `-Wl,--whole-archive {{inputs}} ... -Wl,--no-whole-archive`
— it **already whole-archives `{{inputs}}`**. Since `deps=[:v8_monolith]` puts
`libv8_monolith.a` into `{{inputs}}`, it's whole-archived ONCE by the rule (Rust closure
appended as `{{rlibs}}`). The injected seal target's **hand-rolled** `-Wl,--whole-archive
obj/libv8_monolith.a` whole-archived the *same archive a second time* → lld included every
member twice (it doesn't dedup) → the `ld.lld: duplicate symbol v8::internal::AllowCompilation::…`
failure in CI run **26961155381**. It was NOT an ordering problem (the earlier Temporal-OFF
synthetic only whole-archived once, so it never showed this).

**Fix (already committed on branch `linux-seal-fix`):** in `build-v8.py` `SEAL_TARGET_GN`,
the ELF (`else`) branch keeps only `--version-script` + `-soname` and **drops** the three
`--whole-archive`/monolith-path/`--no-whole-archive` ldflags. `deps` + the rule do the
whole-archiving. Mac branch (`-force_load`, ld64 dedups) unchanged.

**Status:** CI run **26965278162** (ref `linux-seal-fix`, V8 15.1.27, Skia chrome/m150) was
dispatched to validate it. **First step in the next session:** check that run
(`gh run view 26965278162 --repo danielraffel/v8-builder`). If green → `git checkout main &&
git merge linux-seal-fix && git push`. If it fails differently → iterate the seal link on a
**local x86_64 Linux VM** (fast loop) or CI.

---

## Outstanding work — ordered checklist (task IDs in brackets)

1. **[#24] Land Linux x64.** Confirm the `linux-seal-fix` fix (CI 26965278162 / local x86_64 VM),
   validate the sealed `.so` coexists with Skia ICU via the identity-anchored validator (no
   skip-pass), merge to main. → Linux x64 DONE.
2. **[#26] Linux arm64.** Add a matrix row; build by cross-compile from x86_64 (`target_cpu="arm64"`
   + arm64 sysroot) or natively on a Tart arm64 Linux VM; validate on an arm64 runner/VM.
3. **[#27] Windows x64 DLL-export seal lane.** Implement the `win` path in `build-v8.py` (it
   currently `SystemExit`s): `.def`/`/EXPORT:` (or component build) exporting only v8::/cppgc::,
   ICU/zlib/absl internal; audit with `dumpbin /exports`. Iterate on a Tart Windows VM.
4. **[#28] Windows x64 validate, then Windows arm64.** Coexistence + identity on a Tart Windows
   VM and GitHub windows-2022; then the arm64 Windows lane (cross or native Tart VM).
5. **[#29] Intel/ARM sweep.** All 6 desktop cells build + validate; enforce intra-repo single
   V8-SHA alignment (no mixed-revision release; `validate-all` gate asserts agreement).
6. **[#30] macOS A/B vs Homebrew libnode** through Pulp's contract (both pass `pulp-test-js-engine`
   + render the cube; identity reports OUR version for the -ours build) → flip Pulp default off
   libnode (D4).
7. **[#20] Skia m150 migration of Pulp** — AFTER V8 integration lands + PRs merge. Bump m149→m150,
   rebuild Pulp, fix breakage (watch `SkRegion::setRects`→SkSpan, SkStrikeRef in TextShaper), push.
8. **[#31] Pairs / LKGR (FR1).** Generate `lkgr-lock.json` at build time + carry it in the release
   manifest; skia-builder & v8-builder emit the SAME manifest fields so Pulp pairs by matching the
   LKGR triple; assert cross-repo AND intra-repo alignment. (FR2 = auto-rebuild cadence — decide later.)
9. **[#32] iOS (jitless).** After desktop + m150. `v8.dev/docs/cross-compile-ios`; NO JIT for release;
   target = threejs working (JSC already works there, so this isn't critical-path).
10. **[#33] Android (NDK).** After iOS.

Keep `planning/v8-builder-runbook.md` tracker + the task list in 1:1 sync as items change.

---

## Hard toolchain constraint (drives the VM/emulation strategy)

V8 15.1's bundled **clang (llvmorg-23) + Rust are x86_64-Linux-host ONLY**
(`tools/clang/scripts/update.py`: `'linux'→'Linux_x64'`). So:
- **Intel** Linux/Windows build natively with the bundled toolchain on x86_64 hosts.
- **ARM** Linux/Windows: cross-compile from an x86_64 host, OR run the bundled toolchain under
  **x86_64 emulation** (an emulated Intel VM runs bundled clang/rust natively-in-emulation).
- A *native* arm64-Linux host can't run the bundled toolchain (qemu-user segfaults on it; system
  clang-18 has clang-23 flag skew → only good for toolchain-agnostic seal-link checks).
- The seal itself is **arch-independent** (symbol-level), proven on both mac arches.

→ **On the Mac Studio: prefer Tart with local Windows + Linux VMs, and use x86_64 emulation if
available** so a faithful Intel build (and the seal-link loop) runs locally without GitHub CI
round-trips. An emulated x86_64 Linux VM reproduces CI run 26961155381 exactly.

## Environment / context
- Repo public: github.com/danielraffel/v8-builder. `main` clean; fix on branch `linux-seal-fix`.
- Pulp: `/Users/danielraffel/Code/pulp`; identity-gate worktree at
  `/Users/danielraffel/Code/pulp-v8builder-identity` (branch `v8builder-p0-identity-gate`, local only).
- skia-builder releases: `chrome/m150` (latest) + `chrome/m149`, each with `*-linux-x64-*`,
  `*-win-x64-*`, `*-mac-*` artifacts the validator links against. V8 `15.1.27` == the LKGR-pinned
  v8 SHA in `planning/lkgr-lock.json`.
- `build-v8.yml` is the real pipeline (depot_tools→sync→gn→ninja→seal→audit→validate); the harness
  is sound (CI got to 2390/2390 build steps, failed only at the seal link the fix addresses).
- Operating mode: consult Codex on hard calls and pick the right/best solution rather than asking
  for routine technical decisions; **stop for explicit go before outward-facing/irreversible steps**
  (pushing, public CI). CI runs are pre-authorized for this work.

---

## Kickoff prompt (paste into the new Mac Studio session)

> Continue and finish the `v8-builder` project (repo at this path; public on GitHub). Read
> `planning/HANDOFF.md` first, then the runbook CURRENT STATE section. **Finish building
> everything in HANDOFF.md's checklist. Use the local Tart VMs for Windows and Linux, and aim
> to support both ARM and Intel — use x86_64 emulation mode if we can now**, so faithful Intel
> builds and the seal-link loop run locally instead of via GitHub CI.
>
> Start at checklist item #1: a fix for the Linux ELF seal duplicate-symbol bug is already
> committed on branch `linux-seal-fix` (root cause = double whole-archive; the solink rule
> already whole-archives {{inputs}}, so the hand-rolled --whole-archive was a second copy).
> Check CI run 26965278162; if green, merge to main; otherwise validate/iterate on a local
> x86_64 Linux VM. Then work down the checklist: Linux arm64 → Windows x64 DLL lane → Windows
> arm64 → Intel/ARM sweep → mac A/B → (after V8 lands+merges) Skia m150 → LKGR pairs → iOS →
> Android. Keep the runbook tracker + task list in sync; identity-anchored validation only (no
> skip-pass); consult Codex on hard calls; stop for explicit go before outward-facing steps.
> Don't relitigate D2 (i18n ON) / D5 (shared-lib flagship) / D7 (general-audience seal).
