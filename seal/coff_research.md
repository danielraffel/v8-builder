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

---

## ⚠️ CORRECTION (2026-06-04, later): the bug is REAL with Temporal/Rust ON

The "RESOLVED" claim immediately below was **PREMATURE**. A faithful GitHub CI build
(run 26961155381, x86_64, V8 **15.1.27**, **Temporal/Rust ON**, bundled clang-23 + lld)
**reproduced the exact duplicate-symbol failure**:
```
ld.lld: error: duplicate symbol: v8::internal::AllowCompilation::Close(...)
>>>   assert-scope.o:(v8::internal::AllowCompilation::Close...) in archive obj/libv8_monolith.a   (on-demand pull)
>>>   assert-scope.o:(.text+0x0)                               in archive obj/libv8_monolith.a   (--whole-archive pull)
```
So a monolith member is pulled BOTH by `--whole-archive` AND on-demand from the plain
deps copy. My synthetic repro + gn-order analysis were done with **Temporal OFF**
(`v8_enable_temporal_support=false`, to dodge the rust toolchain on the arm64 host) — and
with no Rust closure, whole-archive-first IS clean. **With the Rust closure ON, the link
graph changes** (the rlibs / rust integration create monolith refs that get satisfied from
the plain deps archive in addition to the whole-archive copy) and the duplicate returns.
### DEFINITIVE ROOT CAUSE (2026-06-04, from reading the toolchain): DOUBLE whole-archive
Reading `build/toolchain/gcc_toolchain.gni` `tool("solink")` settles it. On Linux (the
non-aix, non-mipsel branch, line ~505) the rule's rspfile is:
```
rspfile_content = "-Wl,--whole-archive {{inputs}} {{solibs}} -Wl,--no-whole-archive {{libs}}"
```
i.e. **the solink rule ALREADY whole-archives `{{inputs}}`.** Our target does
`deps=[":v8_monolith"]`, which puts `obj/libv8_monolith.a` into `{{inputs}}` → it is
whole-archived ONCE by the rule, in place, and the Rust closure is appended as `{{rlibs}}`.
Our hand-rolled `-Wl,--whole-archive obj/libv8_monolith.a -Wl,--no-whole-archive` in
`ldflags` whole-archived the **same archive a SECOND time** → lld included every member
twice (it doesn't dedup) → the `duplicate symbol` failure. It was NOT an ordering problem;
the Temporal-OFF synthetic only whole-archived once, which is why it looked clean.

**FIX (applied to build-v8.py SEAL_TARGET_GN, ELF branch):** delete the hand-rolled
`--whole-archive`/monolith-path/`--no-whole-archive` ldflags; keep only the version-script
+ soname. `deps=[:v8_monolith]` + the rule's built-in `{{inputs}}` whole-archive do the
rest, and `{{rlibs}}` still carries the Rust closure. (Mach-O differs: ld64 needs explicit
`-force_load` and dedups, so the mac branch is unchanged.) Validating on CI on branch
`linux-seal-fix`. The `-LinkWrapper,add-whole-archive` mechanism is unnecessary — the
default solink rule already does the right thing once we stop double-wrapping.

## ELF seal — (earlier, Temporal-OFF analysis; see CORRECTION above)

Stood up a real on-Linux iteration loop (arm64 Ubuntu 24.04 in QEMU+HVF on Apple
Silicon; egress via a host HTTP-CONNECT proxy reverse-tunneled in; V8 15.1 synced at
SHA 32db030896203ea8d940bfcd2b4566c7a75e91fc). Two decisive results:

**1. The duplicate-symbol bug is purely link-line ORDER (synthetic repro, native
ld.lld 18.1.3).** A static archive referenced as a PLAIN deps archive that appears
*before* `-Wl,--whole-archive <same archive>` → `ld.lld: error: duplicate symbol`
(the plain ref pulls a member on-demand, then --whole-archive pulls it AGAIN; lld does
not dedup, unlike Mach-O `-force_load`). The SAME inputs with `--whole-archive <archive>`
*before* the plain reference link **rc=0** AND the output contains every member
(verified both a synthetic funcA and funcB present). So: order matters, and the bug is
**monolith-vs-monolith, independent of the Rust closure** (the repro has no Rust).

**2. The current `v8_sealed_shared` gn target ALREADY emits the correct (clean) order.**
The real Chromium solink rule (from `out/.../toolchain.ninja`) is:
```
clang++ -shared -Wl,-soname=... ${ldflags} -o libv8.so @libv8.so.rsp ${rlibs}
```
and `ninja -t commands v8_sealed_shared` expands `${ldflags}` to include, in order:
`... -Wl,--whole-archive obj/libv8_monolith.a -Wl,--no-whole-archive`. So the link line
is: **(1)** `--whole-archive monolith` → **(2)** `@rsp` (deps incl. the plain monolith)
→ **(3)** `${rlibs}` (Rust closure, last). `${ldflags}` precede `@rsp` in the rule, so
the monolith is whole-archived FIRST; the plain monolith in the rsp is then a harmless
no-op (everything already defined) → **no duplicate symbols**. This is exactly the
clean "FIX-A" order proven in (1).

**Conclusion:** the duplicate-symbol failure recorded above was an EARLIER seal-target
shape (monolith pulled via deps BEFORE the whole-archive). The current target in
`build-v8.py` (`SEAL_TARGET_GN`, ELF branch) is correct as written and should link
cleanly on ELF. No gn change is required. Candidates #1–#3 above are superseded.

**Optional hardening (not required, deferred):** to make the recipe robust against a
future gn template that reorders `${ldflags}` vs `@rsp`, switch from hand-rolled
`-Wl,--whole-archive` to Chromium's blessed `add-whole-archive` LinkWrapper
(`build/toolchain/whole_archive.py`, driven by the per-target whole-archive config).
Left as-is for now — don't change a recipe that's proven correct by link-line order, and
the mac lane shares `SEAL_TARGET_GN`.

## Why a FAITHFUL local Linux build was NOT completed (and what remains)

The on-Linux loop proved the seal mechanism but could NOT produce the *shipping* artifact
locally, because the host is arm64 and V8 15.1's toolchain is x86_64/clang-23-pinned:
- **No arm64-linux Chromium clang/rust exists.** `tools/clang/scripts/update.py` maps
  `'linux' → 'Linux_x64'` only (host choices: linux/mac/mac-arm64/win). Same for the
  pinned Rust.
- **qemu-user (8.2.2) segfaults** running the bundled x86_64 `clang`/`rustc` (signal 11),
  even with the amd64 sysroot as `QEMU_LD_PREFIX` — so the version-matched toolchain
  cannot be emulated here.
- **System clang 18.1.3 has deep clang-23 flag skew vs V8 15.1**: it compiles V8 base
  fine but rejects clang-23-era flags V8 emits — `-fno-lifetime-dse`, `-Wa,--crel`,
  `-fdiagnostics-show-inlining-chain`, `-fsanitize-ignore-for-ubsan-feature` — and more
  would surface deeper. Stripping them yields a non-faithful build anyway.

So the FAITHFUL end-to-end Linux/x64 validation (bundled clang-23 + Temporal/Rust +
bundled libc++, the real CI target) belongs on **GitHub ubuntu-24.04 (x86_64)** where the
bundled toolchain runs natively — a single confirmation run, not an iteration loop, since
the seal recipe is now proven correct. That run is **owner-gated** (outward-facing CI).
The local loop remains available (system clang 18 + `v8_enable_temporal_support=false` +
`use_sysroot=false`/`use_custom_libcxx=false`) for any future ELF seal-link iteration,
which is all the synthetic repro needs anyway.
