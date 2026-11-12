#!/bin/zsh
# Local signing of AI Virtual Camera Studio.app for use on this Mac: ad-hoc ("-"), no Apple account,
# no certificate, no notarization. Signs inside-out (deepest code first), never with --deep.
#
#   scripts/sign_local.sh "<path>/AI Virtual Camera Studio.app" [--no-runtime]
#
# Default: Hardened Runtime on, with the three non-restricted exceptions in packaging/local.entitlements on the
# two executables only (libraries and frameworks get no entitlements). --no-runtime signs plain ad-hoc.
# Note: every new ad-hoc signature changes the code hash, so macOS may ask for camera / screen recording again
# after an update (TCC keys ad-hoc apps by hash).
set -e
set -o pipefail
APP="${1:?usage: sign_local.sh <app> [--no-runtime]}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENT="$ROOT/packaging/local.entitlements"
RUNTIME=(--options runtime)
[[ "$2" == "--no-runtime" ]] && RUNTIME=()
CS=(codesign --force --sign - --timestamp=none "${RUNTIME[@]}")
[[ -d "$APP/Contents" ]] || { echo "not an app bundle: $APP"; exit 1; }

echo "== 1. loose Mach-O files (libraries, extension modules), deepest first"
n=0
find "$APP/Contents" -type f ! -path "*/Contents/MacOS/*" ! -path "*.framework/*" ! -path "*.plugin/*" -print0 |
  xargs -0 file | grep "Mach-O" | cut -d: -f1 |
  awk '{print gsub("/","/") "\t" $0}' | sort -rn | cut -f2- |
  while IFS= read -r f; do "${CS[@]}" "$f" 2>/dev/null; n=$((n+1)); done
echo "   signed"

echo "== 2. frameworks (their own Mach-O first, then the bundle), deepest first"
find "$APP/Contents" -type d -name "*.framework" ! -path "*/Resources/*" -print |
  awk '{print gsub("/","/") "\t" $0}' | sort -rn | cut -f2- |
  while IFS= read -r fw; do
    find "$fw" -type f -print0 | xargs -0 file | grep "Mach-O" | cut -d: -f1 |
      while IFS= read -r f; do "${CS[@]}" "$f" 2>/dev/null; done
    "${CS[@]}" "$fw"
  done

echo "== 3. plug-in bundles"
find "$APP/Contents" -type d -name "*.plugin" -print | while IFS= read -r p; do "${CS[@]}" "$p"; done

echo "== 4. executables with entitlements, then the app"
for exe in "$APP/Contents/MacOS/vcstudio" "$APP/Contents/MacOS/AIVirtualCameraStudio"; do
  [[ -f "$exe" ]] && "${CS[@]}" --entitlements "$ENT" --generate-entitlement-der "$exe"
done
"${CS[@]}" --entitlements "$ENT" --generate-entitlement-der "$APP"

echo "== verify"
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | tail -2
codesign -dv "$APP" 2>&1 | grep -E "^(Identifier|CodeDirectory|Signature)"
echo "-- entitlements of the app:"
codesign -d --entitlements - --xml "$APP" 2>/dev/null | plutil -p - 2>/dev/null || codesign -d --entitlements - "$APP"
