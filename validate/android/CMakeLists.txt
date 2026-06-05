# validate/android/CMakeLists.txt — the EXTERNAL Android libc++-ABI gate (portable form).
#
# This is the portable, full-NDK form of the gate. The ACTUAL gate run in this lane uses
# the sibling `link_consumer.sh`, because the DEPS cipd android_toolchain that V8 fetches
# is a stripped BUILD toolchain: it has no `android.toolchain.cmake` and no standalone
# clang driver / libunwind.a, so cmake's android toolchain can't drive it. With a FULL
# app NDK installed (one that ships android.toolchain.cmake), THIS file is the equivalent.
#
# Either way it is an EXTERNAL consumer (NOT a gn target inside the V8 tree): it links the
# PACKAGED headers + sealed libv8.so exactly as an Android app would, exercising V8's
# std::-typed public surface — v8::platform::NewDefaultPlatform() returns
# std::unique_ptr<v8::Platform>. Because libv8.so ships V8's bundled __Cr libc++ (the
# NDK-libc++ target ABI is blocked on the cipd toolchain), the consumer must compile
# against a Chromium-style __Cr libc++ (the Windows-model contract); a clean link is the
# gate's pass. See planning/2026-06-05-android-plan.md §4 + review addendum #2.
#
# Configure with the NDK toolchain file + ABI + a __Cr libc++, e.g.:
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
