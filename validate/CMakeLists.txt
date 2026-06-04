# validate/CMakeLists.txt — cross-link proof: V8 + real Skia/Dawn in one binary.
# STATUS: skeleton. Wires up in Phase 1 (Linux) / Phase 2 (macOS).
#
# Inputs (set via -D):
#   V8_INCLUDE_DIR, V8_LIB_DIR, V8_LIBRARY_PATH   (the artifact under test)
#   SKIA_DIR                                       (unzipped, provenance-verified Skia release)
#   EXPECTED_V8_VERSION                            (from the artifact manifest.json)
#
# Builds the synthetic forced-collision identity binary. The Skia ICU/shaper/zlib
# archives are linked under whole-archive so the collision-bearing members are pulled
# (a draw-rect would NOT pull them — proposal §8).

cmake_minimum_required(VERSION 3.20)
project(v8_skia_validate CXX)

set(CMAKE_CXX_STANDARD 20)

if(NOT DEFINED V8_LIBRARY_PATH OR NOT DEFINED SKIA_DIR)
    message(FATAL_ERROR "validate: set -DV8_LIBRARY_PATH and -DSKIA_DIR (and V8_INCLUDE_DIR).")
endif()

add_executable(v8_skia_validate identity_main.cpp) # + smoke_gpu.cpp (Skia/zlib) in Phase 1
target_compile_definitions(v8_skia_validate PRIVATE
    EXPECTED_V8_VERSION="${EXPECTED_V8_VERSION}")
target_include_directories(v8_skia_validate PRIVATE ${V8_INCLUDE_DIR})

# Phase 1+: link V8 (the sealed artifact) + Skia archives under whole-archive, e.g.
#   target_link_libraries(v8_skia_validate PRIVATE
#     ${V8_LIBRARY_PATH}
#     -Wl,--whole-archive ${SKIA_DIR}/lib/libskunicode_icu.a ${SKIA_DIR}/lib/libskshaper.a -Wl,--no-whole-archive
#     ${SKIA_DIR}/lib/libskia.a ...)
# A duplicate-symbol error here is a HARD FAIL (the bug class we guard against).

message(STATUS "validate: skeleton configured (V8=${V8_LIBRARY_PATH}, SKIA=${SKIA_DIR})")
