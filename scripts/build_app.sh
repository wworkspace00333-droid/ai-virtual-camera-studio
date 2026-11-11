#!/bin/zsh
# Build the self-contained application bundle and (optionally) a DMG.
#
#   scripts/build_app.sh            -> dist/AI Virtual Camera Studio.app   (ad-hoc signed)
#   scripts/build_app.sh --dmg      -> also dist/AI-Virtual-Camera-Studio-<version>.dmg
#
# Layout of the bundle:
#   Contents/MacOS/AIVirtualCameraStudio   Swift launcher (native/HostApp/main.swift) = CFBundleExecutable.
#                                          Owns camera permission, activates the camera extension.
#   Contents/MacOS/vcstudio                frozen Python payload (PyInstaller), started by the launcher
#   Contents/Frameworks/                   Python runtime, Qt, PyObjC, OpenCV, libVCSNative/libVCSShm
#   Contents/Resources/                    models, sample data, OBS plugin, developer scripts, icon
# Signing: ad-hoc by default. With VCS_SIGN_IDENTITY="Developer ID Application: ..." the bundle is signed
# with the hardened runtime for notarization (Stage 13, requires an Apple Developer account).
# The CoreMediaIO camera extension is embedded only by the Xcode build (scripts/build_camera.sh).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
VERSION="$($PY -c 'import vcstudio; print(vcstudio.__version__)')"
export VCS_VERSION="$VERSION"
APP="$ROOT/dist/AI Virtual Camera Studio.app"
SDK="${VCS_SDK:-$(ls -d /Library/Developer/CommandLineTools/SDKs/MacOSX26.*.sdk 2>/dev/null | tail -1)}"
IDENTITY="${VCS_SIGN_IDENTITY:--}"

echo "== native libraries"
[[ -f build/native/libVCSNative.dylib && -f build/native/libVCSShm.dylib ]] || scripts/build_native.sh
[[ -d build/obs-plugin/vcstudio-obs.plugin ]] || echo "   (OBS plugin not built; bundle will not offer plugin install — scripts/build_obs_plugin.sh)"
[[ -f packaging/AppIcon.icns ]] || $PY packaging/make_icon.py

echo "== PyInstaller payload (version $VERSION)"
$PY -m PyInstaller --noconfirm --clean --log-level WARN \
    --distpath "$ROOT/dist" --workpath "$ROOT/build/pyinstaller" packaging/vcstudio.spec

if [[ -d build/obs-plugin/vcstudio-obs.plugin ]]; then
    mkdir -p "$APP/Contents/Resources/obs-plugin"
    ditto build/obs-plugin/vcstudio-obs.plugin "$APP/Contents/Resources/obs-plugin/vcstudio-obs.plugin"
fi

echo "== Swift launcher"
swiftc -sdk "$SDK" -O -target arm64-apple-macos13.0 -o "$APP/Contents/MacOS/AIVirtualCameraStudio" \
    native/HostApp/main.swift
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable AIVirtualCameraStudio" "$APP/Contents/Info.plist"

echo "== codesign ($IDENTITY)"
SIGN_OPTS=(--force --sign "$IDENTITY" --timestamp=none)
if [[ "$IDENTITY" != "-" ]]; then
    SIGN_OPTS=(--force --sign "$IDENTITY" --timestamp --options runtime \
               --entitlements "$ROOT/packaging/payload.entitlements")
fi
# inner Mach-O files first (dylibs, .so, OBS plugin), then the executables, then the bundle
find "$APP/Contents/Frameworks" "$APP/Contents/Resources" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 |
    xargs -0 codesign "${SIGN_OPTS[@]}" 2>/dev/null
find "$APP/Contents/Frameworks" -type d -name "*.framework" -prune -print0 | xargs -0 -n 20 codesign "${SIGN_OPTS[@]}" 2>/dev/null || true
if [[ -d "$APP/Contents/Resources/obs-plugin/vcstudio-obs.plugin" ]]; then
    codesign "${SIGN_OPTS[@]}" "$APP/Contents/Resources/obs-plugin/vcstudio-obs.plugin"
fi
codesign "${SIGN_OPTS[@]}" "$APP/Contents/MacOS/vcstudio"
codesign "${SIGN_OPTS[@]}" "$APP/Contents/MacOS/AIVirtualCameraStudio"
codesign "${SIGN_OPTS[@]}" "$APP"
codesign --verify --deep --strict "$APP"
echo "   signature OK"
du -sh "$APP"

if [[ "$1" == "--dmg" ]]; then
    echo "== DMG"
    DMG="$ROOT/dist/AI-Virtual-Camera-Studio-$VERSION.dmg"
    STAGE="$ROOT/build/dmg-stage"
    rm -rf "$STAGE" "$DMG"; mkdir -p "$STAGE"
    ditto "$APP" "$STAGE/AI Virtual Camera Studio.app"
    ln -s /Applications "$STAGE/Applications"
    hdiutil create -volname "AI Virtual Camera Studio" -srcfolder "$STAGE" -fs APFS -format ULFO -ov "$DMG" >/dev/null
    [[ "$IDENTITY" != "-" ]] && codesign --force --sign "$IDENTITY" --timestamp "$DMG"
    rm -rf "$STAGE"
    du -h "$DMG"
fi
echo "Built: $APP"
