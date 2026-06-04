# validate/run_validation.cmake — the GATING, identity-anchored harness.
#
# Drives Pulp's examples/threejs-native-demo (real THREE.WebGPURenderer on V8 + Dawn)
# and enforces a result that is IMPOSSIBLE to fake. Unlike Pulp's capture_test.cmake,
# this NEVER skip-passes: a missing identity block, wrong engine, wrong version, or a
# blank capture is a hard FAIL.
#
# Requires the Pulp patch in planning/pulp-patch-P0.2.md (--print-engine-identity).
#
# Inputs (-D):
#   DEMO_BIN            : built pulp-threejs-native-demo (configured with OUR V8)
#   CAPTURE_PATH        : PNG output path
#   EXPECTED_V8_VERSION : from the artifact manifest.json
#   ALLOW_SOFTWARE_GPU  : OFF (default). If ON, software GPU is recorded, not failed,
#                         but GPU stays "unverified" — never counted as a real pass.

if(NOT DEFINED DEMO_BIN OR NOT DEFINED EXPECTED_V8_VERSION OR NOT DEFINED CAPTURE_PATH)
    message(FATAL_ERROR "run_validation: set -DDEMO_BIN -DEXPECTED_V8_VERSION -DCAPTURE_PATH")
endif()
if(NOT DEFINED ALLOW_SOFTWARE_GPU)
    set(ALLOW_SOFTWARE_GPU OFF)
endif()

# --- 1) Engine identity (unfakeable) --------------------------------------
execute_process(
    COMMAND "${DEMO_BIN}" --print-engine-identity
    TIMEOUT 60 RESULT_VARIABLE id_rc OUTPUT_VARIABLE id_out ERROR_VARIABLE id_err)
message(STATUS "engine-identity stdout:\n${id_out}")
if(id_err)
    message(STATUS "engine-identity stderr:\n${id_err}")
endif()
if(NOT id_rc STREQUAL "0")
    message(FATAL_ERROR "identity probe failed (rc=${id_rc}). NOT a pass.")
endif()
if(NOT id_out MATCHES "PULP_ENGINE_IDENTITY_BEGIN")
    message(FATAL_ERROR "no identity block — refusing to skip-pass. Is the Pulp patch applied?")
endif()

function(_id_field key out)
    if(id_out MATCHES "${key}=([^\n\r]*)")
        set(${out} "${CMAKE_MATCH_1}" PARENT_SCOPE)
    else()
        set(${out} "" PARENT_SCOPE)
    endif()
endfunction()
_id_field("engine_type" ENGINE_TYPE)
_id_field("runtime_version" RUNTIME_VERSION)
_id_field("pulp_has_v8" HAS_V8)
_id_field("gpu_backend" GPU_BACKEND)
_id_field("gpu_software" GPU_SOFTWARE)

if(NOT ENGINE_TYPE STREQUAL "v8")
    message(FATAL_ERROR "engine_type='${ENGINE_TYPE}' — a non-V8 engine ran. FAIL (no hallucinated pass).")
endif()
if(NOT HAS_V8 STREQUAL "1")
    message(FATAL_ERROR "pulp_has_v8='${HAS_V8}' — V8 not compiled in. FAIL.")
endif()
if(NOT RUNTIME_VERSION STREQUAL "${EXPECTED_V8_VERSION}")
    message(FATAL_ERROR "V8 version '${RUNTIME_VERSION}' != expected '${EXPECTED_V8_VERSION}' — wrong/foreign provider. FAIL.")
endif()
message(STATUS "engine identity OK: V8 ${RUNTIME_VERSION} (matches manifest)")

# --- 2) Capture exercises the path; assert real content -------------------
if(EXISTS "${CAPTURE_PATH}")
    file(REMOVE "${CAPTURE_PATH}")
endif()
execute_process(
    COMMAND "${DEMO_BIN}" --demo cube --capture "${CAPTURE_PATH}"
    TIMEOUT 60 RESULT_VARIABLE cap_rc OUTPUT_VARIABLE cap_out ERROR_VARIABLE cap_err)
if(NOT cap_rc STREQUAL "0")
    message(FATAL_ERROR "capture run failed (rc=${cap_rc}); stderr:\n${cap_err}")
endif()
if(NOT EXISTS "${CAPTURE_PATH}")
    message(FATAL_ERROR "no capture PNG written — FAIL.")
endif()
file(SIZE "${CAPTURE_PATH}" cap_size)
if(cap_size LESS 1024)
    message(FATAL_ERROR "capture PNG suspiciously small (${cap_size}B) — likely blank. FAIL.")
endif()
# TODO(Phase 1): decode + assert a controlled region is non-blank (not a cleared FB),
# tolerance-bounded hash as a change-detector, archive PNG as an artifact.

# --- 3) GPU backend identity (recorded; software != silent pass) ----------
if(GPU_SOFTWARE STREQUAL "1")
    if(ALLOW_SOFTWARE_GPU)
        message(WARNING "GPU is SOFTWARE (${GPU_BACKEND}) — recorded GPU-UNVERIFIED, not a real GPU pass.")
    else()
        message(FATAL_ERROR "GPU is software (${GPU_BACKEND}) and ALLOW_SOFTWARE_GPU=OFF — FAIL.")
    endif()
else()
    message(STATUS "GPU backend OK: ${GPU_BACKEND} (hardware)")
endif()

message(STATUS "run_validation: PASS — V8 ${RUNTIME_VERSION} rendered (${cap_size}B), backend ${GPU_BACKEND}.")
