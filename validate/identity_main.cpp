// validate/identity_main.cpp
//
// Synthetic "forced-collision + identity" proof binary. This is the stress test that
// links V8 next to the REAL Skia archives and refuses to pass by hallucination.
//
// It is the ADDITIONAL stress test; the GATING validation is the Pulp-shaped
// threejs-native-demo capture (see validate/run_validation.cmake + the runbook).
//
// Identity, not pixels (proposal §8, Codex adversarial pass):
//   1) ENGINE IDENTITY: read v8::V8::GetVersion() and assert it equals the version
//      this artifact's manifest.json records (passed via -DEXPECTED_V8_VERSION).
//      Other JS engines are NOT compiled in — a substitution is a build error, not a
//      silent pass.
//   2) FORCED COLLISION: also call into Skia's ICU/shaper + zlib paths (in a separate
//      TU, linked under whole-archive) so the ICU/zlib members are actually pulled and
//      a duplicate-symbol clash would fail the LINK, not slip through.
//
// STATUS: skeleton. Compiles only once V8 + Skia are wired in (Phase 1+). The
// structure below is the intended contract.

#include <cstdio>
#include <cstring>
#include <cstdlib>

// #include <v8.h>            // the ONLY TU that includes V8
// extern "C" int skia_forced_collision_probe(char* out, int n);  // from smoke_gpu (Skia/zlib/ICU)

#ifndef EXPECTED_V8_VERSION
#define EXPECTED_V8_VERSION "UNSET"
#endif

int main(int /*argc*/, char** /*argv*/) {
    std::fprintf(stderr,
        "[identity_main] SKELETON. Phase 1+ will:\n"
        "  - init v8::Platform -> V8::Initialize -> Isolate (documented init order)\n"
        "  - assert strcmp(v8::V8::GetVersion(), \"%s\") == 0  (engine identity)\n"
        "  - eval JS and assert a computed result only V8 produces\n"
        "  - call skia_forced_collision_probe() to pull Skia ICU/shaper + zlib members\n"
        "  - exit 0 ONLY if all identity assertions hold; never on a skip.\n",
        EXPECTED_V8_VERSION);
    return 2; // skeleton: not a pass
}
