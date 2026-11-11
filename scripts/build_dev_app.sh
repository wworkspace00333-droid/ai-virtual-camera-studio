#!/bin/zsh
# Build build/AI Virtual Camera Studio Dev.app: an ad-hoc signed launcher bundle that declares
# camera usage and runs the app from this source tree (see native/DevLauncher/main.swift).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/AI Virtual Camera Studio Dev.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
SDK="${VCS_SDK:-$(ls -d /Library/Developer/CommandLineTools/SDKs/MacOSX26.*.sdk 2>/dev/null | tail -1)}"
swiftc -sdk "$SDK" -O -target arm64-apple-macos13.0 -o "$APP/Contents/MacOS/vcstudio-dev" "$ROOT/native/DevLauncher/main.swift"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.aivcstudio.app.dev</string>
  <key>CFBundleName</key><string>AI Virtual Camera Studio Dev</string>
  <key>CFBundleDisplayName</key><string>AI Virtual Camera Studio Dev</string>
  <key>CFBundleExecutable</key><string>vcstudio-dev</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>NSCameraUsageDescription</key><string>AI Virtual Camera Studio reads video from OBS Virtual Camera (or another camera you select) to process it locally on this Mac.</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>VCSProjectRoot</key><string>$ROOT</string>
</dict></plist>
PLIST
codesign --force --sign - --identifier com.aivcstudio.app.dev "$APP"
echo "Built: $APP"
