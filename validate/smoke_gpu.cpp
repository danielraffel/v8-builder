// validate/smoke_gpu.cpp — the "other side" of the forced-collision link.
//
// References ICU symbols that live (flat, unversioned) in Skia's libskunicode_icu
// archive, so that archive's ICU (and, when Dawn is linked, Dawn's flat Abseil) is
// pulled into the validator binary alongside our SEALED V8. This recreates the
// duplicate-symbol condition that aborts an unsealed build.
//
// This TU never includes <v8.h> — mirroring Pulp's structural separation (V8 in one
// TU, the GPU/text stack in another, joined only at link).

#include <cstdint>

extern "C" {
// ICU C symbols shipped flat (unversioned) in Skia's skunicode_icu archive.
const char* u_errorName(int code);
int32_t ubrk_countAvailable(void);
}

extern "C" int v8builder_force_collision_partners() {
  // Force real references so the linker pulls the ICU members in (not dead-stripped).
  const char* name = u_errorName(0);              // U_ZERO_ERROR -> "U_ZERO_ERROR"
  int32_t n = ubrk_countAvailable();              // available break-iterator locales
  int sum = (name ? (int)name[0] : 0) + (int)n;
  return sum;
}
