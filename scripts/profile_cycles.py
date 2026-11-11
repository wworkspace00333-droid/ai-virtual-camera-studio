"""Start/stop the full pipeline repeatedly and record the footprint after each stop (leak check)."""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-cyc-")
os.environ.setdefault("VCS_DISABLE_GLOBAL_HOTKEYS", "1")


def main(cycles=6, run_s=6.0):
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    from vcstudio.app.application import AppContext
    from vcstudio.core.bench_source import BenchSource, bus_scene
    from vcstudio.core.performance import phys_footprint
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI, new_rect
    ctx = AppContext(configure_logging=False)
    scene = bus_scene()
    ctx.settings.set("input.method", "test_pattern"); ctx.settings.set("output.camera_enabled", False)
    ctx.capture_factory = lambda: (lambda: BenchSource(scene, 60))
    root = Path(__file__).resolve().parents[1] / "tests" / "data"
    logo = ctx.assets.import_file(root / "logo_alpha.png"); clip = ctx.assets.import_file(root / "clip_h264.mp4")
    r2 = new_rect("Video", 0.1, 0.5, 0.5, 0.9); r2.replacement = ReplacementSpec(enabled=True, asset_id=clip.id).to_dict()
    people = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
                 replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id).to_dict())
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict(), clip.to_dict()]),
                                         b.__setitem__("rois", [r2.to_dict(), people.to_dict()])))
    import gc
    out = []
    for c in range(cycles):
        ctx.start_capture()
        end = time.monotonic() + run_s
        while time.monotonic() < end:
            app.processEvents(); time.sleep(0.01)
        running = phys_footprint() / 2**20
        ctx.stop_capture(); gc.collect(); time.sleep(0.5)
        stopped = phys_footprint() / 2**20
        out.append((round(running), round(stopped)))
        print(f"cycle {c + 1}: running {running:7.1f} MB   after stop {stopped:7.1f} MB", flush=True)
    growth = out[-1][1] - out[1][1]
    print(f"growth after-stop from cycle 2 to {cycles}: {growth:+d} MB")
    ctx.shutdown()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 6)
