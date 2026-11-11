# Troubleshooting

The app's **Logs** page shows the live log and the recorded events (watchdog actions, state changes, failures).
The log files are in `~/Library/Application Support/AIVirtualCameraStudio/logs/`.

## Input

| Symptom | Cause / fix |
|---|---|
| "Camera permission: denied" | System Settings > Privacy & Security > Camera: enable *AI Virtual Camera Studio* (or *… Dev*), then restart the app. When the app is started as plain Python from a terminal or IDE, macOS asks on behalf of *that* app instead; use the app bundle. |
| State **Frozen input** with the OBS logo | OBS's virtual camera isn't started (OBS shows a placeholder). Click *Start Virtual Camera* in OBS. A truly static scene also reads as frozen; that's only a warning. |
| State **No signal / Reconnecting** | The device stopped delivering frames, for example because OBS quit. The app retries with backoff and resumes on its own. |
| "Refusing to capture from AI Virtual Camera" | That's the app's own output; using it as input would create a feedback loop. Pick OBS Virtual Camera. |
| Direct transfer: "OBS is not sharing frames" | Install the plugin (Input page), restart OBS, and enable *Tools > AI Virtual Camera Studio: share frames*. Check OBS's log for `[vcstudio-obs] sharing …`. |
| Direct transfer: plugin missing from OBS's Tools menu | OBS was updated: rebuild with `scripts/build_obs_plugin.sh --install` (it must match the OBS version). If OBS started in Safe Mode after a crash, third-party plugins are disabled: restart OBS normally. |
| "Input … is also the output camera" | The input and output devices are the same (Settings > Output). Choose different devices. |

## AI

| Symptom | Cause / fix |
|---|---|
| AI state **error: model load failed / No detect models found** | Convert the models: `.venv-convert/bin/python scripts/convert_models.py` (see `models/README.md`). |
| Objects detected but no replacement on them | The ROI's classes are filtered out on the Detection page, or AI is disabled. The ROI Editor shows a warning in both cases. |
| Object replacement shows nothing | Silhouettes need the segmentation model (`yolo11n-seg`). Without a mask the object is skipped by design, unless *Without mask: use its box* is selected. |
| AI state **degraded** / watchdog "AI restart" in Logs | Repeated inference failures or a stall: the engine reloads the model automatically while video output continues. If it repeats, try *Compute units: CPU + GPU* on the AI Engine page. |
| Maximum Quality uses a smaller model than expected | The model measured above the 25 ms p95 budget on this Mac and was stepped down. The AI Engine page shows the reason. |

## Output and virtual camera

| Symptom | Cause / fix |
|---|---|
| Preview: "camera not installed: output visible here only" | Install the camera extension: [CAMERA_SETUP.md](CAMERA_SETUP.md). |
| Receiving app shows "Waiting for AI Virtual Camera Studio…" | The app isn't sending frames: click **Start** on the Preview page, or **Start test pattern** on the Virtual Camera page. |
| Receiving app doesn't list AI Virtual Camera | Quit and reopen that app after installing or approving the extension. |
| Output state **PASSTHROUGH (automatic …)** | Rendering failed 5 times in a row, so the app forwarded the original video to keep the camera alive. See Logs, then switch passthrough off on the Preview page. |
| Compositor shows **CPU** although Metal is selected | The Metal library is missing (`scripts/build_native.sh`) or a GPU error forced a fallback (see Logs). The CPU path is about 75× slower. |

## Performance and memory

| Symptom | Cause / fix |
|---|---|
| Output below 60 fps | Check Performance > Pipeline timings. Choose *Low Resource* or *Balanced*, close other GPU-heavy apps, and lower the video *Decode size*. |
| Memory warning in Logs / dashboard | The footprint is above 3 GB. At 6 GB the watchdog sheds load automatically (Low Resource mode, closes video players, clears caches) and restores itself afterwards. |
| Hotkey doesn't work | Another app owns that shortcut: Settings > Hotkeys lists the conflict, and that action falls back to an in-app shortcut. On macOS, "Ctrl" in the hotkey fields means ⌘. |

## Profiles

| Symptom | Cause / fix |
|---|---|
| Imported profile shows no images or videos | It was exported as *settings only* (`.json`). Export as **.vcsprofile**, which bundles the assets. |
| Import refuses a `.vcsprofile` | The archive contains unexpected paths or files, or a referenced asset is missing. Nothing is imported in that case; see the message. |

## Reset

Quit the app and remove `~/Library/Application Support/AIVirtualCameraStudio/settings.json`. Profiles and assets are kept in `profiles/` and `assets/`; remove those too for a full reset.
