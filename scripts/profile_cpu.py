"""Per-thread CPU profile of the running pipeline (yappi, CPU clock) under a realistic load.

    VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/profile_cpu.py [seconds]
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-cpu-")
os.environ.setdefault("VCS_DISABLE_GLOBAL_HOTKEYS", "1")
import yappi  # noqa: E402


def main(seconds=10.0):
    from PySide6.QtWidgets import QApplication
    from vcstudio.ui.theme import QSS
    app = QApplication(sys.argv); app.setStyleSheet(QSS)
    from vcstudio.app.application import AppContext
    from vcstudio.core.bench_source import BenchSource, bus_scene
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI, new_rect
    from vcstudio.ui.main_window import MainWindow
    ctx = AppContext(configure_logging=False)
    scene = bus_scene()
    ctx.settings.set("input.method", "test_pattern"); ctx.settings.set("output.camera_enabled", False)
    ctx.capture_factory = lambda: (lambda: BenchSource(scene, 60))
    root = Path(__file__).resolve().parents[1] / "tests" / "data"
    logo = ctx.assets.import_file(root / "logo_alpha.png"); clip = ctx.assets.import_file(root / "clip_h264.mp4")
    r1 = new_rect("Logo", 0.6, 0.1, 0.9, 0.4); r1.rotation = 10
    r1.replacement = ReplacementSpec(enabled=True, asset_id=logo.id, feather_px=4).to_dict()
    r2 = new_rect("Video", 0.1, 0.5, 0.5, 0.9); r2.replacement = ReplacementSpec(enabled=True, asset_id=clip.id).to_dict()
    people = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
                 replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id, feather_px=2).to_dict())
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict(), clip.to_dict()]),
                                         b.__setitem__("rois", [r1.to_dict(), r2.to_dict(), people.to_dict()])))
    w = MainWindow(ctx); w.resize(1500, 950); w.show(); w.goto("preview")
    w.start_processing()
    end = time.monotonic() + 4
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.005)
    yappi.set_clock_type("cpu")
    yappi.start(builtins=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        app.processEvents(); time.sleep(0.005)
    yappi.stop()
    s = ctx.pipeline.stats(); a = ctx.ai.stats(); c = ctx.capture.stats()
    print(f"capture {c.measured_fps:.1f} fps · pipeline {s.output_fps:.1f} fps, process {s.process_ms:.2f} ms, "
          f"latency {s.latency_ms:.2f} ms · AI {a.model} {a.achieved_fps:.1f} fps {a.inference_ms_avg:.1f} ms")
    threads = {t.id: t.name for t in yappi.get_thread_stats()}
    for t in yappi.get_thread_stats():
        print(f"\n=== thread {t.name} ({t.id}): CPU {t.ttot:.2f} s over {seconds:.0f} s = {100 * t.ttot / seconds:.0f}% of a core")
        stats = yappi.get_func_stats(filter={"ctx_id": t.id})
        stats.sort("tsub")
        for f in list(stats)[:9]:
            print(f"   {f.tsub * 1000 / seconds:7.1f} ms/s  {f.ncall:7d} calls  {f.name[:60]}  {Path(f.module).name}:{f.lineno}")
    ctx.stop_capture(); w.close(); ctx.shutdown()


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 10.0)
