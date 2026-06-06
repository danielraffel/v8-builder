# validate/ios/CMakeLists.txt — iOS Simulator GATE: link the SEALED V8.framework next
# to iOS Skia (flat ICU) AND Dawn (flat Abseil), forcing the duplicate-Abseil ODR
# condition, then run the identity contract on a booted Simulator.
#
# This is the iOS analog of validate/CMakeLists.txt. It builds a bare Mach-O for the
# iphonesimulator SDK; run_ios_gate.py wraps it in a minimal .app and launches it via
# `xcrun simctl`. A clean V8 init + eval 20+22==42 + version match + WASM-absent is the
# pass; an Abseil ODR abort in V8::Initialize() is the hard FAIL that says iOS is not
# viable even with the framework seal.
#
# Inputs (-D):
#   V8_FRAMEWORK_DIR   : dir containing V8.framework (the sealed artifact under test)
#   V8_INCLUDE_DIR     : V8 public headers (V8.framework/Headers works too)
#   SKIA_DIR           : dir containing iOS Skia archives (libskunicode_icu.a, ...)
#   EXPECTED_V8_VERSION
#   SKIA_DAWN_LIB      : path to Dawn's combined iOS archive (forces the Abseil collision)
cmake_minimum_required(VERSION 3.20)
# Enable BOTH CXX and OBJCXX: smoke_gpu.cpp is plain C++ (the collision-partner TU),
# ios_identity_app.mm is ObjC++. Declaring only OBJCXX leaves the .cpp with no enabled
# compiler, so it is silently dropped and v8builder_force_collision_partners() ends up
# undefined at link.
project(v8_ios_gate CXX OBJCXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT DEFINED V8_FRAMEWORK_DIR OR NOT DEFINED V8_INCLUDE_DIR OR NOT DEFINED SKIA_DIR)
  message(FATAL_ERROR "set -DV8_FRAMEWORK_DIR -DV8_INCLUDE_DIR -DSKIA_DIR")
endif()
if(NOT DEFINED EXPECTED_V8_VERSION)
  set(EXPECTED_V8_VERSION "UNSET")
endif()

# .mm is ObjC++ (needs the Foundation result-file writer in ios_identity_app.mm).
add_executable(v8_ios_gate
  ${CMAKE_CURRENT_LIST_DIR}/../ios_identity_app.mm
  ${CMAKE_CURRENT_LIST_DIR}/../smoke_gpu.cpp)
set_source_files_properties(
  ${CMAKE_CURRENT_LIST_DIR}/../ios_identity_app.mm PROPERTIES COMPILE_FLAGS "-ObjC++")
# For iOS, CMake defaults MACOSX_BUNDLE ON and emits the binary inside
# <target>.app/<target>. run_ios_gate.py assembles its OWN minimal .app (with the
# embedded V8.framework + Info.plist) and expects a BARE Mach-O at
# <build>/v8_ios_gate. Turn the bundle off so the binary lands where the harness reads.
set_target_properties(v8_ios_gate PROPERTIES MACOSX_BUNDLE OFF)
target_include_directories(v8_ios_gate PRIVATE ${V8_INCLUDE_DIR})
target_compile_definitions(v8_ios_gate PRIVATE
  EXPECTED_V8_VERSION="${EXPECTED_V8_VERSION}")

# Link the sealed V8.framework. The framework's install_name is @rpath/V8.framework/V8,
# so set an @loader_path rpath; run_ios_gate.py co-locates V8.framework next to the app
# binary inside the .app bundle.
target_link_options(v8_ios_gate PRIVATE
  "-F${V8_FRAMEWORK_DIR}"
  "-Wl,-rpath,@executable_path"
  "-Wl,-rpath,@loader_path")
# NOTE on framework linking: `target_link_libraries(t "-framework" V8)` does NOT work
# here — CMake treats "-framework" and "V8" as two independent link items and emits
# `-framework -lV8` (V8 demoted to `-l`, the flag orphaned), so `ld` reports
# "library 'V8' not found". Pass each framework as ONE quoted linker token via
# target_link_options + the LINKER: prefix so CMake keeps "-framework V8" intact.
target_link_options(v8_ios_gate PRIVATE
  "LINKER:-framework,V8"
  "LINKER:-framework,Foundation")
target_link_libraries(v8_ios_gate PRIVATE
  # Skia iOS flat ICU — the linker pulls u_errorName/ubrk_countAvailable members in,
  # placing Skia's flat ICU symbols beside our sealed V8 (coexistence condition #1).
  ${SKIA_DIR}/libskunicode_icu.a)

# The load-bearing iOS probe: force Dawn's flat Abseil in too (coexistence condition #2).
if(DEFINED SKIA_DAWN_LIB)
  target_compile_definitions(v8_ios_gate PRIVATE FORCE_DAWN_ABSEIL=1)
  target_link_options(v8_ios_gate PRIVATE
    "LINKER:-force_load,${SKIA_DAWN_LIB}"
    "LINKER:-framework,Metal"
    "LINKER:-framework,QuartzCore"
    "LINKER:-framework,IOSurface"
    "LINKER:-framework,CoreGraphics")
endif()
