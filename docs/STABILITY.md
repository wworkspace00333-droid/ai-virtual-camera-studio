# Stability: soak, fault injection and memory (Stage 12)

All runs were on the target MacBook Pro M5 (16 GB), macOS 27.2, on 2026-11-09.

## 60-minute GUI run on the real OBS video (Mac mini M2 Pro, 2026-11-12)

`scripts/gui_soak.py`: the real window (Russian), OBS input through the plugin, OBS Virtual Camera output in NV12,
Hide face and a game card on; a different page every 5 s; a 50 ms UI timer measures stalls; OBS quit and reopened
at minutes 20 and 40.

| Measure | Result |
|---|---|
| Render errors / AI errors | 0 / 0 |
| Frames sent to the camera / dropped | 106,502 / **0** |
| Input / output fps (median) | 30 / 30 (OBS runs at 30) |
| Footprint first quarter → last quarter (max) | 405.5 → 414.2 MB (528 MB): no growth |
| Threads / file descriptors | 13–30 / 27–38, no accumulation |
| OBS quit + reopen, twice | recovered ≤ 10 s after OBS opened (10 s sampling) both times |
| UI stalls | max 0.30 s; 5 of the 6 stalls > 0.25 s coincided with the tool's own memory sampling (`footprint` on the UI thread, since moved to a thread); the sixth (0.26 s) at the moment OBS quit |

## 30-minute soak on a second Mac (Mac mini M2 Pro (Mac14,12), 16 GB, macOS 27.2.1, 2026-11-12)

Same command and workload as below with `--minutes 30` (6 full fault cycles), output to OBS Virtual Camera.

| Measure | Result |
|---|---|
| Output fps (median / p05) | 60.0 / 60.0; independent client 60.5 / 60.2 |
| Latency median / worst p95 | 3.5 ms / 7.0 ms |
| Footprint start → end (max) | 462 → 452 MB (503 MB); last two quarters 430 → 427 MB |
| Threads / file descriptors | 36–47 / 21 flat |
| Render errors, AI errors, watchdog restarts | 0, 0, 0 |
| Camera frames sent / dropped | 105,157 / 0 |
| Verdict | all faults recovered, no memory growth, sustained 60 fps, threads and fds stable |

The stress suite (`VCS_STRESS=1`, 7 tests) and all live tests (`VCS_LIVE=1`) also pass on this Mac; only the
AI Virtual Camera test is skipped (extension not installed, Stage 9).

## 2-hour soak with fault injection

- **Command:** `open -n "build/AI Virtual Camera Studio Dev.app" --args --soak build/soak_2h --minutes 120 --device "OBS Virtual Camera" --sink "OBS Camera Extension Stream Sink"`, then `scripts/analyse_soak.py build/soak_2h`.
- **Workload:**
  - 1080p60 zero-copy input with the bus scene (4 people);
  - YOLO11n-seg at 20 fps with ByteTrack;
  - silhouette replacement of people, a video region, a feathered rotated image, and a colour effect;
  - Metal compositing;
  - output through a real CMIO camera sink, watched by an independent AVFoundation client.
- **Fault schedule**, repeated every 5 minutes, with recovery verified after each fault (frames flowing, capture running, within 10 s):
  1. input stall (3 s);
  2. frozen input (4 s);
  3. emergency passthrough (5 s);
  4. switch to another profile (detection + pixelate) for 30 s and back;
  5. AI off (5 s);
  6. output paused (3 s);
  7. ROI moved while running;
  8. compositor forced to CPU for 10 s and back to Metal.

| Metric | Result |
|---|---|
| Duration | 120.1 min (1,424 samples, 5 s apart) |
| Faults injected / recovered | **192 / 192**. Slowest recovery 1.32 s (input stall, including reconnect); every other kind ≤ 0.8 s |
| Render errors / AI errors / watchdog restarts | **0 / 0 / 0** |
| Frames sent to the camera / dropped | **419,753 / 0** |
| Output fps outside fault windows | median **60.0**, 5th percentile 57 |
| Client fps (independent capture of the camera) | median **60.4**, 5th percentile 56.2 |
| Capture → output latency | median 4.3 ms; worst 1-second p95 41.9 ms (around fault transitions) |
| Capture reconnects | 24 (one per injected stall; all recovered) |
| Threads | 37–48, trend +0.2 per hour (flat) |
| File descriptors | 21–22, trend 0 |
| Footprint | 527 MB → 662 MB at the end, peak 765 MB; trend **+90 MB/h** (investigated below) |

