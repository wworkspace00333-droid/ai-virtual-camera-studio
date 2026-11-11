"""Memory footprint breakdown: add components one at a time and record the physical footprint.

    VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/profile_memory.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-mem-")
os.environ.setdefault("VCS_DISABLE_GLOBAL_HOTKEYS", "1")
import psutil  # noqa: E402

from vcstudio.core.performance import phys_footprint  # noqa: E402

P = psutil.Process()
rows = []


def mark(label):
    import gc
    gc.collect()
    time.sleep(0.3)
    fp = phys_footprint() / 2**20
    rss = P.memory_info().rss / 2**20
    prev = rows[-1]["footprint_mb"] if rows else 0
    rows.append({"step": label, "footprint_mb": round(fp, 1), "delta_mb": round(fp - prev, 1), "rss_mb": round(rss, 1)})
    print(f"{label:<55} footprint {fp:7.1f} MB  (+{fp - prev:6.1f})   rss {rss:7.1f} MB", flush=True)


def main():
    mark("python + numpy")
    from PySide6.QtWidgets import QApplication
    from vcstudio.ui.theme import QSS
    app = QApplication(sys.argv); app.setStyleSheet(QSS)
    mark("Qt application")
    from vcstudio.app.application import AppContext
    ctx = AppContext(configure_logging=False)
    mark("AppContext (settings, db, Metal context, services)")
    from vcstudio.ui.main_window import MainWindow
    w = MainWindow(ctx); w.resize(1500, 950); w.show(); app.processEvents()
    mark("Main window (12 pages)")

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(); time.sleep(0.01)
    from vcstudio.core.bench_source import BenchSource, bus_scene
    scene = bus_scene()
    ctx.settings.set("input.method", "test_pattern")
    ctx.settings.set("output.camera_enabled", False)
    ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", False))
    ctx.capture_factory = lambda: (lambda: BenchSource(scene, 60))
    ctx.start_capture(); pump(3)
    mark("Capture 1080p60 + pipeline (AI off, passthrough)")
    ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", True)); pump(5)
    mark("+ AI detector yolo11n (Core ML) + tracking")
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI, new_rect
    root = Path(__file__).resolve().parents[1] / "tests" / "data"
    logo = ctx.assets.import_file(root / "logo_alpha.png")
    clip = ctx.assets.import_file(root / "clip_h264.mp4")
    r1 = new_rect("Logo", 0.6, 0.1, 0.9, 0.4); r1.replacement = ReplacementSpec(enabled=True, asset_id=logo.id).to_dict()
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict(), clip.to_dict()]), b.__setitem__("rois", [r1.to_dict()])))
    pump(3)
    mark("+ image replacement (Metal compositing)")
    r2 = new_rect("Video", 0.1, 0.5, 0.5, 0.9); r2.replacement = ReplacementSpec(enabled=True, asset_id=clip.id).to_dict()
    ctx.update_active_profile(lambda b: b.__setitem__("rois", [r1.to_dict(), r2.to_dict()]))
    pump(3)
    mark("+ video replacement (1 player)")
    people = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
                 replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id).to_dict())
    ctx.update_active_profile(lambda b: b.__setitem__("rois", [r1.to_dict(), r2.to_dict(), people.to_dict()]))
    pump(6)
    mark("+ object replacement (segmentation model replaces detector)")
    w.goto("preview"); pump(4)
    mark("+ Preview page visible (original + processed at 30 fps)")
    pump(20)
    mark("steady state after 20 s")
    s = ctx.pipeline.stats(); a = ctx.ai.stats()
    print(f"pipeline {s.output_fps:.1f} fps, latency {s.latency_ms:.1f} ms; AI {a.model} {a.achieved_fps:.1f} fps")
    ctx.stop_capture(); pump(1)
    mark("after stop (models unloaded, players closed)")
    w.close(); ctx.shutdown()
    out = Path(__file__).resolve().parents[1] / "build" / "memory_profile.json"
    out.write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
