// validate/ios_identity_app.mm — iOS Simulator GATE harness for the jitless V8 lane.
//
// THE GATE (#32): does the V8↔Dawn Abseil ODR fire on iOS, and does a SEALED V8
// framework prevent it? There is no CLI on iOS, so this is a tiny app whose
// `main()` runs the SAME identity contract as validate/identity_main.cpp, then
// signals a DETERMINISTIC pass/fail two ways (console scraping alone is not an
// exit-code contract — review addendum point 5):
//   1. a log marker block (PULP_ENGINE_IDENTITY_BEGIN…END) scraped from
//      `simctl launch --console-pty`, and
//   2. a result file written into the app's Documents dir, whose path is printed
//      so the harness can read pass/fail out of band.
//
// It force-loads BOTH collision partners (Skia flat ICU + Dawn flat Abseil, via
// smoke_gpu.cpp compiled with -DFORCE_DAWN_ABSEIL) so V8's sealed-internal Abseil
// and Dawn's flat Abseil are BOTH resident — the exact condition that aborts an
// unsealed build. If V8::Initialize() survives and eval 20+22 == 42 with the
// version matching, the sealed framework prevents the ODR ⇒ iOS lane is viable.
//
// Build shape: jitless (no JIT, no WASM — v8_enable_lite_mode=true in ios_gn_args; that build
// config is the App Store compliance boundary). This gate sets the `--jitless` runtime
// guardrail (v8.dev/docs/cross-compile-ios) before init and MEASURES the jitless posture from
// `typeof WebAssembly === 'undefined'` (the JIT tiers + WASM are compiled out) rather than
// asserting it blindly.

#import <Foundation/Foundation.h>
#include <cstdio>
#include <cstring>
#include <string>

#include <libplatform/libplatform.h>
#include <v8.h>

#ifndef EXPECTED_V8_VERSION
#define EXPECTED_V8_VERSION "UNSET"
#endif

// Defined in smoke_gpu.cpp — references Skia ICU (+ Dawn Abseil when
// FORCE_DAWN_ABSEIL is defined) so their flat copies are resident before V8 runs.
extern "C" int v8builder_force_collision_partners();

