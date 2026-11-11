#!/bin/zsh
# Build, sign, install and activate the signed host app + "AI Virtual Camera" system extension.
#
# Requirements (see docs/CAMERA_SETUP.md):
#   - Xcode (full app) selected:  sudo xcode-select -s /Applications/Xcode.app
#   - Apple Developer Program team; signed in under Xcode > Settings > Accounts
#   - VCS_TEAM_ID=<your 10-character Team ID>
# Usage:  VCS_TEAM_ID=ABCDE12345 scripts/build_camera.sh [--no-install]
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEAM="${VCS_TEAM_ID:-}"
if ! xcodebuild -version >/dev/null 2>&1; then
  echo "ERROR: Xcode is not selected. Install Xcode, then: sudo xcode-select -s /Applications/Xcode.app"; exit 1
fi
if [[ -z "$TEAM" ]]; then
  echo "ERROR: set VCS_TEAM_ID to your Apple Developer Team ID (Xcode > Settings > Accounts > Team, or developer.apple.com > Membership)"; exit 1
fi
command -v xcodegen >/dev/null || brew install xcodegen
OUT="$ROOT/build/xcode"
rm -rf "$OUT/HostApp" "$OUT/CameraExtension"
mkdir -p "$OUT"
cp -R "$ROOT/native/HostApp" "$ROOT/native/CameraExtension" "$ROOT/native/project.yml" "$OUT/"
(cd "$OUT" && VCS_TEAM_ID="$TEAM" VCS_PROJECT_ROOT="$ROOT" xcodegen generate --spec project.yml)
xcodebuild -project "$OUT/AIVirtualCameraStudio.xcodeproj" -scheme AIVirtualCameraStudio -configuration Release \
  -derivedDataPath "$OUT/dd" -allowProvisioningUpdates DEVELOPMENT_TEAM="$TEAM" build | tail -20
APP="$OUT/dd/Build/Products/Release/AI Virtual Camera Studio.app"
EXT="$APP/Contents/Library/SystemExtensions/com.aivcstudio.app.camera.systemextension"
[[ -d "$EXT" ]] || { echo "ERROR: extension not embedded at $EXT"; exit 1; }
echo "--- signature check"
codesign --verify --deep --strict "$APP" && echo "codesign OK"
codesign -d --entitlements :- "$APP" 2>/dev/null | grep -q system-extension.install && echo "host entitlement OK"
codesign -d --entitlements :- "$EXT" 2>/dev/null | grep -q application-groups && echo "extension entitlement OK"
[[ "$1" == "--no-install" ]] && { echo "Built: $APP"; exit 0; }
DEST="/Applications/AI Virtual Camera Studio.app"
echo "--- installing to $DEST"
rm -rf "$DEST"
ditto "$APP" "$DEST"
echo "--- activating camera extension"
set +e
"$DEST/Contents/MacOS/AIVirtualCameraStudio" --activate-camera
rc=$?
set -e
case $rc in
  0) echo "AI Virtual Camera is active." ;;
  2) echo "ACTION NEEDED: System Settings > General > Login Items & Extensions > Camera Extensions -> enable 'AI Virtual Camera', then run this script again or use the Virtual Camera page." ;;
  3) echo "ACTION NEEDED: restart the Mac to finish installing the camera extension." ;;
  *) echo "Activation failed (exit $rc). See docs/CAMERA_SETUP.md > Troubleshooting."; exit $rc ;;
esac
