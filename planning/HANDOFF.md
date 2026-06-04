# v8-builder — session handoff (2026-06-04)

Paste the **"Kickoff prompt"** below into a fresh session on the Mac Studio. Everything
needed is in this repo; read the pointers first.

## Read first (source of truth)
- `planning/v8-builder-runbook.md` → **"CURRENT STATE & REMAINING OPENS"** section at top
  (6-cell Intel/ARM matrix, prioritized opens) + the Progress tracker table.
- `seal/coff_research.md` → top **"CORRECTION (2026-06-04)"** (the live ELF-seal bug).
- `planning/feature-requests.md` → FR1 (LKGR pairs + shared manifest), FR2 (auto-rebuild).
- Memory `linux-vm-build-loop` → how the laptop's QEMU+HVF Linux loop worked (fallback).

## Where we are (honest)
- **macOS arm64 + x86_64: DONE** — sealed shared V8 15.1 coexists with Skia Graphite + Dawn,
  identity-anchored gate PASS (arm64 full Pulp demo; x86_64 validator under Rosetta). universal = lipo.
- **Linux x64: seal link FAILS — #1 open.** Faithful GitHub CI run `26961155381` (x86_64,
  V8 **15.1.27** = the LKGR-pinned SHA, Temporal/Rust ON) built everything (2390 steps) then
  failed at the seal link: `ld.lld: duplicate symbol v8::internal::AllowCompilation::...`
  (monolith `assert-scope.o` pulled twice). The pipeline + harness are sound; only the seal
  target is wrong. A Temporal-OFF synthetic repro looked clean but did NOT generalize — the
  Rust closure changes the link graph.
- **Linux arm64, Windows x64, Windows arm64: open / not yet implemented** (Windows lane
  `SystemExit`s in build-v8.py).
- Decisions **D2 i18n ON / D5 shared-lib flagship / D7 general-audience seal** are SETTLED — do not relitigate.

## The #1 fix (Linux ELF seal)
The `v8_sealed_shared` ELF target references the monolith twice: the `deps=[:v8_monolith]`
plain copy AND a hand-rolled `-Wl,--whole-archive obj/libv8_monolith.a`. With the Rust/Temporal
closure ON, lld pulls a member from both → duplicate symbols (ld64 dedups on mac; lld doesn't).
**Lead fix:** reference the monolith **exactly once**, whole-archived, while still pulling the
Rust closure — use Chromium's blessed `-LinkWrapper,add-whole-archive` (`build/toolchain/whole_archive.py`)
instead of hand-rolled ldflags. Iterate fast on a **Tart x86_64 Linux VM** (runs the bundled
clang-23 + rust natively-in-emulation) or GitHub CI (~1h), then re-dispatch build-v8.yml.

## Hard toolchain constraint (drives the VM strategy)
V8 15.1's bundled clang (llvmorg-23) + Rust are **x86_64-Linux-host ONLY**. So: Intel cells build
natively; ARM cells need cross-compile from x86_64 OR x86_64 emulation. A *native* arm64-Linux host
can't run the bundled toolchain (qemu-user segfaults on it; system clang-18 has clang-23 flag skew —
only good for toolchain-agnostic seal-link checks). The seal itself is arch-independent (proven on mac).

## Prioritized opens (tasks #24,#26–#33)
1. Fix Linux ELF seal (#24) → re-run build-v8.yml → Linux x64 green.
2. Linux arm64 (#26): matrix row, cross/emulated build, validate on arm64 runner/VM.
3. Windows x64 DLL-export seal lane in build-v8.py (#27), then Windows arm64 (#28).
4. Intel/ARM sweep + single-SHA intra-repo alignment (#29).
5. macOS A/B vs libnode → flip Pulp default (#30).
6. **SKIA-M150**: migrate Pulp m149→m150 AFTER V8 integration lands + PRs merge (#20).
7. Pairs/LKGR FR1 (#31).
8. iOS jitless / threejs (#32), then Android NDK (#33).

## Context
- Repo is public: github.com/danielraffel/v8-builder (clean main, fully pushed).
- Pulp lives at `/Users/danielraffel/Code/pulp`; an identity-gate worktree exists at
  `/Users/danielraffel/Code/pulp-v8builder-identity` (branch `v8builder-p0-identity-gate`, local only).
- skia-builder releases: `chrome/m150` (latest) + `chrome/m149`, each with `*-linux-x64-*`,
  `*-win-x64-*`, `*-mac-*` artifacts the validator links against.
- Operating mode: **consult Codex on hard calls and pick the right/best solution rather than
  asking** the owner for routine technical decisions; but **stop for explicit go before
  outward-facing/irreversible steps** (pushing, public CI). Owner already authorized CI.
- Laptop QEMU loop was torn down (scratch clone removed; original Tart disk intact).

---

## Kickoff prompt (paste into the new Mac Studio session)

> Continue the `v8-builder` project (repo at this path; public on GitHub). Start by reading
> `planning/HANDOFF.md`, then `planning/v8-builder-runbook.md` (CURRENT STATE section) and
> `seal/coff_research.md` (CORRECTION at top). We're on the Mac Studio now — **use Tart with
> local VMs (Windows + Linux, both arches) and x86_64 emulation if available**, so we can build
> Intel and iterate the faithful toolchain locally instead of round-tripping GitHub CI.
>
> **#1 task: fix the Linux ELF seal duplicate-symbol bug** that CI run 26961155381 reproduced —
> the `v8_sealed_shared` target pulls the monolith twice (deps + hand-rolled `--whole-archive`);
> with the Rust/Temporal closure ON, lld duplicates. Fix it so the monolith is referenced exactly
> once while still pulling the Rust closure (lead: Chromium's `-LinkWrapper,add-whole-archive`),
> validate the sealed `.so` coexists with Skia (identity gate, no skip-pass) on a Tart x86_64
> Linux VM, then re-run build-v8.yml to land linux/x64 green.
>
> Then work the prioritized opens in `HANDOFF.md`: Linux arm64 → Windows x64 DLL lane → Windows
> arm64 → Intel/ARM sweep → mac A/B → (after V8 lands+merges) Skia m150 migration → LKGR pairs →
> iOS → Android. Keep the runbook tracker + task list in sync. Consult Codex on hard calls and
> pick the best solution; stop for explicit go before any outward-facing step. Don't relitigate
> D2 (i18n ON) / D5 (shared-lib flagship) / D7 (general-audience seal).
