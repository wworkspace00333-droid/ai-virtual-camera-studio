# Testing

| Level | Command | What it covers | Duration |
|---|---|---|---|
| Unit + integration | `.venv/bin/python -m pytest -q tests` | Every module; UI pages driven with Qt events; CPU vs Metal parity; synthetic-scene pipeline; watchdog fault injection; leak regression | ~50 s |
| Live hardware | `VCS_LIVE=1 .venv/bin/python -m pytest -s tests/test_capture_live.py tests/test_camera.py tests/test_pipeline.py -k "live or obs or capture or camera"` | Real AVFoundation capture of OBS Virtual Camera; CMIO sink transport; full end-to-end through a CMIO camera and an independent client | ~1 min |
| Stress | `VCS_STRESS=1 .venv/bin/python -m pytest -s tests/test_stress.py` | 24 ROIs; a ~30-person crowd with segmentation; 6 video players; configuration churn; input flapping; 20 start/stop cycles; GUI page switching under load | ~4 min |
| Soak | `open -W -n "build/AI Virtual Camera Studio Dev.app" --args --soak build/soak --minutes 120 [--device "AI Virtual Camera" --sink "AI Virtual Camera Input"]`, then `.venv/bin/python scripts/analyse_soak.py build/soak` | Hours of operation with a fault injected every ~30 s (stall, freeze, passthrough, profile switch, AI off, pause, ROI edit, CPU fallback); memory/thread/fd drift; recovery checks; an independent client watching the camera | 2 h |
| Benchmarks | `scripts/benchmark.py`, `scripts/profile_memory.py`, `scripts/profile_cycles.py`, `scripts/profile_cpu.py` | Per-mode performance; memory breakdown; leak cycles; per-thread CPU | minutes |

The live, stress and soak levels need the dev app bundle (`scripts/build_dev_app.sh`), because macOS grants camera access per app bundle. They also need the native library (`scripts/build_native.sh`) and the converted models (`models/README.md`).

**Rules the suite enforces:**
- No test may leave capture, AI, pipeline or video threads running: an autouse fixture checks after every test.
- Tests never register system-wide hotkeys (`VCS_DISABLE_GLOBAL_HOTKEYS`).
- Features scheduled for later stages must be visibly labelled, never faked.
