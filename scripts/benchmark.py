"""Measured performance benchmark per operating mode (headless, real pipeline, zero-copy 1080p60 input).

    VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/benchmark.py [--seconds 20]

Writes build/benchmark.json, appends to the app's SQLite benchmark table, and prints a Markdown table.
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-bench-")
os.environ.setdefault("VCS_DISABLE_GLOBAL_HOTKEYS", "1")
import psutil  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def scenario_profiles(ctx, logo, clip):
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI, new_rect
    img = new_rect("Logo", 0.6, 0.08, 0.9, 0.38); img.rotation = 8
    img.replacement = ReplacementSpec(enabled=True, asset_id=logo.id, feather_px=4).to_dict()
    vid = new_rect("Video", 0.36, 0.48, 0.66, 0.72); vid.rotation = -3
    vid.replacement = ReplacementSpec(enabled=True, asset_id=clip.id, fit="fill").to_dict()
    obj = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
              replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id, feather_px=2).to_dict())
    pix = ROI(name="People blur", mode="auto", classes=["person"], padding=0.05,
              replacement=ReplacementSpec(enabled=True, mode="effect", effect="pixelate", pixel_size=16).to_dict())
    return {
        "detection + image/video/pixelate": [img, vid, pix],
        "segmentation (object replacement) + image/video": [img, vid, obj],
    }


def measure(ctx, seconds):
    from vcstudio.core.performance import phys_footprint, read_gpu_stats
    proc = psutil.Process()
    proc.cpu_percent(None)
    fps_samples, gpu, lat, proc_ms = [], [], [], []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        time.sleep(1.0)
        s = ctx.pipeline.stats()
        fps_samples.append(s.output_fps)
        lat.append(s.latency_ms); proc_ms.append(s.process_ms)
        g, _ = read_gpu_stats()
        if g is not None:
            gpu.append(g)
    s, a = ctx.pipeline.stats(), ctx.ai.stats()
    return {
        "output_fps_avg": round(statistics.mean(fps_samples), 1), "output_fps_min_1s": round(min(fps_samples), 1),
        "latency_ms_avg": round(statistics.mean(lat), 2), "latency_ms_p95": round(s.latency_ms_p95, 2),
        "process_ms_avg": round(statistics.mean(proc_ms), 2), "gpu_render_ms": round(s.gpu_ms, 2),
        "render_errors": s.render_errors, "inputs_skipped": s.inputs_skipped,
        "ai_model": a.model, "ai_target_fps": a.target_fps, "ai_fps": round(a.achieved_fps, 1),
        "ai_ms_avg": round(a.inference_ms_avg, 2), "ai_ms_p95": round(a.inference_ms_p95, 2),
        "ai_quality_note": a.quality_note, "tracks": a.tracks_active,
        "app_cpu_pct": round(proc.cpu_percent(None), 0),
        "gpu_util_pct_avg": round(statistics.mean(gpu), 0) if gpu else None,
        "footprint_mb": round(phys_footprint() / 2**20, 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--modes", default="performance,balanced,low_resource,max_quality")
    args = ap.parse_args()
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication(sys.argv)      # noqa: F841  (Qt objects need an app; no GUI here)
    from vcstudio.app.application import AppContext
    from vcstudio.core.bench_source import BenchSource, bus_scene
    from vcstudio.core.system_info import inspect_environment
    ctx = AppContext(configure_logging=False)
    scene = bus_scene()
    ctx.settings.set("input.method", "test_pattern"); ctx.settings.set("output.camera_enabled", False)
    ctx.capture_factory = lambda: (lambda: BenchSource(scene, 60))
    logo = ctx.assets.import_file(ROOT / "tests/data/logo_alpha.png")
    clip = ctx.assets.import_file(ROOT / "tests/data/clip_h264.mp4")
    env = inspect_environment()
    hw = {k: env[k] for k in ("cpu", "memory_gb", "macos")}
    results = []
    for scen, rois in scenario_profiles(ctx, logo, clip).items():
        for mode in args.modes.split(","):
            ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict(), clip.to_dict()]),
                                                 b.__setitem__("rois", [r.to_dict() for r in rois]),
                                                 b.__setitem__("performance_mode", mode)))
            ctx.settings.set("performance.mode", mode)
            ctx.start_capture()
            time.sleep(6 if mode == "max_quality" else 4)       # warm-up (model load; MQ judging)
            r = {"scenario": scen, "mode": mode, **measure(ctx, args.seconds)}
            ctx.stop_capture()
            results.append(r)
            print(json.dumps(r), flush=True)
            ctx.db.add_benchmark(f"pipeline/{scen}/{mode}", hw, r)
    out = ROOT / "build" / "benchmark.json"
    out.write_text(json.dumps({"hardware": hw, "input": "1920x1080 BGRA 60 fps, zero-copy IOSurface",
                               "seconds_per_run": args.seconds, "results": results}, indent=2))
    cols = ["mode", "output_fps_avg", "output_fps_min_1s", "latency_ms_avg", "latency_ms_p95", "process_ms_avg",
            "ai_model", "ai_fps", "ai_ms_p95", "app_cpu_pct", "gpu_util_pct_avg", "footprint_mb"]
    for scen in dict.fromkeys(r["scenario"] for r in results):
        print(f"\n**{scen}**\n\n| " + " | ".join(cols) + " |\n|" + "---|" * len(cols))
        for r in (x for x in results if x["scenario"] == scen):
            print("| " + " | ".join(str(r[c]) for c in cols) + " |")
    ctx.shutdown()


if __name__ == "__main__":
    main()