**A note on the first attempt:** it stopped after 2.8 minutes. The app had been launched with `open -W` inside a background job and was ended along with that job. There was no crash report and no Python error. The second run was launched detached, with `faulthandler` and SIGTERM recording enabled, and completed normally.

## Memory growth investigation

The soak's footprint trend (+90 MB/h) was traced with `scripts/leak_bisect.py`, which repeats each fault type in isolation and measures the physical footprint.

| Operation | 20 reps: MB per rep | 60 reps: footprint every 10 reps | Conclusion |
|---|---|---|---|
| no faults (control) | +0.10 | — | flat |
| compositor CPU fallback | +1.85 | 404 → 414 → 418 → 419 → 419 → 419 | **plateau** (allocator/pool high-water mark of the CPU path) |
| input stall + reconnect | +1.28 | 392 → 393 → 394 → 395 → 395 → 393 | **plateau** (the 20-rep figure was warm-up) |
| profile switch (model reload) | +0.50 | 380 → 380 → 383 → 384 → 385 → 386 | ≈ 0.1 MB per reload, within noise |
| ROI edit | +0.19 | — | negligible |
| freeze / passthrough / AI off / pause | −0.7 … +0.03 | — | none |

The 2-hour soak contained only 24 repetitions of each fault. Plateaus are reached after 20–40, so the soak ended while memory was still *approaching* them.

### Accelerated aging (all 8 faults back to back)

`scripts/leak_bisect.py 100 all`: 100 full fault cycles, equivalent to about 8.3 hours of the soak's fault exposure. Headless, camera output off.

| Cycles | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|
| Footprint (MB) | 426 | 437 | 454 | 451 | 450 | 457 | 443 | 459 | 461 | 459 |

The footprint reaches its high-water level within about 30 cycles (≈ 2.5 h of soak) and then stays **flat**: +5 MB over cycles 30–100, within noise. **There is no leak in the processing path.** The memory is held by the allocator and the pixel-buffer pools for reuse.

### Camera output path and AVFoundation capture

| Run (60 fault cycles, ≈ 5 h of soak faults) | Footprint every 10 cycles (MB) | Verdict |
|---|---|---|
| Camera output **on** + an AVFoundation client capturing that camera **inside the same process** | 595, 593, 605, 617, 623, 646 | still rising |
| Camera output **on**, in-process client **off** | 428, 444, 470, 458, 468, 473 | **plateau** (+3 MB over cycles 30–60) |

In both runs the camera received every frame: 39,344 sent, 0 dropped, and the client held 60 fps. **The app's camera output path does not leak.** The growth belonged to the in-process *test client*. That client re-captures the app's own output, whose pixel-buffer pools are recreated by the faults (each input stall builds a new benchmark source), and it counts against the app only because it runs in the same process. A real client (Zoom, FaceTime) is a separate process, and real AVFoundation input does not recreate pools.

The product's real input path was checked directly as well: OBS Virtual Camera at 1080p60 for 150 s (`--run-script capture_memory.py`). The footprint stayed flat (211 → 207 MB without a per-frame autorelease pool, 212 → 212 MB with one) at 56–59 fps. A per-frame pool is kept in the capture callback as zero-cost insurance.

## Stress tests (`VCS_STRESS=1 pytest tests/test_stress.py`, 7/7 pass)

| Test | Result |
|---|---|
| 24 ROIs (images, overlays, shared video, blur / pixelate / colour) | 60 fps median (worst second 52–56), latency p95 ≤ 12 ms, processing 4–8 ms per frame, 0 errors |
| Crowd (≈ 30 people in a 3×2 tiled scene) with silhouette replacement | 24 tracks, 58.5 fps median, IDs stable on a static scene |
| 6 simultaneous video players (per-ROI sync, staggered offsets) | 60 fps median, 0 errors, 353 MB |
| 30 s configuration churn (285 random profile, ROI, passthrough, mode and AI changes) | 0 render errors, 0 restarts, frames kept flowing |
| Input flapping (12 stalls of 0.7 s) | 12 reconnects, ends running at full rate |
| 20 rapid start/stop cycles | footprint 521 → 521 MB, no leftover worker threads |
| GUI page switching every 100 ms under load | 200 switches, 60 fps, 0 errors |

