#!/usr/bin/env bash
# build-win.sh — Windows helper wrapper around build-v8.py (mirrors skia-builder).
# On Windows, V8/depot_tools want a Python launcher and LLVM. The flagship Windows
# artifact is a DLL with an export-table seal (NOT a static .lib — see proposal §6b).
#
# STATUS: skeleton. Real invocation lands in Phase 3.
set -euo pipefail

echo "[build-win.sh] skeleton — Phase 3 will:"
echo "  - ensure LLVM (C:\\Program Files\\LLVM) + ninja + depot_tools"
echo "  - py -3 build-v8.py win -archs x64   (DLL, Intl on, /MT, export-table seal)"
exit 2
