#!/bin/zsh
# Build (and optionally install) the vcstudio-obs OBS plugin against the installed OBS.app.
#   scripts/build_obs_plugin.sh            -> build/obs-plugin/vcstudio-obs.plugin
#   scripts/build_obs_plugin.sh --install  -> also copies it to ~/Library/Application Support/obs-studio/plugins/
# Headers: obs-studio sources at the tag matching the installed OBS (fetched sparsely into build/obs-sdk).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OBS_APP="${OBS_APP:-/Applications/OBS.app}"
VER="$(defaults read "$OBS_APP/Contents/Info.plist" CFBundleShortVersionString)"
SDK_DIR="$ROOT/build/obs-sdk/obs-studio"
if [[ ! -f "$SDK_DIR/libobs/obs.h" ]] || [[ "$(cat "$ROOT/build/obs-sdk/VERSION" 2>/dev/null)" != "$VER" ]]; then
  rm -rf "$ROOT/build/obs-sdk"; mkdir -p "$ROOT/build/obs-sdk"
  git clone -q --depth 1 --branch "$VER" --filter=blob:none --sparse https://github.com/obsproject/obs-studio.git "$SDK_DIR"
  (cd "$SDK_DIR" && git sparse-checkout set libobs frontend/api)
  echo "$VER" > "$ROOT/build/obs-sdk/VERSION"
fi
# libobs headers include SIMDe (header-only); OBS gets it from obs-deps, we use Homebrew's.
SIMDE_INC="$(brew --prefix simde 2>/dev/null)/include"
[[ -f "$SIMDE_INC/simde/x86/sse2.h" ]] || { brew install simde; SIMDE_INC="$(brew --prefix simde)/include"; }
GEN="$ROOT/build/obs-sdk/generated"; mkdir -p "$GEN"
printf '#pragma once\n#define OBS_RELEASE_CANDIDATE 0\n#define OBS_BETA 0\n' > "$GEN/obsconfig.h"
OUT="$ROOT/build/obs-plugin/vcstudio-obs.plugin"
rm -rf "$OUT"; mkdir -p "$OUT/Contents/MacOS"
SDK="${VCS_SDK:-$(ls -d /Library/Developer/CommandLineTools/SDKs/MacOSX26.*.sdk 2>/dev/null | tail -1)}"
# fall back to whatever SDK the selected toolchain provides (other Macs may not have the 26.x SDK)
[[ -n "$SDK" ]] || SDK="$(xcrun --sdk macosx --show-sdk-path)"
clang -isysroot "$SDK" -arch arm64 -mmacosx-version-min=13.0 -O2 -Wall -Wno-unused-parameter -std=c11 -bundle \
  -I"$GEN" -I"$SIMDE_INC" -I"$SDK_DIR/libobs" -I"$SDK_DIR/frontend/api" -I"$ROOT/native/obs-plugin" \
  -F"$OBS_APP/Contents/Frameworks" -framework libobs \
  "$OBS_APP/Contents/Frameworks/obs-frontend-api.dylib" \
  -Wl,-rpath,@executable_path/../Frameworks \
  -o "$OUT/Contents/MacOS/vcstudio-obs" "$ROOT/native/obs-plugin/vcstudio-obs.c"
cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.aivcstudio.obs-plugin</string>
  <key>CFBundleName</key><string>vcstudio-obs</string>
  <key>CFBundleExecutable</key><string>vcstudio-obs</string>
  <key>CFBundlePackageType</key><string>BNDL</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PLIST
codesign --force --sign - "$OUT"
echo "Built: $OUT (OBS $VER)"
if [[ "$1" == "--install" ]]; then
  DEST="$HOME/Library/Application Support/obs-studio/plugins"
  mkdir -p "$DEST"; rm -rf "$DEST/vcstudio-obs.plugin"; cp -R "$OUT" "$DEST/"
  echo "Installed: $DEST/vcstudio-obs.plugin (restart OBS; Tools > AI Virtual Camera Studio: share frames)"
fi
