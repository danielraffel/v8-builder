#!/usr/bin/env bash
# tools/rhel8/build-in-docker.sh — build + seal the Linux V8 inside the Rocky 8
# (glibc-2.28) image and assert the glibc floor (task #24b).
#
# Run this on an x86_64 Docker host (a native Intel Linux box, or an x86_64 cloud
# runner). On an arm64 Mac, Docker would emulate x86_64 via qemu — correct but
# slow (a full V8 build under emulation is impractical); prefer the CI lane
# (.github/workflows/build-v8-rhel8.yml) or a native x86_64 host.
#
# What it does:
#   1. docker build the rockylinux:8 + gcc-toolset-12 image (tools/rhel8/Dockerfile)
#   2. run `build-v8.py linux -archs x64` inside it (system libstdc++ on glibc 2.28)
#   3. seal audit (seal/elf.py, 0 ICU/zlib/Abseil leaks) runs as part of build-v8.py
#   4. assert the glibc floor of the produced libv8.so is <= 2.28
#
# Usage:
#   tools/rhel8/build-in-docker.sh [V8_TAG]
# Env:
#   V8_MAX_GLIBC   max acceptable floor (default 2.28)
#   DOCKER         docker binary (default: docker)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
V8_TAG="${1:-}"
MAX_GLIBC="${V8_MAX_GLIBC:-2.28}"
DOCKER="${DOCKER:-docker}"
IMAGE="v8-builder-rhel8:local"

if ! command -v "$DOCKER" >/dev/null 2>&1; then
  echo "ERROR: '$DOCKER' not found. Need an x86_64 Docker host." >&2
  exit 2
fi

echo "==> building image $IMAGE (rockylinux:8 + gcc-toolset-12)"
"$DOCKER" build --platform linux/amd64 -t "$IMAGE" -f "$REPO_ROOT/tools/rhel8/Dockerfile" "$REPO_ROOT/tools/rhel8"

# Build args: a -tag if given, else --use-synced (build the fetched tip).
if [ -n "$V8_TAG" ]; then BUILD_ARGS="linux -archs x64 -tag $V8_TAG";
else BUILD_ARGS="linux -archs x64 --use-synced"; fi

echo "==> building + sealing V8 in the container ($BUILD_ARGS)"
# Mount the repo so the sealed artifact + manifest land back on the host.
# The ENTRYPOINT enables gcc-toolset-12; we pass the build command as the CMD.
"$DOCKER" run --rm --platform linux/amd64 \
  -v "$REPO_ROOT:/work" -w /work \
  "$IMAGE" \
  bash -lc "python3 build-v8.py $BUILD_ARGS"

LIB="$REPO_ROOT/build/linux-x64/lib/libv8.so"
if [ ! -f "$LIB" ]; then
  echo "ERROR: expected sealed $LIB not produced" >&2
  exit 1
fi

echo "==> glibc-floor gate (objdump -T, max <= $MAX_GLIBC)"
# Run the floor check INSIDE the container so objdump is the rocky-8 binutils
# (the host may be macOS with no objdump).
"$DOCKER" run --rm --platform linux/amd64 \
  -v "$REPO_ROOT:/work" -w /work \
  "$IMAGE" \
  bash -lc "python3 tools/rhel8/check_glibc_floor.py --lib build/linux-x64/lib/libv8.so --max $MAX_GLIBC --json"

echo "==> done: sealed libv8.so at $LIB, glibc floor <= $MAX_GLIBC"
