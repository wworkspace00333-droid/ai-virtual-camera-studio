# Packaging (Stage 13)

## What exists today (13a, no Apple Developer account needed)

```bash
scripts/build_app.sh          # dist/AI Virtual Camera Studio.app   (~370 MB, ad-hoc signed)
scripts/build_app.sh --dmg    # also dist/AI-Virtual-Camera-Studio-<version>.dmg   (~205 MB, ULFO/LZFSE)
```

The app is self-contained. It needs no `.venv`, no repository and no Homebrew. The only optional
external tool is `ffmpeg`, and only for MKV/WebM replacement videos.

### Bundle layout

| Path | Contents |
|---|---|
| `Contents/MacOS/AIVirtualCameraStudio` | Swift launcher ([native/HostApp/main.swift](../native/HostApp/main.swift)), the `CFBundleExecutable`. It owns camera permission (`NSCameraUsageDescription`), runs `--activate-camera` / `--deactivate-camera`, and otherwise starts the payload. |
| `Contents/MacOS/vcstudio` | Frozen Python payload (PyInstaller 6.22, Python 3.12 arm64). |
| `Contents/Frameworks/` | Python runtime, Qt 6, PyObjC, OpenCV, NumPy/SciPy; also `native/libVCSNative.dylib` + `native/libVCSShm.dylib`; `models/*.mlpackage`; `sample_data/`. |
| `Contents/Resources/` | Icon, `obs-plugin/vcstudio-obs.plugin` (installable from the Input page), developer `scripts/` (run with `--run-script`). |

All runtime code finds these files through [vcstudio/core/resources.py](../vcstudio/core/resources.py).
`tests/test_packaging.py` fails if any module goes back to repository-relative paths.

### Verify an installation

```bash
"/Applications/AI Virtual Camera Studio.app/Contents/MacOS/AIVirtualCameraStudio" --self-check [report.json]
```

The self-check loads and runs each Core ML model on the sample image and runs Vision face detection.
It also creates a Metal context, loads the shared-memory reader, and checks for the OBS plugin, the
sample data and ffmpeg. The exit code is 0 only when every required check passes.

Measured on 2026-11-10 (MacBook Pro M5, 16 GB):

| Check | Result |
|---|---|
| `--self-check` from the mounted DMG, clean environment (`env -i`, no venv) | 12/12 pass. yolo11n 3.1–3.7 ms, yolo11s 5.3–6.2 ms, yolo11m 10.4–11.7 ms, yolo11n-seg 5.3 ms, yolo11m-seg 14.3–15.5 ms per frame |
| `--capture-selftest` launched with `open` (TCC prompt for `com.aivcstudio.app`) | Permission granted; OBS Virtual Camera 1080p at 59.8 fps average (min 59); AI 20 fps, 8.1 ms average |
| GUI `--autostart-capture --snapshot` | All pages render; the pipeline runs |
| `codesign --verify --deep --strict` on the app and on the app inside the DMG | Pass |

Rebuilt and re-measured on 2026-11-12 (Mac mini M2 Pro (Mac14,12), 16 GB, macOS 27.2.1):

| Check | Result |
|---|---|
| `--self-check` from the mounted DMG, clean environment (`env -i`, no venv) | 11/12 pass (ffmpeg, optional, not installed). yolo11n 5.6 ms, yolo11s 6.2 ms, yolo11m 13.4 ms, yolo11n-seg 8.6 ms, yolo11m-seg 18.8 ms per frame; Metal on Apple M2 Pro; OBS sharing live; plugin installed |
| `VCS_LIVE=1 pytest tests/test_packaging.py` | 5/5 pass |
| `codesign --verify --deep --strict` on the app inside the DMG | Pass |
| Sizes | app 371 MB, DMG 205 MB |

### Gatekeeper with an ad-hoc signature

An ad-hoc signed app copied from a DMG downloaded from the Internet is quarantined. On first launch, open
it with right-click → **Open**, or allow it in System Settings → Privacy & Security. A DMG built on
the same Mac is not quarantined. Only a Developer ID signature plus notarization (below) removes this
step.

## Remaining (13b, needs Xcode + Apple Developer Program)

1. **Embed the camera extension.** Build the host app and extension with Xcode
   (`scripts/build_camera.sh`, [CAMERA_SETUP.md](CAMERA_SETUP.md)), then merge them with this payload:
   copy `Contents/Library/SystemExtensions/com.aivcstudio.app.camera.systemextension` into the bundle.
   Use the Xcode-signed launcher with the `com.apple.developer.system-extension.install` entitlement and
   its provisioning profile (`Contents/embedded.provisionprofile`).
2. **Developer ID signing:**
   `VCS_SIGN_IDENTITY="Developer ID Application: <Name> (<TEAMID>)" scripts/build_app.sh --dmg`
   signs every Mach-O inside-out with the hardened runtime and
   [packaging/payload.entitlements](../packaging/payload.entitlements). The Python runtime needs
   `allow-unsigned-executable-memory` and `disable-library-validation`.
3. **Notarization:** `xcrun notarytool submit dist/*.dmg --keychain-profile <profile> --wait`,
   then `xcrun stapler staple` on the DMG.
4. Rerun the E2E and soak tests from the installed app through "AI Virtual Camera".
