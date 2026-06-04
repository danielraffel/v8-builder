# validate/CMakeLists.txt — standalone coexistence + identity validator.
# Links our SEALED V8 next to Skia's flat-ICU archive (and Dawn when SKIA_DAWN_LIB is
# given), forcing the collision-bearing members in. A duplicate-symbol/ODR abort here
# is a hard FAIL; a clean V8 init + JS eval + version match is the pass.
#
# Inputs (-D):
#   V8_INCLUDE_DIR, V8_LIBRARY_PATH   (the sealed artifact under test)
#   SKIA_DIR                           (dir containing libskunicode_icu.a, libskia.a, ...)
#   EXPECTED_V8_VERSION
#   SKIA_DAWN_LIB (optional)           (path to libdawn_combined.a to also force the
#                                       Abseil-collision condition; needs GPU frameworks)
cmake_minimum_required(VERSION 3.20)
project(v8_skia_validate CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT DEFINED V8_LIBRARY_PATH OR NOT DEFINED V8_INCLUDE_DIR OR NOT DEFINED SKIA_DIR)
  message(FATAL_ERROR "set -DV8_LIBRARY_PATH -DV8_INCLUDE_DIR -DSKIA_DIR")
endif()
if(NOT DEFINED EXPECTED_V8_VERSION)
  set(EXPECTED_V8_VERSION "UNSET")
endif()

add_executable(v8_skia_validate identity_main.cpp smoke_gpu.cpp)
target_include_directories(v8_skia_validate PRIVATE ${V8_INCLUDE_DIR})
target_compile_definitions(v8_skia_validate PRIVATE EXPECTED_V8_VERSION="${EXPECTED_V8_VERSION}")

# Link Skia's ICU archive normally: the linker pulls the ICU members that smoke_gpu
# references (u_errorName, ubrk_countAvailable), putting Skia's FLAT ICU symbols in the
# binary alongside our sealed V8 — the coexistence condition under test. (We avoid
# force_load + libskia.a to keep out Skia's SkUnicode/Graphite archive web; the full
# Dawn/Abseil coexistence is covered by the Pulp demo and by SKIA_DAWN_LIB below.)
target_link_libraries(v8_skia_validate PRIVATE
  ${V8_LIBRARY_PATH}
  ${SKIA_DIR}/libskunicode_icu.a
)

# Optional: also force Dawn's flat Abseil in (the strongest collision test).
if(DEFINED SKIA_DAWN_LIB)
  target_compile_definitions(v8_skia_validate PRIVATE WITH_DAWN=1)
  if(APPLE)
    target_link_libraries(v8_skia_validate PRIVATE "-Wl,-force_load,${SKIA_DAWN_LIB}"
      "-framework" Metal "-framework" QuartzCore "-framework" IOSurface
      "-framework" IOKit "-framework" Foundation "-framework" CoreGraphics)
  else()
    target_link_libraries(v8_skia_validate PRIVATE
      "-Wl,--whole-archive" "${SKIA_DAWN_LIB}" "-Wl,--no-whole-archive")
  endif()
endif()

if(APPLE)
  # find the sealed dylib at runtime (install_name is @rpath/libv8.dylib)
  get_filename_component(_v8dir "${V8_LIBRARY_PATH}" DIRECTORY)
  set_target_properties(v8_skia_validate PROPERTIES BUILD_RPATH "${_v8dir}")
  find_library(CF_LIB CoreFoundation)
  find_library(CT_LIB CoreText)
  target_link_libraries(v8_skia_validate PRIVATE ${CF_LIB} ${CT_LIB})
endif()