**Defects found by stress testing and fixed:**
- **PyObjC lazy-symbol race.** Six video decoders starting at the same moment made one fail with `AttributeError: 'CMTimeMakeWithSeconds'`: PyObjC resolves framework symbols lazily, and that resolution is not thread-safe. All 79 Objective-C symbols the app uses are now resolved on the main thread at start-up (`vcstudio/core/pyobjc_prewarm.py`). A test fails if new code uses a symbol missing from the list, and a regression test starts six decoders from six threads at once.
- **24-ROI cost.** A video frame shared by several regions was uploaded to the GPU once per region; asset paths were rebuilt on every frame. Both are now done once per frame. The first stress run's 13.9 ms per frame was mostly one-time start-up (model compile and decoder warm-up in a fresh data folder); steady state is now 4–8 ms.

## Live OBS plugin check (input method 2)

OBS 33.0.1 with `vcstudio-obs` installed and sharing enabled:
- the OBS log shows `[vcstudio-obs] loaded (protocol v1)` and then `sharing 720x1280 @ 60/1 fps`;
- the app captured **61 fps, 673 frames, 0 dropped** at portrait 720×1280;
- quitting OBS logged `stopped after 3757 frames`, and the app reported "OBS stopped sharing", which triggers the automatic reconnect.

The frames were black because every media file in that OBS scene collection is missing on disk, so OBS rendered black. **Colour correctness of real OBS content through the plugin is therefore not yet verified live**; it is verified with the protocol test writer. Latency from OBS's render timestamp to the app was 20 ms median. That figure is dominated by OBS's own GPU→CPU readback; the transport itself adds 0.7 ms.

## 30-minute soak with real AVFoundation capture (OBS Virtual Camera)

`--soak build/soak_real --minutes 30 --input "OBS Virtual Camera"`. OBS itself was not running, so the camera delivered its static placeholder (capture state *frozen*: identical frames, still flowing at full rate). Camera output was off, because OBS's camera cannot be both input and output.

| Metric | Result |
|---|---|
| Output fps | median **60**, 5th percentile **59** |
| Capture → output latency | median 2.3 ms, worst p95 6.3 ms |
| Errors / watchdog restarts / capture reconnects | 0 / 0 / 0 |
| Footprint | 408 → 439 MB; third-quarter median 426, last-quarter median 435 (+2 %): **plateau** |
| Threads / file descriptors | 33–43 (trend −3/h), 21–22 |
| Faults | 48 injected. The harness first scored them "not recovered" because its check required capture state *running* and did not accept *frozen*: a harness bug, now fixed. Re-scored from the samples, **48/48** were followed by flowing output within one 5 s sample period. Input stall and freeze are no-ops with a real camera; real input dropouts are covered by the reconnect and flapping tests. |

## Conclusion

- **Fault tolerance:** 192 / 192 injected faults recovered over 2 hours, with no errors, no watchdog restarts and no dropped camera frames.
- **Throughput:** 60 fps sustained at the output and at an independent client, outside fault windows.
- **Real capture:** 30 minutes of OBS Virtual Camera input at 60 fps (5th percentile 59) with 2.3 ms latency, and memory at a plateau.
- **Memory:** bounded. The processing path, the camera output and AVFoundation capture all reach a stable footprint (≈ 460–475 MB headless or with camera output, ≈ 210 MB for capture alone). The slow rise seen in the 2-hour soak is pools and the allocator reaching their high-water marks, plus test-client memory counted in the same process.

## How to run

```bash
open -n "build/AI Virtual Camera Studio Dev.app" --args --soak build/soak --minutes 120     # long run
.venv/bin/python scripts/analyse_soak.py build/soak                                         # verdict
VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/leak_bisect.py 60 cpu_backend input_stall
VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/leak_bisect.py 100 all                 # accelerated aging
```
