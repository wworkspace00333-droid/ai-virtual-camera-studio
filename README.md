# AI Virtual Camera Studio

A native macOS (Apple Silicon) studio app. It takes live video from OBS, detects and tracks objects on-device with Core ML / Vision, replaces or overlays regions with images or video using Metal, and publishes the result as a system camera called **AI Virtual Camera** (a CoreMediaIO Camera Extension).

All processing runs locally. The app contains no network code.

> **Status: Stages 1–8 and 10–12 are complete: capture (OBS Virtual Camera and the direct OBS plugin), on-device AI detection and tracking, the ROI editor, image and video replacement, the Metal compositor, the real-time pipeline with watchdogs, M5 optimisation, and the 2-hour soak. Stage 13a is also complete: a self-contained `.app` and DMG. Stage 9 (virtual camera) is code-complete. Stage 9 and Stage 13b (embedded extension, Developer ID signing, notarization) wait on Xcode and an Apple Developer team.** See [docs/ROADMAP.md](docs/ROADMAP.md).

## Supported Macs

Any Apple Silicon Mac (M1 through M5, any memory size) on macOS 13 or later. Everything is built for `arm64-apple-macos13.0`; Intel Macs are not supported.
Nothing is tuned to one chip: Maximum Quality picks its model from timings measured on the running Mac, and the memory warning and load-shedding thresholds scale with installed RAM (Settings > Memory warning). The published benchmarks were measured on a MacBook Pro M5 with 16 GB; other machines will reach different numbers.

## Quick start (development)

```bash
scripts/setup_dev.sh                         # creates .venv (Python 3.12, arm64) and prints an environment report
scripts/build_native.sh                      # builds build/native/libVCSNative.dylib (Metal compositor)
scripts/build_dev_app.sh                     # builds build/AI Virtual Camera Studio Dev.app (needed for camera access)
open "build/AI Virtual Camera Studio Dev.app"
.venv/bin/python -m pytest -q tests          # unit/UI tests (synthetic source)
VCS_LIVE=1 .venv/bin/python -m pytest -q tests/test_capture_live.py   # live OBS Virtual Camera capture
```

Camera permission belongs to the app bundle. The first time capture starts, macOS asks whether "AI Virtual Camera Studio Dev" may use the camera. If you denied it, enable it in **System Settings → Privacy & Security → Camera**. `scripts/run.sh` (plain Python) works for everything except camera capture.

User data is stored in `~/Library/Application Support/AIVirtualCameraStudio/`: `settings.json`, `profiles/`, `studio.db`, `logs/`. Set `VCSTUDIO_HOME=/some/dir` to use another location (the tests do this).

## What works now (Stage 1)

- A dark PySide6 shell with 12 pages: Dashboard, Input, Preview, AI Engine, Detection, ROI Editor, Replacement, Profiles, Virtual Camera, Performance, Logs, Settings.
- **Settings**: validated, written atomically, merged with defaults. A corrupt file is backed up and reset; invalid keys are repaired individually.
- **Profiles**: create, rename, duplicate, delete, import and export (JSON), and switching without a restart. The full profile schema is in place (models, classes, confidence, ROIs, assets, masks, playback, performance mode, output).
- **SQLite store**: sessions (crashed sessions are detected on the next start), events, benchmarks.
- **Logging**: rotating log file plus a live log viewer with level and text filters.
- **Live resource monitor**: system/app CPU, RSS, memory pressure, and Apple GPU utilisation from IOAccelerator statistics.
- **Performance modes**: Performance, Balanced, Low Resource, Maximum Quality. These are stored as *targets*, not measured results.
- **Real device list** (AVFoundation) with a guard that refuses the app's own virtual camera as an input, plus the camera-permission state.
- **Camera extension status**, read from `systemextensionsctl`.
- **Hotkeys**: configurable, with conflict detection. They work while the app is focused. Pipeline actions stay disabled until Stage 10.

## What works now (Stage 2)

- **Capture** from any AVFoundation device. OBS Virtual Camera is selected automatically. Frames stay in IOSurface-backed CVPixelBuffers, and only the newest one is kept (no frame queue).
- **Format selection** from the device's real formats (resolution and fps). Measured fps, latency, drops and uptime are shown.
- **Health states:** running, frozen (identical frames, warning only), no signal, reconnecting with backoff, error.
- **Automatic reconnect** after a device disconnect, a session runtime error, or missing frames.
- **Self-loop guard:** the app's own *AI Virtual Camera* can never be selected as input.
- **Previews:** the original live preview on the Preview and Input pages, decimated to the view size at ≤30 fps. The processed preview stays empty until Stage 10.
- **Test-pattern input** for diagnostics, and a headless `--capture-selftest` mode.

## What works now (Stage 3)

- **YOLO11 → Core ML conversion** (FP16, with NMS inside the model). See [models/README.md](models/README.md).
- **Inference through Apple Vision** directly on the capture CVPixelBuffers. Compute units are selectable: All, CPU + Neural Engine, CPU + GPU, CPU only.
- **Apple Vision face detection** (rectangles only, no identity).
- **AI engine thread:**
  - Works on the newest frame only.
  - Its rate follows the performance mode (12 / 20 / 30 fps, or unthrottled with the larger model in Maximum Quality) or a fixed per-profile rate.
  - Failures are isolated: after 3 errors in a row it reloads the model, and capture is never affected.
  - Only one detector stays loaded, and models are unloaded when capture stops.
- **Detection page:** confidence, IoU, frequency, include/exclude class tree, live detection table, overlay toggle.
- **AI Engine page:** status, model and compute-unit choice, installed models with their licenses, and an on-device benchmark saved to SQLite.
- **Bounding-box overlays** on the original preview, plus dashboard AI metrics.

