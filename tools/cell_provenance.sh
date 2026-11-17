#!/usr/bin/env bash
# tools/cell_provenance.sh — package one build cell and prove its records.
#
#   tools/cell_provenance.sh write <cell> <platform> <arch> [container image]
#   tools/cell_provenance.sh prove
#
# `write` packs build/<cell> into package/v8-<cell>-<release id>.zip and
# writes the toolchain receipt and the legal manifest against that archive.
# Each record is written under provenance/ for diagnosis, and its
# content-addressed copy (the name a release publishes) goes to package/ only
# when it proves what it claims. The exit status of the record writers is
# appended to $GITHUB_ENV as provenance_status, so the archive and the
# diagnostics are still uploaded before `prove` fails the job.
#
# `prove` requires exactly one archive and one of each record in package/,
# and re-runs both writers' --check against the archive.
#
# Run from the repository root, after build-v8.py and the cell's validation.
set -uo pipefail

mode="${1:-}"
case "$mode" in
  write)
    cell="$2"; platform="$3"; arch="$4"; container="${5:-}"
    v8=build/src/v8
    out="$v8/out/$cell"
    mkdir -p package provenance
    meta="$(python3 tools/release_naming.py id "build/$cell/manifest.json" --v8-version "${V8_VERSION:-}")" || exit 1
    rid="$(printf '%s' "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release_id"])')" || exit 1
    archive="package/v8-$cell-$rid.zip"
    python3 tools/package_cell.py "build/$cell" "$archive" || exit 1
    case "$platform" in
      mac|ios) lib=libv8.dylib ;;
      linux|android) lib=libv8.so ;;
      win) lib=v8.dll ;;
      *) echo "::error::unknown platform $platform"; exit 1 ;;
    esac
    # The consumer link a cell's own job performs, if any. A receipt without
    # one says why.
    consumer=()
    case "$cell" in
      mac-*|linux-x64)
        if [ -f validate/build/v8_skia_validate ]; then
          consumer=(--consumer validate/build/v8_skia_validate --consumer-library "build/$cell/lib/$lib"
                    --consumer-ran --consumer-note "the Skia coexistence validator (forced-collision link and V8 identity) linked the packaged library and ran in this job")
        else
          consumer=(--consumer-note "the Skia coexistence validator did not build in this job (no skia-builder asset was reachable); the seal audit and the glibc floor gate are the checks")
        fi ;;
      win-x64)
        consumer=(--consumer "$out/v8_identity_validator.exe" --consumer-library "$out/$lib" --consumer-ran
                  --consumer-note "the GN identity validator linked v8.dll.lib and ran in this job") ;;
      win-arm64)
        consumer=(--consumer "$out/v8_identity_validator.exe" --consumer-library "$out/$lib"
                  --consumer-note "the GN identity validator linked v8.dll.lib here; it is cross-built and runs on a native arm64 Windows host") ;;
      android-*)
        consumer=(--consumer "$out/v8_identity_validator" --consumer-library "$out/$lib"
                  --consumer-note "the GN identity validator linked libv8.so here; it is cross-built and runs on an arm64 device or emulator") ;;
      linux-arm64)
        consumer=(--consumer-note "no consumer links in this job: the Skia coexistence harness is skipped for the linux/arm64 cell") ;;
      ios-*)
        consumer=(--consumer-note "no consumer links in this job: the Simulator ODR gate (validate/ios) is the iOS consumer check and the Mach-O seal audit is the coexistence check") ;;
    esac
    image=()
    [ -n "$container" ] && image=(--container-image "$container")
    # The checkout's own ninja reads the .ninja_deps the build wrote.
    export PATH="$PWD/build/tmp/depot_tools:$PATH"
    status=0
    python3 tools/write_toolchain_receipt.py provenance/toolchain-receipt.json \
      --archive "$archive" --platform "$platform" --arch "$arch" --cell "$cell" \
      --out-dir "$out" --source-root "$v8" ${image[@]+"${image[@]}"} "${consumer[@]}" \
      --content-addressed-dir package || status=1
    python3 tools/write_legal_manifest.py provenance/legal-manifest.json \
      --archive "$archive" --source-root "$v8" --out-dir "$out" --library "$lib" \
      --platform "$platform" --arch "$arch" --content-addressed-dir package || status=1
    if [ -n "${GITHUB_ENV:-}" ]; then
      echo "provenance_status=$status" >> "$GITHUB_ENV"
    fi
    ls -la package provenance
    exit 0
    ;;
  prove)
    set -e
    shopt -s nullglob
    zips=(package/*.zip)
    receipts=(package/*-toolchain.sha256-*.json)
    manifests=(package/*.legal-manifest.sha256-*.json)
    if [ "${provenance_status:-1}" != 0 ] || [ ${#zips[@]} -ne 1 ] || [ ${#receipts[@]} -ne 1 ] || [ ${#manifests[@]} -ne 1 ]; then
      echo "::error::the toolchain receipt or legal manifest does not prove what it claims; see this cell's provenance artifact"
      exit 1
    fi
    python3 tools/write_toolchain_receipt.py "${receipts[0]}" --archive "${zips[0]}" --check
    python3 tools/write_legal_manifest.py "${manifests[0]}" --archive "${zips[0]}" --check
    ;;
  *)
    sed -n '2,20p' "$0" >&2
    exit 2
    ;;
esac
