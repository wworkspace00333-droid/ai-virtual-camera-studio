# AI Virtual Camera Studio — Architecture

## 1. Environment baseline (inspected 2026-11-09)

| Item | Found | Consequence |
|---|---|---|
| macOS | 27.2 (26B123) | CMIOExtension available (macOS 12.3+) |
| CPU / memory | Apple M5, 16 GB unified, arm64 | Target hardware confirmed |
| Metal | Metal 4, `recommendedMaxWorkingSetSize` ≈ 11.8 GB | GPU compositing is possible |
| Core ML / Vision | Available through PyObjC | ANE/GPU inference from Python is possible |
| Swift | 6.3.3 (Command Line Tools) | SwiftPM dylibs/executables build |
| Xcode | **Not installed** | Needed for the camera extension (Stage 9) |
| Code-signing identities | **None** | Needed for the camera extension (Stage 9) |
| Python | 3.12 (Homebrew) venv in `.venv` | 3.11+ requirement met |
| OBS | OBS.app with **OBS Virtual Camera** extension active | Input source available |
| FFmpeg | 9.0.1 (Homebrew) | Fallback decoder for assets |
| Camera TCC | `notDetermined` | macOS will ask the first time capture starts (Stage 2) |

## 2. Process and module topology

```
┌──────────────────────── AI Virtual Camera Studio.app (Python + PySide6) ──────────────────────┐
│                                                                                               │
│  UI thread (Qt)          Capture thread            Pipeline thread           AI worker        │
│  ─────────────           ──────────────            ───────────────           ─────────         │
│  pages, previews  ◄──    AVFoundation  ──latest──► tracker predict ──────►   Core ML YOLO     │
│  (downscaled            (CVPixelBuffer,  frame     ROI resolve               Vision faces     │
│   QImage, ≤30 fps)       IOSurface)      slot      compositor (Metal)  ◄──   segmentation     │
│                                                         │              results (async,        │
│                                                         │              newest wins)           │
│                                                         ▼                                     │
│                                            libVCSNative.dylib (Swift, C ABI via ctypes)       │
│                                            • Metal compositor on IOSurface textures           │
│                                            • CMIO sink client (enqueue CMSampleBuffers)       │
└─────────────────────────────────────────────────────────┬─────────────────────────────────────┘
                                                          │ CMIO sink stream (IOSurface-backed
                                                          │ CVPixelBuffers cross by mach port,
                                                          ▼ no pixel copies)
                 ┌──────────── AI Virtual Camera.systemextension (Swift, CMIOExtension) ──────────┐
                 │ sink stream  ──►  source stream "AI Virtual Camera" 1920×1080 @ 60/30, BGRA/NV12 │
                 │ fallback: last-frame repeat / "no signal" card when the app is gone            │
                 └────────────────────────────────────────────────────────────────────────────────┘
                                                          ▼
                                       Zoom / Teams / FaceTime / QuickTime / browsers
```

### Python ↔ Swift communication

There are two bridges, and each one does a different job:

1. **In-process native library (`libVCSNative.dylib`)**: SwiftPM builds it with plain Command Line Tools, and Python loads it with `ctypes`. Every frame-rate operation crosses this bridge: Metal compositing, IOSurface/CVPixelBuffer pooling, and pushing frames to the camera. Python passes only handles (`CVPixelBufferRef` pointers from PyObjC) and small parameter structs, never pixel arrays.
2. **App → camera extension: a CMIOExtension *sink stream*.** OBS uses this same mechanism, and it is the transport Apple supports across the sandbox boundary. The extension publishes one hidden sink stream and one public source stream. The app opens the device through the CoreMediaIO C API and calls `CMSimpleQueueEnqueue` with IOSurface-backed sample buffers. The surfaces move by mach port, so the pixels themselves are never copied. Shared memory or sockets would also need an App Group, and they still could not carry IOSurfaces as cleanly.

Control messages that are not frames (status, heartbeat, the "no signal" text) travel as custom CMIO properties on the device.

### Why not all-Python?

One 1080p BGRA frame is 8.3 MB. If NumPy touched every frame at 60 fps, that would be about 500 MB/s of copies, plus Python overhead. So in the final pipeline Python touches pixels only for:
- AI inference input. A downscaled 640×640 copy is made by Core ML/Vision directly from the CVPixelBuffer.
- UI previews, downscaled to ≤960 px wide at ≤30 fps.
- Development fallbacks (OpenCV path), which are labelled as such in the UI.

## 3. Threading and backpressure rules

- **Latest-frame slots, never FIFO queues of frames.** Each stage reads the newest frame. When a newer frame arrives, the older one is dropped and counted (`frames_dropped`).
- **AI never blocks output.** The pipeline uses whatever detections are newest. ByteTrack's Kalman prediction carries boxes forward between inference frames.
- **Pixel buffers come from fixed-size `CVPixelBufferPool`s** (3–4 buffers each), so memory is bounded by construction.
- **One AI model per role stays loaded:** a detector, an optional face model and an optional segmenter. `model_manager` unloads models a profile doesn't use.

## 4. Failure isolation

| Failure | Behaviour |
|---|---|
| AI worker exception or stall | AI is marked degraded. Output continues as passthrough or with the last tracks. The watchdog restarts the worker. |
| A replacement layer fails | Only that layer is disabled. Other ROIs keep running. |
| Input lost or frozen | The last frame plus a "signal lost" overlay go out, and reconnect runs with backoff. |
| App quits or crashes | The extension shows a "no signal" card, so client apps keep the device open. |
| Emergency passthrough | Input goes straight to output, bypassing AI and compositing. |

## 5. Persistence

- `~/Library/Application Support/AIVirtualCameraStudio/` (override with `VCSTUDIO_HOME`)
  - `settings.json`: app-wide settings. Writes are atomic and the file is deep-merged with defaults.
  - `profiles/*.json`: one file per profile, which can be exported and imported.
  - `studio.db` (SQLite): sessions, watchdog/recovery events, benchmark results.
  - `logs/studio.log`: rotating log file.
- Everything stays local. There is no network code in the app.

## 6. Signing and distribution requirements (Stage 9/13)

To install a CMIOExtension, all of the following are required:
- An **Apple Developer Program** membership (team ID). The system-extension entitlement needs a provisioning profile.
- The entitlements `com.apple.developer.system-extension.install` (host app) and `com.apple.security.app-sandbox` plus an App Group (extension).
- **Xcode** for provisioning-profile management. The bundle can be assembled with SwiftPM + `codesign`, but profiles are created through Xcode or the developer portal.
- The app must run from `/Applications`. The user approves the extension in **System Settings → General → Login Items & Extensions → Camera Extensions**.
- The DMG must be signed with Developer ID and notarized.
