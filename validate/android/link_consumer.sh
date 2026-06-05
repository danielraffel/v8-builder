#!/bin/bash
# validate/android/link_consumer.sh — the Android libc++-ABI gate (link step).
#
# Builds the EXTERNAL consumer (consumer_main.cpp) against the PACKAGED sealed libv8.so,
# proving an Android app can link V8's std::-typed public surface
# (v8::platform::NewDefaultPlatform -> std::unique_ptr). Because libv8.so is built with
# V8's bundled __Cr libc++ (the NDK-libc++ target ABI is blocked on the DEPS cipd
# android_toolchain — see build-v8.py android_gn_args), the consumer must compile against
# a Chromium-style __Cr libc++, exactly the Windows-model contract. This is the faithful
# proof of that contract.
#
# Why a script and not the sibling CMakeLists.txt: the cipd android_toolchain ships no
# `android.toolchain.cmake` and no standalone clang driver / libunwind.a, so the link is
# driven directly with V8's bundled clang + the in-tree __Cr libc++/libc++abi/libunwind
# (the exact runtime V8 was built with). With a FULL app NDK installed, the sibling
# CMakeLists.txt is the portable equivalent.
#
# Usage:
#   validate/android/link_consumer.sh <V8_SRC_DIR> <OUT_DIR> <PKG_DIR> [api_level]
# e.g. validate/android/link_consumer.sh \
#        ~/v8-builder/build/src/v8 \
#        ~/v8-builder/build/src/v8/out/android-arm64 \
#        ~/v8-builder/build/android-arm64 29
set -euo pipefail
ROOT="${1:?V8 src dir}"; OUT="${2:?gn out dir}"; PKG="${3:?packaged artifact dir}"
API="${4:-29}"
HERE="$(cd "$(dirname "$0")" && pwd)"

NDK="$ROOT/third_party/android_toolchain/ndk/toolchains/llvm/prebuilt/linux-x86_64"
CLANG="$ROOT/third_party/llvm-build/Release+Asserts/bin/clang++"
AR="$ROOT/third_party/llvm-build/Release+Asserts/bin/llvm-ar"
LIBCXXCFG="$ROOT/buildtools/third_party/libc++"          # generated __config_site (__Cr)
LIBCXX="$ROOT/third_party/libc++/src/include"
LIBCXXABI="$ROOT/third_party/libc++abi/src/include"
CXXA="$OUT/obj/buildtools/third_party/libc++/libc++.a"
CXXABIA="$OUT/obj/buildtools/third_party/libc++abi/libc++abi.a"
UWOBJS="$OUT/obj/buildtools/third_party/libunwind/libunwind"
LIB="$PKG/lib/libv8.so"
INC="$PKG/include"
VER="$(python3 -c "import json;print(json.load(open('$PKG/manifest.json'))['v8_version'])")"

# clang's android driver hard-adds `-l:libunwind.a`; the cipd toolchain ships none, so
# archive V8's bundled libunwind objects (the same unwinder V8 uses) to satisfy it.
UWDIR="$(mktemp -d)"; trap 'rm -rf "$UWDIR"' EXIT
"$AR" rcs "$UWDIR/libunwind.a" "$UWOBJS"/*.o

echo "[android-gate] linking __Cr-libc++ consumer (aarch64-linux-android$API) against $LIB"
"$CLANG" \
  --target="aarch64-linux-android$API" \
  --sysroot="$NDK/sysroot" \
  -fno-rtti -std=c++20 -nostdinc++ -nostdlib++ \
  -D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST \
  -L"$UWDIR" \
  -isystem "$LIBCXXCFG" -isystem "$LIBCXX" -isystem "$LIBCXXABI" \
  -I"$INC" \
  -DEXPECTED_V8_VERSION="\"$VER\"" \
  "$HERE/consumer_main.cpp" "$LIB" \
  -Wl,--start-group "$CXXA" "$CXXABIA" -l:libunwind.a -Wl,--end-group \
  -lm -ldl -llog \
  -o "$OUT/v8_android_consumer"

echo "[android-gate] CONSUMER LINK OK"
file "$OUT/v8_android_consumer"
readelf -d "$OUT/v8_android_consumer" | grep NEEDED
# Bundle exe + libv8.so for an offline arm64-emulator/device run (adb push + run).
mkdir -p "$PKG/validate"
cp "$OUT/v8_android_consumer" "$LIB" "$PKG/validate/"
echo "[android-gate] bundled consumer + libv8.so into $PKG/validate/ (run on an arm64 device:"
echo "  adb push v8_android_consumer libv8.so /data/local/tmp/v8c/ && \\"
echo "  adb shell 'cd /data/local/tmp/v8c && LD_LIBRARY_PATH=. ./v8_android_consumer')"
