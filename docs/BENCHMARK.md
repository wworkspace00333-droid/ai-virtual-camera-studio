# Performance benchmark: MacBook Pro M5, 16 GB unified memory

All numbers were measured on the target machine (Apple M5, macOS 27.2) on 2026-11-09. They are not projections.
To reproduce:

```bash
VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/benchmark.py --seconds 20      # modes x scenarios
VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/profile_memory.py              # footprint breakdown
VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/profile_cycles.py 6            # start/stop leak check
VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/profile_cpu.py 8               # per-thread CPU profile
VCS_LIVE=1 .venv/bin/python -m pytest -s tests/test_pipeline.py -k live              # end to end through a CMIO camera
```

## Method

- **Input:** 1920×1080 BGRA at 60 fps from `BenchSource`. It hands out pre-rendered IOSurface pixel buffers, exactly as AVFoundation capture does (zero-copy), so the source itself costs nothing. The scene is the Ultralytics bus image: a bus and four people.
- **Processing:** the real pipeline, headless (no preview window): AI → ByteTrack → ROI resolution → Metal compositor. Camera output is off in the per-mode table; the end-to-end row below includes it.
- **Measurements:**
  - Output fps is sampled every second. "min 1 s" is the worst one-second window.
  - Latency runs from the frame reaching the app to the processed output pixel buffer being ready.
  - "Footprint" is the macOS physical footprint (Activity Monitor's *Memory*).
  - GPU utilisation is the **system-wide** IOAccelerator counter, so it includes other apps.

## Results per operating mode (20 s each, after warm-up)

**Scenario A: detection + image overlay + video region + pixelate effect on people**

| Mode | Output fps avg | min 1 s | Latency avg | Latency p95 | Processing | AI model | AI fps | AI p95 | App CPU | GPU (sys) | Footprint |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Performance | 60.1 | 60 | 3.6 ms | 5.2 ms | 3.5 ms | yolo11n | 30.0 | 13.1 ms | 47 % | 29 % | 347 MB |
| Balanced | 60.0 | 59 | 3.7 ms | 4.7 ms | 3.5 ms | yolo11n | 20.0 | 10.4 ms | 38 % | 29 % | 336 MB |
| Low Resource | 60.0 | 59 | 3.5 ms | 4.9 ms | 3.4 ms | yolo11n | 12.0 | 11.5 ms | 31 % | 29 % | 337 MB |
| Maximum Quality | 60.0 | 60 | 2.5 ms | 4.2 ms | 2.4 ms | yolo11m | 60.0 | 16.6 ms | 50 % | 27 % | 347 MB |

**Scenario B: segmentation (object silhouette replacement) + image overlay + video region**

| Mode | Output fps avg | min 1 s | Latency avg | Latency p95 | Processing | AI model | AI fps | AI p95 | App CPU | GPU (sys) | Footprint |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Performance | 59.9 | 59 | 4.0 ms | 5.4 ms | 3.9 ms | yolo11n-seg | 30.0 | 14.0 ms | 51 % | 31 % | 359 MB |
| Balanced | 60.0 | 59 | 4.1 ms | 6.2 ms | 4.0 ms | yolo11n-seg | 20.0 | 15.9 ms | 43 % | 31 % | 368 MB |
| Low Resource | 59.9 | 59 | 3.8 ms | 5.5 ms | 3.7 ms | yolo11n-seg | 12.0 | 14.7 ms | 33 % | 31 % | 349 MB |
| Maximum Quality | 60.0 | 60 | 2.2 ms | 3.3 ms | 2.2 ms | yolo11m-seg | 59.5 | 14.9 ms | 33 % | 27 % | 358 MB |

App CPU is a percentage of one core; the M5 has 10.

**Observations**

- **60 fps output is sustained in every mode** and both scenarios. The AI rate meets every target exactly (12 / 20 / 30 fps). Output never waits for AI.
- **Maximum Quality** chooses the largest model that keeps p95 inference within 25 ms on this machine. That is yolo11m / yolo11m-seg here, running on every frame. The choice is measured live, and the engine steps down if the budget is exceeded.
- **Power management shows up in the numbers.** At low duty cycles the GPU and Neural Engine clock down, so per-inference time in Low Resource is not lower than in Performance. When AI runs on every frame (Maximum Quality), the GPU stays clocked up and compositing latency is lowest.
- **One transient:** in 1 of 5 Low Resource runs, one 1-second window dropped to 45 fps. It was not reproduced in 4 repeat runs (all with a minimum of 59–60).

## Second machine: Mac mini M2 Pro (Mac14,12), 16 GB (2026-11-12)

Same command, same scenarios, 20 s per mode after warm-up. macOS 27.2.1; M2 Pro with 6 performance + 4 efficiency cores. Headless, so these numbers don't include the preview window.

**Scenario A: detection + image overlay + video region + pixelate effect on people**

| Mode | Output fps avg | min 1 s | Latency avg | Latency p95 | Processing | AI model | AI fps | AI p95 | App CPU | GPU (sys) | Footprint |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Performance | 59.9 | 59 | 3.1 ms | 4.2 ms | 3.1 ms | yolo11n | 30.0 | 12.9 ms | 44 % | 38 % | 297 MB |
| Balanced | 60.0 | 59 | 3.2 ms | 4.6 ms | 3.1 ms | yolo11n | 20.0 | 14.4 ms | 38 % | 38 % | 310 MB |
| Low Resource | 59.9 | 59 | 3.3 ms | 4.9 ms | 3.2 ms | yolo11n | 12.0 | 15.2 ms | 31 % | 37 % | 277 MB |
| Maximum Quality | 59.9 | 59 | 2.3 ms | 3.3 ms | 2.2 ms | yolo11m | 59.0 | 16.9 ms | 58 % | 37 % | 273 MB |

**Scenario B: segmentation (object silhouette replacement) + image overlay + video region**

| Mode | Output fps avg | min 1 s | Latency avg | Latency p95 | Processing | AI model | AI fps | AI p95 | App CPU | GPU (sys) | Footprint |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Performance | 60.1 | 60 | 3.3 ms | 4.7 ms | 3.2 ms | yolo11n-seg | 30.5 | 13.0 ms | 50 % | 38 % | 301 MB |
| Balanced | 60.0 | 59 | 3.2 ms | 4.7 ms | 3.1 ms | yolo11n-seg | 20.0 | 17.0 ms | 41 % | 39 % | 277 MB |
| Low Resource | 60.1 | 60 | 3.3 ms | 4.8 ms | 3.2 ms | yolo11n-seg | 12.0 | 17.3 ms | 34 % | 37 % | 292 MB |
| Maximum Quality | 60.0 | 60 | 3.2 ms | 4.3 ms | 3.1 ms | yolo11m-seg | 57.0 | 19.5 ms | 57 % | 39 % | 294 MB |

**Compared with the M5**

- **60 fps output in every mode and both scenarios here too**; AI targets of 12 / 20 / 30 fps are met exactly.
- **Maximum Quality chose the same models** (yolo11m / yolo11m-seg) from its own on-device timing. With segmentation it reached 57 fps of AI instead of the M5's 59.5, and its p95 inference is higher (19.5 ms vs 14.9 ms), still inside the 25 ms budget.
- **Per-frame latency is about the same or slightly lower** (p95 3.3–4.9 ms vs 3.3–6.2 ms); the M2 Pro's GPU is larger than the base M5's.
- **Footprint is 270–310 MB** vs 336–368 MB on the M5.
- **Not repeated on this machine:** the 2-hour soak, the GUI CPU profile and the end-to-end camera run.
- Single run per mode; the M5 section notes a one-off 45 fps window in 1 of 5 runs, so treat one run as indicative.

## Detection accuracy (labelled data, 2026-11-12, M2 Pro)

The app's own Core ML models, run exactly as the app runs them (Vision, compute units "All"), on
Ultralytics COCO128: 128 COCO images, 929 labelled boxes. `scripts/eval_accuracy.py` (data download
instructions inside). P / R = precision (share of shown boxes that are correct) / recall (share of
objects found) at IoU 0.5.

| Model | mAP50 | mAP50-95 | P / R @0.25 | P / R @0.35 | P / R @0.45 | P / R @0.50 | Best F1 (conf) | Median ms |
|---|---|---|---|---|---|---|---|---|
| yolo11n | 0.667 | 0.488 | 0.76 / 0.53 | 0.83 / 0.48 | 0.89 / 0.43 | 0.91 / 0.40 | 0.627 (0.25) | 5.2 |
| yolo11s | 0.737 | 0.564 | 0.78 / 0.64 | 0.84 / 0.56 | 0.88 / 0.51 | 0.90 / 0.49 | 0.700 (0.20) | 6.7 |
| yolo11m | 0.764 | 0.590 | 0.78 / 0.65 | 0.86 / 0.59 | 0.90 / 0.54 | 0.92 / 0.51 | 0.717 (0.20) | 13.0 |
| yolo11n-seg | 0.638 | 0.479 | 0.80 / 0.52 | 0.86 / 0.45 | — | 0.91 / 0.36 | 0.629 (0.25) | 9.6 |
| yolo11m-seg | 0.765 | 0.588 | 0.81 / 0.66 | 0.86 / 0.60 | — | 0.92 / 0.50 | 0.724 (0.25) | 20.2 |

What this changed:
- **"Auto" no longer means yolo11n** outside Low Resource: yolo11s finds 8–11 % more objects at the same precision.
- **Default confidence 0.35 → 0.45** for new profiles: about a quarter fewer wrong boxes (precision 0.84 → 0.88 with yolo11s) for 5 points of recall.
- **New objects need 3 consecutive detections** before they are shown (was 2): one-off false detections, the usual source of flickering stray boxes, never reach the output. Adjustable on the Detection page.
- Weakest classes are small objects (mouse, scissors, handbag, cell phone, traffic light): expect misses there.

A 16-image subset (`tests/data/coco16`, 53 classes) backs `tests/test_accuracy.py` (mAP50 floors per model) and the
**AI Engine > Check accuracy** button, so accuracy can be verified on any Mac.

## Model latency (1920×1080 frame, Core ML, compute units "All", 40 runs)

| Model | Size | Median | p95 |
|---|---|---|---|
| yolo11n | 5 MB | 3.5 ms | 5.0 ms |
| yolo11s | 18 MB | 4.9 ms | 6.4 ms |
| yolo11m | 39 MB | 10.4 ms | 11.3 ms |
| yolo11n-seg | 6 MB | 5.6 ms | 12.3 ms |
| yolo11m-seg | 43 MB | 14.4 ms | 18.9 ms |

Compute-unit comparison for yolo11n: Neural Engine 3.5 ms, All 3.6 ms, GPU 5.5 ms, CPU 12.2 ms (Stage 3).

## End to end through a real CMIO camera

Input 1080p60 → yolo11n-seg (20 fps) → tracking → object replacement → Metal → CMIO sink → an independent AVFoundation client.
OBS's camera extension was used as the transport until the signed AI Virtual Camera can be installed.

| Pipeline fps | Pipeline latency | Client distinct fps | Client resolution | Frames with replacement |
|---|---|---|---|---|
| 60.0 | 4.2 ms | **60.2** | 1920×1080 | **100 %** |

The CMIO sink transport on its own (Stage 9): 99.7 % of frames delivered in order, 6–7 ms median latency from sender to client.

## Memory (16 GB budget)

Breakdown of the physical footprint as components are added (`scripts/profile_memory.py`):

| Step | Footprint | Δ |
|---|---|---|
| Qt app + main window (12 pages) | 157 MB | |
| + 1080p60 capture + pipeline (passthrough) | 311 MB | +154 (incl. 66 MB of benchmark source buffers) |
| + AI detector yolo11n + tracking | 405 MB | +94 |
| + image replacement (Metal) | 465 MB | +60 |
| + video replacement (1 player) | 477 MB | +12 |
| + object replacement (segmentation model replaces detector) | 514 MB | +37 |
| + preview page visible, steady state after 20 s | **500 MB** | flat |
| after Stop | 405 MB | −95 |

- **Leak found and fixed in this stage.** Worker threads (AI, pipeline, video decoder) are plain Python threads with no Objective-C autorelease pool. Core ML outputs, Vision results, Metal command objects and decoded sample buffers therefore accumulated: **the footprint grew from 0.43 GB to 5.6 GB in about 30 s** with segmentation active. Every worker iteration and every native entry point now drains a pool. A regression test (`test_no_autorelease_leak_in_worker_threads`) runs 120 segmentation inferences on a worker thread and requires the footprint to stay within +60 MB.
- **Start/stop cycles** (`scripts/profile_cycles.py`): the footprint after each stop stays flat over 6 cycles (−16 MB from cycle 2 to cycle 6). The ~250 MB that stays after the first stop is one-time framework loading (Core ML/ANE runtime, AVFoundation/VideoToolbox, Metal); it is reused on the next start.
- **Per video player:** about 62 MB at 720p60 (8 buffered frames plus the decoder session), with at most 6 players.
- **Watchdog thresholds:** a warning at 3 GB footprint, and load shedding at 6 GB.

## CPU optimisation (same heavy scenario + preview visible, `scripts/profile_cpu.py`)

| | Before Stage 11 | After |
|---|---|---|
| Per-frame processing | 11.4 ms | **3.9 ms** |
| Capture → output latency | 13.4 ms | **4.0 ms** |
| Pipeline thread | 28 % of a core | 17 % |
| AI thread | 21 % | 8 % |
| UI thread (previews) | 31 % | 22 % |

The changes:
- **Object masks** are refined (threshold, morphology, feathering) once per AI update at ≤256 px and cached on the GPU. The shader resamples them into the predicted box every frame. Before, they were rebuilt at full resolution on the CPU for every output frame.
- **Letterbox and preview downscaling run on the GPU** (MPS). The GPU letterbox is bit-identical to the OpenCV one. It also releases the GIL while the GPU works.
- **Smaller savings:** the model directory scan is cached (it ran on every inference), asset `stat()` calls are throttled, and stats are snapshotted cheaply.
