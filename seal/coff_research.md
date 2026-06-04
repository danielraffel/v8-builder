# Windows / COFF symbol sealing — research notes

**TL;DR:** the flagship Windows artifact is a **DLL**, which has a real export table —
so sealing is natural (export only `v8::`/`cppgc::`, keep ICU/zlib internal), exactly
like the macOS/Linux shared libs. This sidesteps the genuinely-unsolved case.

## The unsolved case (why we avoid it)

A static **`.lib`** has *no* export boundary, and there is no clean COFF analogue to
`objcopy --keep-global-symbols`. `/WHOLEARCHIVE` does **not** seal — it can make
duplicate extraction worse. So *static `.lib` + sealed ICU + `Intl` on* is a research
spike, **not** a shipping plan (proposal §6b). We do not use it for the flagship.

## DLL export-table approach (flagship — Phase 3)

Options to validate when we get there:
- A module-definition file (`.def`) / `/EXPORT:` listing only the mangled
  `v8::`/`cppgc::` surface, so ICU/zlib are never exported from `v8.dll`.
- V8's component/shared build (`is_component_build=true`) — Chromium ships `v8.dll`
  in component builds; confirm its default export set and trim to our policy.
- Consumer links the generated import lib (`v8.dll.lib`), which only references the
  exported V8 symbols → no ICU collision with Skia at link time.
- Audit with `dumpbin /exports` (deny prefixes absent) and `dumpbin /symbols` on the
  consumer objects for `/MT` vs `/MD` agreement.

## Candidate that could simplify everything (unverified)

ICU symbol **renaming** (compile-time suffix via ICU's `U_DISABLE_RENAMING` /
version-suffix machinery) would make V8's ICU symbols not collide with Skia's bare
names at all — and it works on COFF. Needs verification that V8's GN/ICU build allows
a custom suffix. Spike in Phase 0.4 / Phase 3.

---

## ELF seal — duplicate-symbol fix candidates (Linux, found 2026-06-04 CI)

**Symptom:** `ninja v8_sealed_shared` on Linux → `ld.lld: error: duplicate symbol:
v8::internal::AllowCompilation::...` (many). The injected gn target links the monolith
TWICE: once via `deps = [":v8_monolith"]` (needed for the Rust-Temporal closure) and
again via `-Wl,--whole-archive <monolith>`. macOS `-force_load` + deps dedups on
Mach-O; **lld does NOT dedup under --whole-archive** → every symbol duplicated.

**Constraint:** the monolith must appear ONCE on the link line, yet all its objects
must be pulled (leaf .so has no undefined refs, so on-demand archive linking yields an
empty lib) AND the Rust closure must still resolve.

**Candidates to try (need a fast x64 Linux runner — each verify ≈ 2h on free GitHub):**
1. **`data_deps` + explicit whole-archive + explicit Rust closure.** `data_deps =
   [":v8_monolith"]` (build-order only, NOT link → no double) + ldflags
   `--whole-archive <monolith> --no-whole-archive` + add the Rust rlibs explicitly
   (glob `$root_build_dir/obj/third_party/rust/*/*/lib/*.rlib`,
   `$root_build_dir/obj/build/rust/allocator/*.rlib`, and
   `$root_build_dir/local_rustc_sysroot/lib/rustlib/x86_64-unknown-linux-gnu/lib/*.rlib`
   via an `exec_script` glob). Mirrors V8's own `v8_hello_world` link closure (proven
   on mac when I extracted it). LEAD candidate.
2. ~~**`complete_static_lib` wrapper**~~ — **RULED OUT (tested on mac 2026-06-04):**
   a `complete_static_lib` static_library depping `:v8_monolith` yields an EMPTY
   archive (`nm`: "no symbols", 16K dylib) — it does NOT unpack the nested
   `v8_monolith.a`, so it bundles nothing. Dead end.
3. **Last resort:** `-Wl,--allow-multiple-definition` — silences it but risks
   wrong-symbol-wins; NOT acceptable for a shipped artifact.

**Narrowed to candidate #1.** The remaining nuance is lld archive ordering: with
`deps=[:v8_monolith]` (Rust closure) + an explicit `--whole-archive <monolith>`, lld
duplicates because Rust/other objects' undefined refs pull deps-monolith members that
whole-archive already provided. Fix likely = whole-archive the monolith BEFORE the
deps libs, or dep only on the Rust targets (not the monolith) + whole-archive the
monolith. Both need an on-Linux gn iteration loop to verify. macOS (force_load+deps,
Mach-O dedup) is the proven analog. NOT pushed blind.


## Key constraint (confirmed on mac 2026-06-04): ELF fix is NOT iterable on macOS
- The duplicate-symbol failure is **Mach-O vs ELF specific**: ld64 *dedups* identical
  archive members (so codex's `deps=[:v8_monolith]` + `-force_load` works on mac);
  lld does **not** dedup under `--whole-archive` (so the same recipe duplicates on ELF).
  => I cannot reproduce — let alone validate — the ELF fix on this Mac.
- Tested swapping `deps=[:v8_monolith]` → `deps=[//:v8_maybe_temporal]` (Rust closure
  only, monolith via force_load): got the full ~56-rlib Rust closure on the line, but
  **undefined symbols** — proving `deps=[:v8_monolith]` is REQUIRED for the complete
  C++ closure (it brings non-Rust deps the monolith .a references). So candidate #1
  must KEEP `deps=[:v8_monolith]` and fix the double-link some other way.
- Net: the ELF seal fix genuinely needs an **on-Linux gn/lld iteration loop**. Lead
  approach to try there: keep `deps=[:v8_monolith]`; make the monolith's objects pulled
  WITHOUT a second file reference (e.g. `-Wl,--whole-archive` ordered before deps libs,
  or `-Wl,--start-lib/--end-lib`, or lld `-z` flags) — verify on Linux.
