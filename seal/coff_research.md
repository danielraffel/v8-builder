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
