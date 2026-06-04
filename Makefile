# Makefile — local helper targets for v8-builder (mirrors skia-builder ergonomics).
# STATUS: skeleton — most targets delegate to build-v8.py (not yet implemented).

V8_BUILDER = python3 build-v8.py

.PHONY: v8-mac v8-linux v8-win validate validate-mac clean help

help:
	@echo "v8-builder targets (Phase 0 scaffold):"
	@echo "  make v8-mac        # build sealed shared V8 for macOS (arm64)        [Phase 2]"
	@echo "  make v8-linux      # build sealed shared V8 for Linux (x64)          [Phase 1]"
	@echo "  make v8-win        # build sealed DLL V8 for Windows (x64)           [Phase 3]"
	@echo "  make validate-mac  # link V8 + real Skia, run identity-anchored harness"
	@echo "  make clean         # remove build/"

v8-mac:
	ulimit -n 2048 && $(V8_BUILDER) mac -archs arm64

v8-linux:
	$(V8_BUILDER) linux -archs x64

v8-win:
	$(V8_BUILDER) win -archs x64

# Validation harness (built on pulp's threejs-native-demo). Wired up in Phase 0.2.
validate-mac:
	@echo "validate-mac: see validate/run_validation.cmake (Phase 0.2)"; exit 2

clean:
	rm -rf build
