# validate/run_validation.cmake — the GATING, identity-anchored harness.
#
# Drives Pulp's examples/threejs-native-demo (the real THREE.WebGPURenderer on V8 +
# Dawn) and enforces a result that is IMPOSSIBLE to fake. This deliberately replaces
# the permissive behavior of pulp's capture_test.cmake, which SKIPS (=passes) when V8
# is absent — exactly the false-pass we must not accept here.
#
# Inputs (-D):
#   DEMO_BIN            : built pulp-threejs-native-demo (configured with OUR V8)
#   CAPTURE_PATH        : PNG output path
#   EXPECTED_V8_VERSION : from the artifact manifest.json
#
# Pass criteria — ALL required, NO skip allowed:
#   - DEMO_BIN --print-engine-identity reports PULP_HAS_V8=1 AND
#     v8::V8::GetVersion() == EXPECTED_V8_VERSION (a fallback engine => FAIL)
#   - GPU adapter/backend identity is recorded; a software adapter
#     (SwiftShader/WARP/llvmpipe) => status "GPU-unverified", never a silent pass
#   - capture PNG exists, exceeds a real-content size, and a controlled region hash
#     is non-blank (not a cleared framebuffer)
#
# STATUS: skeleton — depends on the Pulp --print-engine-identity work (task P0.2).

if(NOT DEFINED DEMO_BIN OR NOT DEFINED EXPECTED_V8_VERSION)
    message(FATAL_ERROR "run_validation: set -DDEMO_BIN and -DEXPECTED_V8_VERSION")
endif()

message(STATUS "run_validation: SKELETON — Phase 0.2 implements the strict checks below.")
message(STATUS "  1) ${DEMO_BIN} --print-engine-identity  => assert PULP_HAS_V8=1 + version match")
message(STATUS "  2) run --demo cube --capture ${CAPTURE_PATH}  => assert non-blank content")
message(STATUS "  3) record GPU backend; software adapter => GPU-unverified (not a pass)")
message(FATAL_ERROR "run_validation: not implemented yet — refusing to report a pass (honesty bar).")
