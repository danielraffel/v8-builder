// validate/android/consumer_main.cpp — external V8 consumer (the libc++-ABI gate).
//
// Built by validate/android/link_consumer.sh (or the sibling CMakeLists.txt with a full
// NDK), linking ONLY the packaged headers + sealed libv8.so. It deliberately exercises
// V8's std::-typed public surface:
//
//   std::unique_ptr<v8::Platform> = v8::platform::NewDefaultPlatform();
//
// libv8.so is built with V8's bundled __Cr-namespaced libc++ (the NDK-libc++ target ABI
// is blocked on the DEPS cipd android_toolchain — see build-v8.py android_gn_args), so
// the consumer must compile against a Chromium-style __Cr libc++, exactly the Windows-
// model contract. The proof is that the consumer's undefined reference to
// `v8::platform::NewDefaultPlatform(..., std::__Cr::unique_ptr<...>, ...)` mangles
// IDENTICALLY to the symbol libv8.so exports (verified: the __Cr in both the consumer's
// undefined ref and the library's export match) — a clean link is the gate's pass. The
// in-tree gn validator proves a Chromium-toolchain consumer; this proves an external one
// built outside the V8 tree, which is the consumer story Pulp actually ships.
//
// Asserts ENGINE IDENTITY: V8 inits, evals 20+22 == 42, and GetVersion() ==
// EXPECTED_V8_VERSION. Exit 0 only on all three (the run needs an arm64 device/emulator).

#include <cstdio>
#include <memory>
#include <string>

#include <libplatform/libplatform.h>
#include <v8.h>

#ifndef EXPECTED_V8_VERSION
#define EXPECTED_V8_VERSION "UNSET"
#endif

int main(int argc, char* argv[]) {
  v8::V8::InitializeICUDefaultLocation(argv[0]);
  v8::V8::InitializeExternalStartupData(argv[0]);
  // The std:: surface under ABI test: a __Cr-vs-NDK libc++ skew fails to link here.
  std::unique_ptr<v8::Platform> platform = v8::platform::NewDefaultPlatform();
  v8::V8::InitializePlatform(platform.get());
  v8::V8::Initialize();

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
              "js_eval_20_plus_22=%d\nconsumer=stock-ndk-libcxx\n"
              "PULP_ENGINE_IDENTITY_END\n",
              version.c_str(), answer);

  if (answer != 42) {
    std::fprintf(stderr, "FAIL: V8 eval wrong (%d != 42)\n", answer);
    return 1;
  }
  if (version != EXPECTED_V8_VERSION) {
    std::fprintf(stderr, "FAIL: V8 version '%s' != expected '%s'\n",
                 version.c_str(), EXPECTED_V8_VERSION);
    return 1;
  }
  std::fprintf(stderr,
               "PASS: stock-NDK consumer linked + ran sealed libv8.so (V8 %s), "
               "libc++ ABI consumable, eval OK\n", version.c_str());
  return 0;
}