## What works now (Stage 4)

- **ByteTrack multi-object tracking:** stable IDs, two-stage association that also uses low-score detections, Kalman smoothing, recovery of lost objects under the same ID, and class-aware matching.
- **Continuous-time prediction:** each displayed frame gets boxes predicted to its own timestamp, so tracking keeps working between AI inferences.
- **Tracking settings** on the Detection page, saved in the profile.
- **Live track table:** ID, state, age.
- **ID-labelled overlays:** lost objects that are still coasting are drawn dashed.
- **Tracking metrics** on the AI Engine page and the dashboard.

## What works now (Stage 5)

- **ROI editor on the live input:** rectangles (move, resize, rotate) and polygons (vertex editing), entered with the mouse or by typing coordinates.
- **Three modes:**
  - Manual: the drawn area.
  - Automatic AI: every matching tracked object.
  - Hybrid: matching objects inside the drawn area, by object centre or by overlap.
- **Per-region settings:** class filters, minimum confidence, max objects, padding, clipping, priority, enable/disable, duplicate and delete.
- **Saved and resolved:** ROIs are stored in the profile and resolved for every output frame from the tracked objects. Polygon masks are generated with NumPy and cached.

## What works now (Stage 6)

- **Asset library:** PNG, JPG, WEBP and more, decoded by Apple ImageIO with alpha. Files are copied into the app's data folder, and unused files are deleted when removed.
- **Per-ROI replacement settings:**
  - mode: region replacement, object replacement (segmentation silhouette), or overlay;
  - fit, fill, stretch or original size, plus scale, offset, rotation, opacity and crop;
  - edge feathering, background fill colour, mask threshold and edge refinement.
- **Segmentation** with YOLO11n-seg in Core ML. Masks are tracked with each object and follow its predicted box.
- **Live replacement preview** on the Replacement page, rendered by the CPU compositor at up to 15 fps.

## What works now (Stage 7)

- **Video assets:** MP4 and MOV are decoded in hardware through VideoToolbox (H.264, HEVC, ProRes, including alpha). Other containers fall back to FFmpeg when it's installed.
- **Playback controls:** loop or hold/hide at the end, start offset, speed, frame-rate conversion (nearest or blend), pause/play/restart, and shared, per-ROI or per-object sync.
- **All replacement modes accept video:** region, object silhouette and overlay. Memory is bounded: 8 buffered frames per player, at most 6 players, and idle players close automatically.

## What works now (Stage 8)

- **Metal GPU compositor:** a native Swift library with zero-copy IOSurface frames. A 1080p frame with 5 layers renders in about 1 ms, versus about 70 ms on the CPU fallback.
- **New ROI effect mode:** blur, pixelate and colour correction.
- **Compositor setting:** Settings → Compositor (Automatic / Metal / CPU). At runtime it falls back to CPU automatically if the GPU fails.

## What works now (Stage 10)

- **Real-time pipeline:** capture → AI → tracking → ROIs → Metal → virtual camera. 60 fps, 3–5 ms latency.
- **Controls:** Start / Stop / Pause output / Emergency passthrough / Replacement on-off, available as buttons and as **system-wide hotkeys**.
- **Watchdogs:** stalled pipeline or AI → restart; memory pressure → load shedding. Repeated render failures switch to automatic passthrough.
- **Synced original/processed preview**, plus live pipeline timings on the Performance page.

## Also completed

- **Input method 2: direct OBS frame transfer** through the `vcstudio-obs` plugin (shared memory, ≈0.7 ms). See [docs/OBS_INTEGRATION.md](docs/OBS_INTEGRATION.md).
- **Portable profiles:** `.vcsprofile` bundles a profile together with its images and videos.

## Stage 9 (virtual camera): in progress

All the code for the "AI Virtual Camera" extension, its host app and the frame sender is written. The sender is verified live through a CMIO sink at 1080p60. Signing and installing the extension needs Xcode and an Apple Developer team: see [docs/CAMERA_SETUP.md](docs/CAMERA_SETUP.md).

## Packaged app (Stage 13a)

```bash
scripts/build_app.sh --dmg     # dist/AI Virtual Camera Studio.app + dist/AI-Virtual-Camera-Studio-0.1.0.dmg
"dist/AI Virtual Camera Studio.app/Contents/MacOS/AIVirtualCameraStudio" --self-check
```

The app is self-contained (no venv or repository needed) and ad-hoc signed. See [docs/PACKAGING.md](docs/PACKAGING.md).

## Documents

- [docs/PACKAGING.md](docs/PACKAGING.md): building the app and DMG, bundle layout, self-check, signing and notarization.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): environment baseline, process topology, the Python↔Swift bridge, failure isolation, signing requirements.
- [docs/ROADMAP.md](docs/ROADMAP.md): stage plan and status.
- [docs/STABILITY.md](docs/STABILITY.md): 2-hour soak with fault injection, memory investigation, stress tests.
- [docs/BENCHMARK.md](docs/BENCHMARK.md): measured performance and memory on the MacBook Pro M5.
- [docs/CAMERA_SETUP.md](docs/CAMERA_SETUP.md): Xcode, signing, install, approval and troubleshooting for the virtual camera.

## Requirements for later stages

The **virtual camera (Stage 9) and the signed installer (Stage 13)** need:
1. **Xcode** (full app, not only Command Line Tools), then `sudo xcode-select -s /Applications/Xcode.app`.
2. An **Apple Developer Program team**. A system-extension entitlement requires a provisioning profile, and a free account can't get one.
3. A signing certificate in the login keychain: Apple Development for testing, Developer ID Application for distribution.
