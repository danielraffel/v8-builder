# v8-builder

Build & **seal** a standalone, embeddable [V8](https://v8.dev/) for use *alongside*
[Skia](https://skia.org/) Graphite + [Dawn](https://dawn.googlesource.com/dawn)
(WebGPU) in a single binary — on macOS, Linux, and Windows.

The flagship artifact is a **shared library** (`.dylib`/`.so`/`.dll`) with **`Intl`
on**, whose export table exposes only the `v8::`/`cppgc::` embedder API and keeps
ICU/zlib **internal** — the same property that lets Homebrew Node's `libnode` coexist
with Skia today. This avoids the duplicate-ICU-symbol clash a naïve `v8_monolith`
hits when linked next to Skia.

> **Status: scaffolding (Phase 0).** Nothing builds V8 yet. The scripts here are
> skeletons; build logic lands phase by phase per the runbook. See the planning docs.

## Why this exists

See **`planning/v8-builder-proposal.md`** (the rationale, the ICU/ABI crux, and
decisions D1–D9) and **`planning/v8-builder-runbook.md`** (the gated execution plan +
live progress tracker). It is modeled on [skia-builder](https://github.com/olilarkin/skia-builder)
so Skia and V8 artifacts share conventions.

## Layout

```
build-v8.py            # build orchestrator (CLI), mirrors build-skia.py  [skeleton]
build-win.sh           # Windows helper wrapper                           [skeleton]
seal/                  # the "make ICU/zlib invisible" policy + backends
  policy.json          #   public-symbol policy + ICU/zlib deny prefixes
  macho.py elf.py      #   per-platform export-list generators/auditors   [skeleton]
  coff_research.md     #   Windows static-.lib sealing notes (not the flagship path)
validate/              # cross-link proof: V8 + Skia/Dawn in one binary
  CMakeLists.txt
  identity_main.cpp    #   asserts ENGINE identity, not pixels
  run_validation.cmake #   strict, no-skip-pass gate
.github/workflows/     # build-v8.yml + validate-v8.yml                    [skeleton]
planning/              # proposal + runbook (authoritative)
```

## Consuming from Pulp

Through Pulp's existing provider contract (`core/view/CMakeLists.txt`):
`-DPULP_JS_ENGINE=v8 -DV8_INCLUDE_DIR=… -DV8_LIB_DIR=… -DV8_LIBRARY_PATH=…`.

**On Skia/V8 "matching":** there is no upstream-blessed Skia+V8 version pair here.
V8 and Skia/Dawn don't share C++ types — they talk via serialized `choc::value`, and
each bundles its **own sealed** ICU/zlib/Abseil. So coexistence depends on ABI compat
at the link boundary (libc++/STL, RTTI, **pointer compression**) + the symbol seal —
**not** on version matching. (Empirically: V8 15.1 coexists fine with Skia `chrome/m149`,
which are *different* Chromium revisions.) Releases therefore record a **validated
pair** — the exact V8 build + the exact Skia release it was tested against (SHA-pinned
in the manifest) — which is a *proof of coexistence*, not a claim of an upstream match.
The only way to get a truly co-tested pair would be to build both Skia and V8 from the
**same Chromium DEPS revision** (a possible future option; not what we do today).

## License

MIT (see `LICENSE`). V8/ICU/zlib carry their own licenses; per-release SBOM planned.
