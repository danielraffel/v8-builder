# Pulp patch spec — P0.2 identity-anchored validation hook

**Why:** the v8-builder validation gate must assert *engine + GPU-backend identity*,
not pixels (proposal §8). Pulp's `threejs-native-demo` + `capture_test.cmake` today
only check "PNG non-empty" and **skip-pass when V8 is absent** — the exact false-pass
we must remove. This patch adds the minimal hooks so v8-builder's
`validate/run_validation.cmake` can prove the real V8 ran.

**Not yet applied** — Pulp `main` has your uncommitted WIP. Awaiting your go +
preferred flow (branch after you stash/commit, or I prepare a branch).

Grounded in current Pulp code (read-only inspection):
- `core/view/include/pulp/view/js_engine.hpp`: `JsEngine::type()`,
  `engine_type_name(JsEngineType)`, `is_engine_available(...)` exist; **no** runtime
  (V8) version accessor exists yet.
- `core/render/src/gpu_surface_dawn.cpp`: `dawn_backend_type_name(wgpu::BackendType)`;
  `core/render/src/gpu_compute.cpp`: `wgpu::AdapterInfo` (has `adapterType` →
  detect software/CPU) + backend name mapping.
- `examples/threejs-native-demo/main.cpp`: arg loop ~L1825; capture ~L1943;
  `is_engine_available(JsEngineType::v8)` gate ~L1886.

## Change 1 — expose the runtime (V8) version (interface)
`core/view/include/pulp/view/js_engine.hpp`, in `class JsEngine`:
```cpp
// Underlying engine runtime version string (e.g. V8 "13.6.233.8"); empty if N/A.
virtual std::string runtime_version() const { return {}; }
```

## Change 2 — implement it for V8
`core/view/src/js_v8_engine.cpp` (the only TU including <v8.h>):
```cpp
std::string V8Engine::runtime_version() const { return v8::V8::GetVersion(); }
```
(QuickJS/JSC keep the default empty — which is itself a useful signal.)

## Change 3 — `--print-engine-identity` in the demo
`examples/threejs-native-demo/main.cpp` arg loop, add:
```cpp
} else if (std::strcmp(argv[i], "--print-engine-identity") == 0) {
    print_engine_identity = true;
```
After the engine + GPU device are initialized (before/independent of the render loop),
print one machine-parseable block to stdout and exit 0:
```
PULP_ENGINE_IDENTITY_BEGIN
engine_type=v8
runtime_version=13.6.233.8
pulp_has_v8=1
gpu_backend=Metal            # dawn_backend_type_name(...)
gpu_adapter_type=DiscreteGPU # from wgpu::AdapterInfo.adapterType; CPU/Null => software
gpu_software=0
PULP_ENGINE_IDENTITY_END
```
- `engine_type` from `engine_type_name(engine.type())`.
- `runtime_version` from `engine.runtime_version()`.
- `pulp_has_v8` from the `PULP_HAS_V8` compile def.
- `gpu_backend`/`gpu_adapter_type`/`gpu_software` from the Dawn adapter info (mark
  software when `BackendType::Null` or `adapterType == CPU`).

## Change 4 — do NOT change capture_test.cmake's skip behavior in Pulp
Leave Pulp's `capture_test.cmake` as-is (it guards the #542 hang for Pulp's own CI).
v8-builder ships its OWN strict gate (`validate/run_validation.cmake`) that consumes
the identity block and **forbids skip-pass**. Separation keeps Pulp's CI semantics
intact while giving v8-builder the hard bar.

## Acceptance
With our V8: `--print-engine-identity` prints `engine_type=v8`,
`runtime_version` == the artifact `manifest.json` version, `pulp_has_v8=1`. With a
QuickJS/JSC build, `engine_type != v8` → v8-builder gate FAILS (no silent pass).
