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

// FORCE_DAWN_ABSEIL: pull a Dawn-bundled Abseil symbol into the binary so V8's
// (sealed, internal) Abseil and Dawn's flat Abseil are BOTH resident — the exact
// duplicate-Abseil condition that aborts an unsealed build (P0.3a). On iOS this is
// the load-bearing probe: a static V8 .a could not hide its Abseil and would abort
// here; a SEALED V8.framework keeps its Abseil internal so the two coexist.
//
// We reference absl::base_internal::GetTID() — a flat, stable, no-arg Abseil entry
// point VERIFIED present in Dawn's bundled iOS Abseil
// (__ZN4absl13base_internal6GetTIDEv; Dawn's libdawn_combined.a carries ~2472 flat
// absl symbols). Declared with the real C++ linkage so the mangled name resolves
// against Dawn's archive at link, pulling Dawn's Abseil into the binary. Its return
// type is absl::base_internal::pid_t-ish (int64 on this platform); we only need the
// reference to resolve, so a matching signature is enough.
#ifdef FORCE_DAWN_ABSEIL
namespace absl { namespace base_internal {
int64_t GetTID();
}}  // namespace absl::base_internal
#endif

extern "C" int v8builder_force_collision_partners() {
  // Force real references so the linker pulls the ICU members in (not dead-stripped).
  const char* name = u_errorName(0);              // U_ZERO_ERROR -> "U_ZERO_ERROR"
  int32_t n = ubrk_countAvailable();              // available break-iterator locales
  int sum = (name ? (int)name[0] : 0) + (int)n;
#ifdef FORCE_DAWN_ABSEIL
  // Reference Dawn's Abseil so its flat copy is resident alongside V8's sealed copy.
  sum += (int)(absl::base_internal::GetTID() & 0xFF);
#endif
  return sum;
}
