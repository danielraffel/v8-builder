// validate/identity_main.cpp — OS-agnostic standalone coexistence + identity validator.
//
// The pulp threejs-native-demo proves coexistence on macOS with the real stack, but
// it isn't available on CI (private, heavy). This standalone binary reproduces the
// hazard so Linux/Windows CI can validate too:
//   - it links our SEALED libv8 AND force-loads Dawn + Skia's ICU archive, so Dawn's
//     flat Abseil and Skia's flat ICU/zlib are present in the executable alongside
//     V8's (sealed, internal) copies — exactly the condition that aborted an UNSEALED
//     build (P0.3a). If our seal is correct, V8 initializes and runs; if not, it aborts.
//   - it asserts ENGINE IDENTITY (not pixels): V8 actually initializes, evaluates JS,
//     and reports a version equal to EXPECTED_V8_VERSION.
//
// Exit 0 ONLY if V8 inits + evals + version matches. Any abort / mismatch = fail.

#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <string>

#include <libplatform/libplatform.h>
#include <v8.h>

#ifndef EXPECTED_V8_VERSION
#define EXPECTED_V8_VERSION "UNSET"
#endif

// Defined in smoke_gpu.cpp — references Dawn + Skia ICU so their archives (with flat
// Abseil/ICU/zlib) are pulled into THIS binary, recreating the collision condition.
extern "C" int v8builder_force_collision_partners();

int main(int argc, char* argv[]) {
  // Pull Dawn/Skia symbols in first so their Abseil/ICU/zlib are resident before V8 runs.
  volatile int partners = v8builder_force_collision_partners();
  (void)partners;

  v8::V8::InitializeICUDefaultLocation(argv[0]);
  v8::V8::InitializeExternalStartupData(argv[0]);
  std::unique_ptr<v8::Platform> platform = v8::platform::NewDefaultPlatform();
  v8::V8::InitializePlatform(platform.get());
  v8::V8::Initialize();   // <-- aborted here on an UNSEALED build (Abseil ODR)

  int answer = -1;
  std::string version = v8::V8::GetVersion();
  {
    v8::Isolate::CreateParams params;
    params.array_buffer_allocator = v8::ArrayBuffer::Allocator::NewDefaultAllocator();
    v8::Isolate* isolate = v8::Isolate::New(params);
    {
      v8::Isolate::Scope iscope(isolate);
      v8::HandleScope hscope(isolate);
      v8::Local<v8::Context> ctx = v8::Context::New(isolate);
      v8::Context::Scope cscope(ctx);
      v8::Local<v8::String> src = v8::String::NewFromUtf8Literal(isolate, "20+22");
      v8::Local<v8::Script> script = v8::Script::Compile(ctx, src).ToLocalChecked();
      answer = script->Run(ctx).ToLocalChecked()->Int32Value(ctx).FromJust();
    }
    isolate->Dispose();
    delete params.array_buffer_allocator;
  }

  std::printf("PULP_ENGINE_IDENTITY_BEGIN\nengine_type=v8\nruntime_version=%s\n"
              "js_eval_20_plus_22=%d\ncollision_partners=%d\nPULP_ENGINE_IDENTITY_END\n",
              version.c_str(), answer, partners);

  if (answer != 42) { std::fprintf(stderr, "FAIL: V8 eval wrong (%d != 42)\n", answer); return 1; }
  if (version != EXPECTED_V8_VERSION) {
    std::fprintf(stderr, "FAIL: V8 version '%s' != expected '%s'\n", version.c_str(), EXPECTED_V8_VERSION);
    return 1;
  }
#ifdef WITH_DAWN
  const char* partners_desc = "Dawn(Abseil)+Skia(ICU)";
#else
  const char* partners_desc = "Skia(ICU/zlib); Dawn-Abseil covered by the Pulp demo / set SKIA_DAWN_LIB to add it here";
#endif
  std::fprintf(stderr, "PASS: V8 %s coexists with %s (no collision), eval OK\n",
               version.c_str(), partners_desc);
  return 0;
}
