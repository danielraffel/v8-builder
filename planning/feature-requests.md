# v8-builder — Feature Requests (future, post desktop-lanes + m150)

Investigations + ideas captured 2026-06-04. **Not** for now — sequence after
mac/win/linux V8 integration lands + PR merges, and after the Skia m150 migration.
These are filed as GitHub issues on the relevant repo(s).

---

## FR1 — Source a Chromium-co-tested Skia/V8/Dawn set from LKGR DEPS

**Idea:** instead of independently building a Skia branch (`chrome/mNNN`) + a V8 tag
(today's "validated pair"), pin all three to a single **Chromium LKGR DEPS** revision
— the set Google's waterfall actually tested together. Closest thing to a truly
co-tested pair, and could even remove the *need* to seal Abseil (one shared absl).

**Proven feasible (2026-06-04):** `tools/lkgr_pin.py` fetches live Chromium LKGR DEPS
and extracts the exact SHAs — no full Chromium checkout needed:
```
skia: 4fdb859c8da740f1c1d94183637ff0c175329bc2   (skia.googlesource.com/skia.git)
v8:   535361f082f89df6b3ceecea73b3ba7503bc83ef   (chromium.googlesource.com/v8/v8.git)
dawn: 075626b4b324101b06cd309c4140672588a5adf2   (dawn.googlesource.com/dawn.git)
```
(`lkgr` is a Gitiles ref alias, not a repo dir — it doesn't appear in the GitHub
mirror tree; fetch it from googlesource Gitiles as the tool does.)

**Where Dawn comes from:** `dawn.googlesource.com/dawn` (Chromium DEPS `dawn_revision`).
skia-builder bundles Dawn into `libdawn_combined.a`; v8-builder doesn't pull Dawn today.

**Proposed shape (sweet spot — lockfile, not a full Chromium checkout):**
- `v8-builder` generates an `lkgr-lock.json` at build time (the tool above) and records
  it in the release manifest. Pulp reads the lock to align Skia/V8/Dawn.
- Tag/release alignment: have skia-builder fork + v8-builder both able to build from
  the LKGR-pinned SHA so their releases name the same co-tested set.

**Hard caveat (must prove before claiming co-tested):** the DEPS SHAs are the
*revisions Chromium expects together*; a standalone build still needs Chromium's GN
args, compiler, sysroot, libc++ assumptions, patches, and generated headers. So "build
both from these SHAs and prove Pulp's ABI/build model consumes them cleanly" is the
real next step — not just reading the SHAs. **Acceptance: we already prove SHA
generation (done); proving a clean co-built bundle is the open work.**

**Shared release-manifest contract (both repos return similar data).** For Pulp to
pair them, **skia-builder and v8-builder releases must each emit the same manifest
fields** describing what they were built from, e.g.:
```json
{ "source": "chromium-lkgr-deps", "chromium_deps_blob": "<sha>",
  "skia": "<sha>", "v8": "<sha>", "dawn": "<sha>",
  "this_artifact": "v8|skia", "built_revision": "<the sha THIS release built>" }
```
Then Pulp reads either release's manifest and confirms they reference the **same
LKGR set** (same skia/v8/dawn SHAs) before pairing — a machine-checkable co-tested
guarantee, not a naming convention. (This is the cross-repo "shared contract" from
proposal §3, extended to carry the LKGR triple.)

**Dependency it creates:** keeping skia-builder fork + v8-builder regenerating
regularly and in sync against LKGR — which motivates FR2.

---

## FR2 — Auto-rebuild cadence (v8-builder + skia-builder fork)

**Idea:** auto-trigger rebuilds on a cadence so the published libs track upstream:
- trigger on upstream source updates (V8 / Skia / Dawn / Chromium LKGR roll), or
- a scheduled cadence (e.g. weekly), or
- explicitly off a new Chromium LKGR DEPS roll (ties to FR1).

**Per-repo (each is unique — file on both):**
- **v8-builder:** rebuild + reseal + revalidate the sealed shared V8 when its pinned
  V8 (or the LKGR `v8_revision`) advances. Gate release on the identity-anchored
  validator passing.
- **skia-builder fork:** rebuild Skia+Dawn when Skia branch / LKGR `skia_revision` /
  `dawn_revision` advances; keep the fork's extra slices (mac-x86_64, iOS, visionOS).

**Open questions to investigate:** what's the right trigger (GitHub
`repository_dispatch` from a tiny LKGR-watcher cron? `schedule:`?); how to avoid
churn (only rebuild when a SHA actually changed); how releases get tagged so Pulp can
pin "latest co-tested LKGR set". Align with FR1's lockfile.

**Verdict:** possibly more trouble than it's worth; investigate + decide later. The
LKGR-lock generation (FR1) is the cheap, high-value piece already proven.
