# validate/android/CMakeLists.txt — the REAL Android libc++-ABI gate.
#
# This is deliberately an EXTERNAL, stock-NDK consumer: it is NOT a gn target inside
# the V8 tree. It links the PACKAGED headers + the sealed libv8.so exactly as a normal
# NDK app would, using the NDK's own toolchain file and the NDK's default STL. That is
# the only way to prove the *consumable* libc++ ABI — a gn-target validator (built with
# V8's Chromium toolchain) would only prove a Chromium-toolchain consumer can link, not
# a stock-NDK/Pulp one. See planning/2026-06-05-android-plan.md §4 + review addendum #2.
#
# It exercises V8's std::-typed public surface — v8::platform::NewDefaultPlatform()
# returns std::unique_ptr<v8::Platform> — which is exactly where a __Cr-vs-NDK libc++
# mismatch would surface as an undefined reference at link. A clean link + run is the
# gate's pass.
#
# Configure with the NDK toolchain file + ABI, e.g.:
#   cmake -S validate/android -B validate/android/build \
#     -DCMAKE_TOOLCHAIN_FILE=$NDK/build/cmake/android.toolchain.cmake \
#     -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-29 \
#     -DV8_INCLUDE_DIR=<pkg>/include -DV8_LIBRARY_PATH=<pkg>/lib/libv8.so \
#     -DEXPECTED_V8_VERSION=15.1.27
cmake_minimum_required(VERSION 3.20)
project(v8_android_consumer CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT DEFINED V8_LIBRARY_PATH OR NOT DEFINED V8_INCLUDE_DIR)
  message(FATAL_ERROR "set -DV8_LIBRARY_PATH (sealed libv8.so) and -DV8_INCLUDE_DIR (packaged include/)")
endif()
if(NOT DEFINED EXPECTED_V8_VERSION)
  set(EXPECTED_V8_VERSION "UNSET")
endif()

# The consumer must compile with V8's exact feature defines or v8::V8::Initialize aborts
# on an embedder-vs-V8 mismatch (proven on Windows: the validator needs :external_config).
# For Android the relevant non-default toggles are pointer-compression OFF + sandbox OFF
# (build-v8.py common_gn_args). V8's public headers gate ABI on V8_COMPRESS_POINTERS /
# V8_ENABLE_SANDBOX, so a consumer that does NOT define them matches our compression-OFF,
# sandbox-OFF build. We therefore define NEITHER here (matching the artifact), and assert
# that contract loudly so a future compression-ON Android artifact forces this to update.

add_executable(v8_android_consumer consumer_main.cpp)
target_include_directories(v8_android_consumer PRIVATE ${V8_INCLUDE_DIR})
target_compile_definitions(v8_android_consumer PRIVATE EXPECTED_V8_VERSION="${EXPECTED_V8_VERSION}")

# Link the sealed libv8.so by absolute path (a relative path would be treated as a -l
# library NAME by cmake). The NDK toolchain links the NDK's libc++ (c++_shared by
# default) — that is the ABI under test.
target_link_libraries(v8_android_consumer PRIVATE ${V8_LIBRARY_PATH})

# So the runtime loader finds libv8.so next to the exe when pushed to the device.
set_target_properties(v8_android_consumer PROPERTIES BUILD_RPATH "$ORIGIN")
