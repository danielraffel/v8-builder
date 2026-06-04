# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Builds and **seals** a standalone V8 for embedding next to Skia Graphite + Dawn.
Flagship = **shared library, `Intl` on**, export-table-sealed (ICU/zlib internal).
Modeled on `skia-builder` (depot_tools + gn + ninja).

## Authoritative docs — read before doing anything

- `planning/v8-builder-runbook.md` — the gated execution plan and the **Progress
  tracker** table (single source of truth; keep in sync with the task list).
- `planning/v8-builder-proposal.md` — rationale, ICU/ABI crux, decisions D1–D9,
  §6b per-platform solution status.

## Settled decisions (do not relitigate without owner input)

- **D2 i18n ON** — public artifact; embedders expect `Intl`.
- **D5 SHARED library** flagship — clean export-table ICU seal on all 3 OSes
  (incl. Windows DLL); matches how Pulp already links `libnode.dylib`.
- **D7 general audience** — seal to the full V8 public ABI, not a Pulp-narrowed set.

## Hard rules

- **Phase 0 first**, in order. Do **not** trust a green validation until the negative
  controls (unsealed link, substituted engine) actually FAIL as specified.
- **Local only.** Do **not** create or push the public GitHub repo until the owner
  explicitly says go. Stop and ask before any outward-facing / irreversible step.
- **No hallucinated passes.** The validation harness asserts engine + GPU-backend
  **identity**, never pixels alone. A skip is not a pass. If unproven, say so.

## Build (once implemented — currently skeletons)

```bash
python3 build-v8.py mac   -archs arm64        # → shared libv8 (.dylib), Intl on, sealed
python3 build-v8.py linux -archs x64
py -3   build-v8.py win   -archs x64          # DLL with export-table seal
```

## Validate

`validate/` links the built V8 next to a real Skia release artifact, forces the
ICU/zlib collision paths, and runs the identity-anchored harness (built on Pulp's
`examples/threejs-native-demo --demo cube --capture`).
