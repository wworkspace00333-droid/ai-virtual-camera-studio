# Using OBS Studio as the video source

AI Virtual Camera Studio reads OBS's output through **OBS Virtual Camera** (AVFoundation), processes it, and
publishes the result as **AI Virtual Camera**.

```
OBS Studio ──► OBS Virtual Camera ──► AI Virtual Camera Studio ──► AI Virtual Camera ──► Zoom / Meet / FaceTime / OBS
```

## Set up OBS

1. **Settings > Video:** set *Output (Scaled) Resolution* to **1920×1080** and *Common FPS Values* to **60**. You can also use 1280×720 or 30 fps; the app adapts to the camera format.
2. Click **Start Virtual Camera** in the Controls dock. When OBS's virtual camera isn't started, it shows a placeholder image (the OBS logo). The app reports that as **Frozen input** (identical frames), which is expected.
3. You don't need to change "Output Type" or the target in the virtual-camera settings: the default (Program) is what the app captures.

## Set up the app

1. Open the app with **AI Virtual Camera Studio** in `/Applications` (signed build), or with `build/AI Virtual Camera Studio Dev.app` (development build). macOS asks once for camera permission: click **Allow**.
2. On the **Input** page, choose *Method: Camera device*, *Device: OBS Virtual Camera*, *Format: 1920×1080 @ 60 fps*, then **Start capture**. Measured on an M5: 59–60 fps with about 3 ms capture latency.
3. Open **Preview** and click **Start**. The right-hand view is exactly what AI Virtual Camera sends.
4. In the receiving app (Zoom, Meet, FaceTime…), select **AI Virtual Camera**.

## Things that cannot work (and are prevented)

- **Don't select AI Virtual Camera as a source inside the same OBS scene that feeds OBS Virtual Camera.** That would loop the output back into the input. The app refuses AI Virtual Camera as its own input. It can't see loops that go through OBS, so avoid them.
- **Don't use OBS Virtual Camera as both input and output.** The app's feedback guard refuses to start if the input device is the configured output camera.

## Input method 2: direct frame transfer (vcstudio-obs plugin)

The plugin hands OBS's final program output to the app through shared memory. OBS's own virtual camera then stays free for other uses, and latency is lower.

| | OBS Virtual Camera | Direct transfer (plugin) |
|---|---|---|
| Latency OBS → app | ≈ 3 ms | **≈ 0.7 ms median, 1.9 ms p95** (measured with the protocol test writer at 1080p60) |
| Occupies OBS Virtual Camera | yes | no |
| Timestamps | camera clock | OBS render timestamps |
| Setup | none | install plugin once |

**Install**
1. Build the plugin with `scripts/build_obs_plugin.sh` (development; the packaged app ships it prebuilt).
2. Install it: **Input page > Method: Direct OBS frame transfer > Install OBS plugin**, or run `scripts/build_obs_plugin.sh --install`. This copies `vcstudio-obs.plugin` to `~/Library/Application Support/obs-studio/plugins/`.
3. Restart OBS, then enable **Tools > AI Virtual Camera Studio: share frames (on/off)**. The setting is remembered across OBS restarts.
4. In the app, choose **Direct OBS frame transfer** and click **Start capture**. The Input page shows "OBS is sharing W×H @ fps".

**How it works:**
- The plugin registers `obs_add_raw_video_callback` with a BGRA conversion at OBS's output resolution.
- It writes each frame into POSIX shared memory `/vcs-obs-frames`: three slots plus a sequence lock (`native/obs-plugin/vcs_frame_shm.h`). OBS never waits for the app.
- The app's C reader (`libVCSShm`) copies the newest complete frame straight into an IOSurface pixel buffer, without holding the GIL. From there on, processing is identical to camera capture.
- If OBS quits or stops sharing, the input shows *No signal*, and the app reconnects automatically when OBS comes back.

**Notes:**
- OBS loads third-party plugins because it is signed with `disable-library-validation`. The plugin is ad-hoc signed and needs no Apple certificate.
- The plugin is built against the headers of the **installed OBS version** (fetched automatically). Rebuild it after updating OBS.
- Maximum shared resolution is 3840×2160. After changing OBS's output resolution, toggle sharing off and on.

## Output through OBS Virtual Camera (no Apple Developer account)

Installing the app's own **AI Virtual Camera** needs a paid Apple Developer team (see [CAMERA_SETUP.md](CAMERA_SETUP.md)).
Without one, the app can send its processed video into **OBS Virtual Camera** instead. OBS's camera extension is
already signed and approved on Macs that run OBS, and it accepts frames through the same sink mechanism.

```
OBS Studio ──(vcstudio-obs plugin)──► AI Virtual Camera Studio ──► OBS Virtual Camera ──► Zoom / Meet / FaceTime / QuickTime
```

1. Install the vcstudio-obs plugin (above), restart OBS and enable **Tools > AI Virtual Camera Studio: share frames**.
2. In the app, **Virtual Camera > Send processed video to > OBS Virtual Camera (no Apple Developer account needed)**.
   If the input was OBS Virtual Camera, the app switches it to *Direct OBS frame transfer* automatically (the same
   camera can't be input and output).
3. **Preview > Start.** In the receiving app, select **OBS Virtual Camera**.
4. Don't click **Start Virtual Camera** in OBS while the app feeds it: both would write to the same camera.

Measured on a Mac mini M2 Pro (Mac14,12) (2026-11-12), OBS at 1080p30, `--pipeline-e2e-test … --input obs`: client received
30.1 distinct fps at 1920×1080, the replacement was visible in 100% of frames, 0 render errors, pipeline latency
3.3 ms (p95 4.5 ms). Loopback into the OBS camera alone: 351 of 352 frames, 6.0 ms median / 15.6 ms p95.

```bash
open -W -n "build/AI Virtual Camera Studio Dev.app" --args --pipeline-e2e-test /tmp/obs-e2e.json --seconds 10 \
  --device "OBS Virtual Camera" --sink "OBS Camera Extension Stream Sink" --input obs
```

## Performance tips

- Keep OBS's own GPU load reasonable (fewer filters and scenes). The app's AI and compositing share the GPU and Neural Engine.
- **Balanced** mode (AI 20 fps) is a good default. Output stays at 60 fps in every mode: tracking predicts object positions between AI updates. Measurements are in [BENCHMARK.md](BENCHMARK.md).
