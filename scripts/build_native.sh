#!/bin/zsh
# Build build/native/libVCSNative.dylib (Swift + runtime-compiled Metal shaders; no Xcode needed).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FINAL="$ROOT/build/native"
OUT="$(mktemp -d "$ROOT/build/native-tmp.XXXXXX")"   # build aside, then rename into place atomically:
trap 'rm -rf "$OUT"' EXIT                             # running processes keep their mapped copy (no crash)
mkdir -p "$FINAL"
SDK="${VCS_SDK:-$(ls -d /Library/Developer/CommandLineTools/SDKs/MacOSX26.*.sdk 2>/dev/null | tail -1)}"
[[ -n "$SDK" ]] || SDK="$(xcrun --show-sdk-path)"
swiftc -sdk "$SDK" -O -whole-module-optimization -emit-library -module-name VCSNative \
  -target arm64-apple-macos13.0 -o "$OUT/libVCSNative.dylib" \
  "$ROOT"/native/VCSNative/*.swift \
  -framework Metal -framework MetalPerformanceShaders -framework CoreVideo -framework CoreMedia -framework CoreMediaIO -framework Carbon -framework Foundation
codesign --force --sign - "$OUT/libVCSNative.dylib"
# libVCSShm: OBS shared-memory frame reader (C, C11 atomics; same protocol header as the OBS plugin)
clang -isysroot "$SDK" -arch arm64 -mmacosx-version-min=13.0 -O2 -Wall -std=c11 -dynamiclib \
  -o "$OUT/libVCSShm.dylib" "$ROOT/native/shm-reader/vcs_shm_reader.c" -framework CoreVideo
codesign --force --sign - "$OUT/libVCSShm.dylib"
for f in libVCSNative.dylib libVCSShm.dylib; do mv -f "$OUT/$f" "$FINAL/$f"; done
echo "Built: $FINAL/libVCSNative.dylib, $FINAL/libVCSShm.dylib"
