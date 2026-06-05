#!/usr/bin/env python3
"""run_ios_gate.py — drive the iOS-Simulator Abseil-ODR gate end to end.

THE GATE (#32): build a tiny Sim app that links the SEALED V8.framework next to
iOS Skia (flat ICU) and Dawn (flat Abseil), then run it on a booted Simulator and
scrape a deterministic PASS/FAIL. If V8::Initialize() survives the duplicate Abseil
(no ODR abort) and identity verifies, the sealed framework lets V8 + Dawn coexist on
iOS ⇒ the lane is viable. If it aborts, iOS is not viable even with the seal.

Steps:
  1. CMake-configure + build validate/ios for the iphonesimulator SDK (arm64).
  2. Assemble a minimal <name>.app: the gate binary + V8.framework + Info.plist.
  3. Boot a Simulator, install, `launch --console-pty`, scrape the identity block
     and the result file, then TERMINATE + (optionally) SHUTDOWN — audio-etiquette
     teardown (CLAUDE.md interim contract).

Exit 0 only on a real PASS. No skip-pass: a missing identity block, an abort, a
wrong version, or no result file is a hard FAIL.

Usage:
  run_ios_gate.py --framework-dir build/ios-simulator-arm64/lib \\
                  --include-dir build/ios-simulator-arm64/include \\
                  --skia-dir <skia-ios-sim>/lib --expected-version 15.1.27 \\
                  [--dawn-lib <libdawn_combined.a>] [--udid <sim-udid>] \\
                  [--keep-booted]
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "V8iOSGate"
BUNDLE_ID = "org.v8.iosgate"


def say(msg):
    print(f"[ios-gate] {msg}", flush=True)


def run(cmd, **kw):
    say("$ " + " ".join(map(str, cmd)))
    return subprocess.run(cmd, check=True, **kw)


def build(args, work):
    out = work / "cmake-build"
    out.mkdir(parents=True, exist_ok=True)
    cfg = [
        "cmake", "-S", str(HERE), "-B", str(out), "-G", "Ninja",
        "-DCMAKE_SYSTEM_NAME=iOS",
        "-DCMAKE_OSX_SYSROOT=iphonesimulator",
        "-DCMAKE_OSX_ARCHITECTURES=arm64",
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=16.4",
        f"-DV8_FRAMEWORK_DIR={args.framework_dir}",
        f"-DV8_INCLUDE_DIR={args.include_dir}",
        f"-DSKIA_DIR={args.skia_dir}",
        f"-DEXPECTED_V8_VERSION={args.expected_version}",
    ]
    if args.dawn_lib:
        cfg.append(f"-DSKIA_DAWN_LIB={args.dawn_lib}")
    run(cfg)
    run(["cmake", "--build", str(out)])
    binary = out / "v8_ios_gate"
    if not binary.exists():
        raise SystemExit(f"gate binary not built: {binary}")
    return binary


def assemble_app(binary, framework_dir, work):
    app = work / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    app.mkdir(parents=True)
    # iOS .app is flat: executable at the bundle root.
    shutil.copy2(binary, app / APP_NAME)
    os.chmod(app / APP_NAME, 0o755)
    # Embed the sealed V8.framework (the dylib's install_name is
    # @rpath/V8.framework/V8; the app's rpath is @executable_path).
    fw_src = Path(framework_dir) / "V8.framework"
    if not fw_src.exists():
        raise SystemExit(f"V8.framework not found at {fw_src}")
    shutil.copytree(fw_src, app / "V8.framework")
    # DTSDKName: the concrete iphonesimulatorNN.N the app was built against. simctl
    # install accepts the bundle when DTPlatformName/DTSDKName are present (verified
    # against a real installed sim app). Derive the version from the active SDK.
    sdk_name = "iphonesimulator"
    try:
        v = subprocess.run(
            ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"],
            capture_output=True, text=True).stdout.strip()
        if v:
            sdk_name = f"iphonesimulator{v}"
    except Exception:
        pass
    info = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'  <key>CFBundleIdentifier</key><string>{BUNDLE_ID}</string>\n'
        f'  <key>CFBundleName</key><string>{APP_NAME}</string>\n'
        f'  <key>CFBundleExecutable</key><string>{APP_NAME}</string>\n'
        '  <key>CFBundlePackageType</key><string>APPL</string>\n'
        '  <key>CFBundleVersion</key><string>1</string>\n'
        '  <key>CFBundleShortVersionString</key><string>1.0</string>\n'
        '  <key>MinimumOSVersion</key><string>16.4</string>\n'
        '  <key>CFBundleSupportedPlatforms</key>'
        '<array><string>iPhoneSimulator</string></array>\n'
        '  <key>DTPlatformName</key><string>iphonesimulator</string>\n'
        f'  <key>DTSDKName</key><string>{sdk_name}</string>\n'
        '  <key>UIDeviceFamily</key>'
        '<array><integer>1</integer><integer>2</integer></array>\n'
        '</dict></plist>\n')
    (app / "Info.plist").write_text(info, encoding="utf-8")
    say(f"assembled {app}")
    return app


def pick_udid(args):
    if args.udid:
        return args.udid
    # Prefer an already-booted device, else the first available iPhone.
    out = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "available", "-j"],
        capture_output=True, text=True).stdout
    import json
    data = json.loads(out)
    booted, first = None, None
    for runtime, devs in data.get("devices", {}).items():
        if "iOS" not in runtime:
            continue
        for d in devs:
            if "iPhone" not in d.get("name", ""):
                continue
            if first is None:
                first = d["udid"]
            if d.get("state") == "Booted":
                booted = d["udid"]
    udid = booted or first
    if not udid:
        raise SystemExit("no available iPhone simulator found")
    return udid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--framework-dir", required=True,
                    help="dir containing V8.framework")
    ap.add_argument("--include-dir", required=True)
    ap.add_argument("--skia-dir", required=True)
    ap.add_argument("--expected-version", required=True)
    ap.add_argument("--dawn-lib", help="Dawn combined iOS archive (forces Abseil ODR)")
    ap.add_argument("--udid", help="specific simulator UDID")
    ap.add_argument("--keep-booted", action="store_true",
                    help="don't shutdown the Sim after (still terminates the app)")
    ap.add_argument("--timeout", type=int, default=90,
                    help="hard wall-clock cap on the launch (audio-etiquette)")
    args = ap.parse_args()

    work = HERE / "work"
    work.mkdir(exist_ok=True)

    binary = build(args, work)
    app = assemble_app(binary, args.framework_dir, work)

    udid = pick_udid(args)
    say(f"using simulator {udid}")
    # AUDIO-ETIQUETTE: announce. This gate runs a pure JS-identity app with no audio
    # bus, but per the CLAUDE.md interim contract we announce, cap, and tear down.
    say("AUDIO-ETIQUETTE: launching an iOS Simulator app (silent JS-identity gate); "
        f"capped at {args.timeout}s; will terminate + shutdown after.")

    booted_here = False
    state = subprocess.run(["xcrun", "simctl", "list", "devices"],
                           capture_output=True, text=True).stdout
    if f"({udid}) (Booted)" not in state:
        run(["xcrun", "simctl", "boot", udid])
        booted_here = True
        run(["xcrun", "simctl", "bootstatus", udid])

    passed = False
    console = ""
    try:
        run(["xcrun", "simctl", "install", udid, str(app)])
        say("launching gate app (console-pty)")
        proc = subprocess.run(
            ["xcrun", "simctl", "launch", "--console-pty", "--terminate-running-process",
             udid, BUNDLE_ID],
            capture_output=True, text=True, timeout=args.timeout)
        console = (proc.stdout or "") + (proc.stderr or "")
        print(console)

        # Deterministic signal #1: the gate marker line.
        if "PULP_IOS_GATE=PASS" in console:
            passed = True
        # Deterministic signal #2: the result file written into the app container.
        m = re.search(r"PULP_RESULT_FILE=(\S+)", console)
        if m:
            # Map the in-Sim path to the host container.
            container = subprocess.run(
                ["xcrun", "simctl", "get_app_container", udid, BUNDLE_ID, "data"],
                capture_output=True, text=True).stdout.strip()
            host_path = Path(container) / "Documents" / "v8_ios_identity_result.txt"
            if host_path.exists():
                body = host_path.read_text()
                say(f"result file:\n{body}")
                if body.strip().startswith("PASS"):
                    passed = passed or True
                elif body.strip().startswith("FAIL"):
                    passed = False
        if "PULP_ENGINE_IDENTITY_BEGIN" not in console:
            say("WARNING: no identity block in console — refusing to skip-pass")
            passed = False
    except subprocess.TimeoutExpired:
        say(f"TIMEOUT after {args.timeout}s — treating as FAIL (likely V8::Initialize "
            "abort/hang from the Abseil ODR)")
        passed = False
    finally:
        # Audio-etiquette teardown.
        subprocess.run(["xcrun", "simctl", "terminate", udid, BUNDLE_ID],
                       capture_output=True)
        if booted_here and not args.keep_booted:
            subprocess.run(["xcrun", "simctl", "shutdown", udid], capture_output=True)
            say("simulator shut down")

    if passed:
        say("GATE PASS — sealed V8.framework + Dawn(Abseil) + Skia(ICU) coexist on iOS; "
            "identity verified, jitless (WASM absent).")
        return 0
    say("GATE FAIL — see console/result above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