namespace {

void write_result(bool pass, const char* detail) {
  @autoreleasepool {
    NSArray* dirs = NSSearchPathForDirectoriesInDomains(
        NSDocumentDirectory, NSUserDomainMask, YES);
    NSString* doc = [dirs firstObject];
    NSString* path = [doc stringByAppendingPathComponent:@"v8_ios_identity_result.txt"];
    NSString* body = [NSString stringWithFormat:@"%s\n%s\n",
                      pass ? "PASS" : "FAIL", detail ? detail : ""];
    [body writeToFile:path atomically:YES encoding:NSUTF8StringEncoding error:nil];
    // Print the path so the harness can also read the file out of band.
    std::printf("PULP_RESULT_FILE=%s\n", [path UTF8String]);
    std::fflush(stdout);
  }
}

// Run the V8 identity contract. Returns true on full pass.
bool run_identity(std::string* version_out, int* answer_out,
                  int* partners_out, bool* wasm_absent_out) {
  // APP STORE / JITLESS GUARDRAIL (https://v8.dev/docs/cross-compile-ios): the iOS framework
  // is BUILT jitless (ios_gn_args: v8_enable_lite_mode=true ⇒ v8_jitless ⇒ no
  // TurboFan/Maglev/Sparkplug/WASM, and "jitless" = no runtime executable/RWX memory) — that
  // BUILD CONFIG is the App Store compliance boundary. We also set --jitless at runtime as a
  // defensive guardrail/regression-tripwire (redundant on a correct lite build, not
  // load-bearing — per V8-source review). (--expose_gc is test-only and unnecessary; omitted
  // to keep the gate on the exact production posture.) Must precede InitializePlatform.
  v8::V8::SetFlagsFromString("--jitless");

  // Pull Dawn/Skia symbols in first so their Abseil/ICU/zlib are resident before V8.
  volatile int partners = v8builder_force_collision_partners();
  *partners_out = partners;

  // The sealed framework is built with v8_use_external_startup_data=false (the
  // snapshot is EMBEDDED in the binary) and i18n OFF (no ICU). Do NOT call
  // InitializeExternalStartupData / InitializeICUDefaultLocation: pointing V8 at a
  // non-existent external snapshot blob makes Snapshot::Initialize fail the
  // SerializedData magic-number check (a deserializer abort, NOT the Abseil ODR).
  // With the embedded snapshot, Isolate::New finds the blob inside the image.
  std::unique_ptr<v8::Platform> platform = v8::platform::NewDefaultPlatform();
  v8::V8::InitializePlatform(platform.get());
  v8::V8::Initialize();   // aborts here on an UNSEALED build (Abseil ODR)

  *version_out = v8::V8::GetVersion();

  int answer = -1;
  bool wasm_absent = false;
  {
    v8::Isolate::CreateParams params;
    params.array_buffer_allocator =
        v8::ArrayBuffer::Allocator::NewDefaultAllocator();
    v8::Isolate* isolate = v8::Isolate::New(params);
    {
      v8::Isolate::Scope iscope(isolate);
      v8::HandleScope hscope(isolate);
      v8::Local<v8::Context> ctx = v8::Context::New(isolate);
      v8::Context::Scope cscope(ctx);

      // 1) eval 20+22 — the language runs under the interpreter.
      {
        v8::Local<v8::String> src =
            v8::String::NewFromUtf8Literal(isolate, "20+22");
        v8::Local<v8::Script> script =
            v8::Script::Compile(ctx, src).ToLocalChecked();
        answer = script->Run(ctx).ToLocalChecked()->Int32Value(ctx).FromJust();
      }
      // 2) prove the JITLESS shape: WebAssembly global must be absent on iphoneos.
      {
        v8::Local<v8::String> src = v8::String::NewFromUtf8Literal(
            isolate, "typeof WebAssembly === 'undefined'");
        v8::Local<v8::Script> script =
            v8::Script::Compile(ctx, src).ToLocalChecked();
        wasm_absent = script->Run(ctx).ToLocalChecked()->BooleanValue(isolate);
      }
    }
    isolate->Dispose();
    delete params.array_buffer_allocator;
  }
  *answer_out = answer;
  *wasm_absent_out = wasm_absent;

  return answer == 42 && *version_out == std::string(EXPECTED_V8_VERSION);
}

}  // namespace

int main(int argc, char* argv[]) {
  std::string version;
  int answer = -1, partners = 0;
  bool wasm_absent = false;
  bool ok = run_identity(&version, &answer, &partners, &wasm_absent);

  // jitless is MEASURED, not hardcoded: the build is lite-mode (the JIT tiers + WASM are
  // compiled out ⇒ no `WebAssembly` global ⇒ wasm_absent), and we additionally ran under the
  // --jitless runtime guardrail. wasm_absent is the build-shape witness for the no-JIT posture.
  bool jitless = wasm_absent;
  std::printf("PULP_ENGINE_IDENTITY_BEGIN\n"
              "engine_type=v8\n"
              "runtime_version=%s\n"
              "js_eval_20_plus_22=%d\n"
              "wasm_absent=%d\n"
              "collision_partners=%d\n"
              "v8_flags=--jitless\n"
              "jitless=%d\n"
              "PULP_ENGINE_IDENTITY_END\n",
              version.c_str(), answer, wasm_absent ? 1 : 0, partners, jitless ? 1 : 0);
  std::fflush(stdout);

  bool full_pass = ok && wasm_absent;
  char detail[320];
  std::snprintf(detail, sizeof(detail),
                "v8=%s eval=%d wasm_absent=%d partners=%d expected=%s",
                version.c_str(), answer, wasm_absent ? 1 : 0,
                partners, EXPECTED_V8_VERSION);
  write_result(full_pass, detail);

  if (full_pass) {
    std::printf("PULP_IOS_GATE=PASS\n");
  } else {
    std::printf("PULP_IOS_GATE=FAIL (%s)\n", detail);
  }
  std::fflush(stdout);

  // Exit promptly so the harness can tear down the Sim (audio-etiquette teardown).
  return full_pass ? 0 : 1;
}
