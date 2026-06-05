# #30 — Pulp consumes the sealed v8-builder V8 (macOS A/B vs libnode → flip default)

Scoped via RepoPrompt context_builder (chat `sealed-v8-plan-C6CF1E`) against real Pulp +
v8-builder code, 2026-06-05. The published release is `v8-15.1.27` (sealed libv8.dylib +
include/ + manifest.json per cell).

## Current state (grounded)
- `pulp/core/view/CMakeLists.txt`: `PULP_JS_ENGINE=auto|quickjs|jsc|v8`; V8 needs
  `V8_INCLUDE_DIR` + `V8_LIB_DIR`, accepts `V8_LIBRARY_PATH`/`V8_LIBRARY_NAME`, else globs
  `v8_monolith`/`node`/`libnode.*`. **It does NOT discover `libv8.dylib`** unless given
  `V8_LIBRARY_PATH`. `PULP_HAS_V8=1` when found; `PULP_DEFAULT_ENGINE_V8=1` only for explicit
  `=v8`. `auto` stays QuickJS (the CMake comment claiming otherwise is stale).
- Runtime: `JsEngine`←`ScriptEngine`; `js_v8_engine.cpp` is a thin adapter over CHOC
  `choc::javascript::V8Context` → needs the v8-builder CHOC patch (Utf8LengthV2/WriteUtf8V2/
  Isolate::GetCurrent) for V8 15.x.
- A/B surface: `examples/threejs-native-demo` (ScriptEngine(v8) + WidgetBridge + Dawn/Skia
  GPU; `--demo cube --capture` PNG). `capture_test.cmake` skip-passes when no V8/Dawn — fine
  for Pulp CI, insufficient for #30 (needs no-skip identity).
- "Flip default" (D4) = make explicit-V8 PREFER the sealed libv8 over libnode (a provider
  preference), NOT change the `auto` engine default.

## Design (additive, non-breaking)
A. **CMake provider**: add `PULP_V8BUILDER_ROOT` (unpacked release root) → validate
   include/v8.h + lib/libv8.dylib + manifest.json, parse `v8_version`, optional
   `PULP_V8BUILDER_EXPECTED_VERSION` equality, derive V8_INCLUDE_DIR/LIB_DIR/LIBRARY_PATH,
   emit `PULP_V8_PROVIDER_KIND`/`_PATH`/`_EXPECTED_RUNTIME_VERSION` defines. Fatal if an
   explicit V8 path outside the root conflicts (so the sealed lane can't link libnode). Keep
   libnode discovery for the A/B baseline. Do NOT change the engine default.
B. **Identity API**: add `runtime_version()`/`provider_kind()`/`provider_path()`/
   `expected_runtime_version()` to `JsEngine` (default empty) + `ScriptEngine` forwarders;
   V8 impl returns `v8::V8::GetVersion()` + the compile-def provider values.
C. **Demo**: add `--print-engine-identity` to threejs-native-demo → prints a
   `PULP_ENGINE_IDENTITY_BEGIN…END` block (engine_type, runtime_version, provider_kind,
   provider_path, pulp_has_v8, gpu_available, gpu_native_bridge, gpu_backend, gpu_software).
D. **Strict CTest** `provider_identity_test.cmake` (gated on
   `PULP_VALIDATE_V8_PROVIDER_STRICT`): assert identity (no skip-pass) + `--demo cube
   --capture` produces a non-empty PNG.
E. **Tests**: extend `test/test_js_engine.cpp` with V8 runtime/provider identity asserts.

## A/B procedure
- **Sealed**: configure `-DPULP_JS_ENGINE=v8 -DPULP_V8BUILDER_ROOT=<unpacked mac-arm64>
  -DPULP_V8BUILDER_EXPECTED_VERSION=15.1.27 -DPULP_VALIDATE_V8_PROVIDER_STRICT=ON`; build
  `pulp-test-js-engine` + `pulp-threejs-native-demo`; ctest the identity/cube; `otool -L`
  must show `@rpath/libv8.dylib` and NOT libnode.
- **libnode baseline**: configure with `V8_*` pointing at `$(brew --prefix node)`; same tests,
  expected `provider_kind=libnode`.

## Flip gates (D4 — do NOT flip until all pass)
1. Both builds pass `pulp-test-js-engine`. 2. Both render `--demo cube --capture` (no skip).
3. Sealed identity: provider_kind=v8builder, runtime_version=15.1.27, pulp_has_v8=1.
4. `otool -L` proves no libnode in the sealed build. 5. Hardware GPU (Metal, gpu_software=0,
   native bridge). 6. Three.js benchmark runs, upload counters non-zero. 7. Sealed median
   ≤25% slower than libnode (3 runs). 8. Repeat on mac x86_64 (Rosetta). 9. v8-builder seal
   audit green (0 ICU/zlib/Abseil).

## Implementation order
CMake provider → identity API → engine tests → demo CLI → strict CTest → A/B run → docs →
(separate, gated) default-flip patch.

## Risks
- CHOC V8 15.x: Pulp's CHOC must carry the v8-builder patch (Utf8LengthV2 etc.) or
  js_v8_engine.cpp won't compile. Gate: it compiles.
- rpath: sealed install_name is `@rpath/libv8.dylib`; the demo must run on a clean env w/o
  libnode.
