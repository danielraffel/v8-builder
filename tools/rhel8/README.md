# RHEL/Rocky-8 glibc-floor build (task #24b)

Builds the Linux V8 lane inside a **Rocky Linux 8** container (glibc 2.28) so the
sealed `libv8.so` is loadable on older enterprise Linux, not just on the
ubuntu-24.04 build host (glibc ~2.39).

## Why

`build-v8.py`'s `linux_gn_args` deliberately uses `use_sysroot=false` +
`use_custom_libcxx=false` so the artifact links the **platform** C++ ABI (system
`libstdc++`) — that's what makes the `.so` drop-in (Node ships `libnode` the same
way; the bundled Chromium `__Cr` libc++ is not consumable). The price is that the
sealed `.so` inherits the **build host's glibc symbol-version floor**:

- Built on ubuntu-24.04 → floor ~`GLIBC_2.39` → won't `dlopen` on RHEL/Rocky/Alma 8,
  Amazon Linux 2, SLES 15, Debian 10, Ubuntu 18.04.
- Built on Rocky 8 → floor `≤ GLIBC_2.28` → loads on any glibc ≥ 2.28 host.

The floor is set by the glibc dev headers / crt objects (`/usr/include`,
`/usr/lib64`) the bundled clang resolves against, **not** by the compiler. V8 still
uses its own bundled clang; `gcc-toolset-12` only supplies a C++20-capable
`libstdc++` (its `GLIBCXX_*` symbols are independent of the glibc floor).

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | `rockylinux:8` + `gcc-toolset-12` + depot_tools/gn/ninja deps |
| `build-in-docker.sh` | build + seal + floor-gate on an x86_64 Docker host |
| `check_glibc_floor.py` | parse `objdump -T`, assert floor ≤ max (default 2.28) |
| `../../.github/workflows/build-v8-rhel8.yml` | CI lane (native x86_64 ubuntu runner runs the rocky:8 container) |

## Where to run it

V8's bundled clang is **x86_64-Linux-host only**, so this lane is x64 and needs a
**native x86_64 Linux host**:

- **CI (recommended / authoritative):** dispatch `Build V8 (RHEL/Rocky-8 glibc
  floor)`. The ubuntu-24.04 runner is native x86_64 and runs the `rockylinux:8`
  container — no emulation. Use `compare_ubuntu=true` to also print the
  ubuntu-24.04 "before" floor for the delta.
- **Locally:** `tools/rhel8/build-in-docker.sh [V8_TAG]` on an x86_64 Docker host.
  On an arm64 Mac, Docker emulates x86_64 via qemu — correct but far too slow for a
  full V8 build; prefer the CI lane.

## Acceptance gate

1. `objdump -T libv8.so | grep -oE 'GLIBC_[0-9.]+' | sort -V | tail -1` ≤ `2.28`.
2. Seal audit (`seal/elf.py`) still passes — 0 ICU/zlib/Abseil leaks.
3. Identity harness links the sealed `.so` next to a real Skia ICU archive and
   asserts V8 init + `20+22` eval + version (no skip-pass).
