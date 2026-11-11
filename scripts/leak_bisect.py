"""Attribute slow memory growth to a specific operation: repeat each soak fault type in isolation
and measure the physical-footprint change per repetition (plus a no-fault control).

    VCS_DISABLE_GLOBAL_HOTKEYS=1 .venv/bin/python scripts/leak_bisect.py [reps] [types...]
"""
import gc
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-bisect-")
os.environ.setdefault("VCS_DISABLE_GLOBAL_HOTKEYS", "1")
ROOT = Path(__file__).resolve().parents[1]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    camera = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--camera=")), None)   # "Device|Sink"
    out_name = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--out=")), "leak_bisect.json")
    reps = int(args[0]) if args else 20
    if camera and "--no-client" in sys.argv:
        print("camera output on, in-process client OFF", flush=True)
    only = args[1:]
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication(sys.argv)  # noqa: F841
    from vcstudio.app.application import AppContext
    from vcstudio.core.bench_source import BenchSource, bus_scene
    from vcstudio.core.performance import phys_footprint
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI, new_rect
    ctx = AppContext(configure_logging=False)
    scene = bus_scene()
    holder = {}

    def factory():
        holder["s"] = BenchSource(scene, 60)
        return holder["s"]
    ctx.settings.set("input.method", "test_pattern"); ctx.settings.set("output.camera_enabled", bool(camera))
    client = None
    if camera:
        dev, sink = camera.split("|")
        ctx.settings.set("output.camera_device", dev); ctx.settings.set("output.camera_sink", sink)
    ctx.settings.set("input.missing_input_timeout_ms", 400)
    ctx.capture_factory = lambda: factory
    logo = ctx.assets.import_file(ROOT / "tests/data/logo_alpha.png")
    clip = ctx.assets.import_file(ROOT / "tests/data/clip_h264.mp4")
    img = new_rect("Logo", 0.6, 0.08, 0.9, 0.38); img.rotation = 8
    img.replacement = ReplacementSpec(enabled=True, asset_id=logo.id, feather_px=4).to_dict()
    vid = new_rect("Video", 0.36, 0.48, 0.66, 0.72); vid.replacement = ReplacementSpec(enabled=True, asset_id=clip.id).to_dict()
    obj = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
              replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id, feather_px=2).to_dict())
    eff = new_rect("Grey", 0.3, 0.0, 0.7, 0.3)
    eff.replacement = ReplacementSpec(enabled=True, mode="effect", effect="color", saturation=0.0).to_dict()
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict(), clip.to_dict()]),
                                         b.__setitem__("rois", [img.to_dict(), vid.to_dict(), obj.to_dict(), eff.to_dict()])))
    main_name = ctx.profiles.active_name
    alt = ctx.profiles.get(main_name)["body"]
    alt["rois"] = [ROI(name="Blur", mode="auto", classes=["person"],
                       replacement=ReplacementSpec(enabled=True, mode="effect", effect="pixelate").to_dict()).to_dict()]
    ctx.profiles.create("Alt", alt)
    p = ctx.pipeline

    def settle(s=1.0):
        time.sleep(s)

    def wait_running():
        end = time.monotonic() + 10
        while time.monotonic() < end and ctx.capture.stats().state != "running":
            time.sleep(0.05)

    actions = {
        "control": lambda: settle(2.0),
        "input_stall": lambda: (holder["s"].stalled.set(), settle(1.0), holder["s"].stalled.clear(), wait_running(), settle(0.5)),
        "input_freeze": lambda: (holder["s"].frozen.set(), settle(1.0), holder["s"].frozen.clear(), settle(0.5)),
        "passthrough": lambda: (p.set_passthrough(True, "x"), settle(1.0), p.set_passthrough(False), settle(0.5)),
        "profile_switch": lambda: (ctx.profiles.switch("Alt"), settle(1.5), ctx.profiles.switch(main_name), settle(1.5)),
        "ai_off": lambda: (ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", False)), settle(0.7),
                           ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", True)), settle(1.0)),
        "pause": lambda: (p.set_paused(True), settle(0.7), p.set_paused(False), settle(0.5)),
        "roi_edit": lambda: (ctx.update_active_profile(lambda b: [r.__setitem__("cx", r["cx"] + 0.001) for r in b["rois"]
                                                                  if r["name"] == "Logo"]), settle(0.5)),
        "cpu_backend": lambda: (ctx.settings.set("processing.compositor", "cpu"), settle(1.0),
                                ctx.settings.set("processing.compositor", "auto"), settle(0.5)),
    }
    def all_faults():
        for k in ("input_stall", "input_freeze", "passthrough", "profile_switch", "ai_off", "pause", "roi_edit", "cpu_backend"):
            actions[k]()
    actions["all"] = all_faults          # one full soak fault cycle, back to back (accelerated aging)
    names = only or [k for k in actions if k != "all"]
    ctx.start_capture()
    if camera and "--no-client" not in sys.argv:  # independent client watching the output camera
        from vcstudio.core.capture_supervisor import CaptureSupervisor
        from vcstudio.core.video_capture import AVFoundationSource, request_camera_access
        request_camera_access(60)
        client = CaptureSupervisor(missing_timeout_ms=4000)
        client.start(lambda: AVFoundationSource(None, dev, 1920, 1080, 60, allow_own_output=True))
    time.sleep(8)
    results = {}
    for name in names:
        gc.collect(); time.sleep(1.5)
        before = phys_footprint() / 2**20
        t0 = time.monotonic()
        curve = []
        for i in range(reps):
            actions[name]()
            if (i + 1) % 10 == 0:
                gc.collect(); time.sleep(0.5)
                curve.append(round(phys_footprint() / 2**20))
        print(f"   {name} curve every 10 reps: {curve}", flush=True)
        results.setdefault("curves", {})[name] = curve
        gc.collect(); time.sleep(2.0)
        after = phys_footprint() / 2**20
        results[name] = {"reps": reps, "seconds": round(time.monotonic() - t0, 1), "before_mb": round(before, 1),
                         "after_mb": round(after, 1), "per_rep_mb": round((after - before) / reps, 2)}
        print(f"{name:15s} {before:7.1f} -> {after:7.1f} MB   {results[name]['per_rep_mb']:+6.2f} MB/rep "
              f"({results[name]['seconds']} s)", flush=True)
    if client:
        cs = client.stats()
        results["camera_client"] = {"state": cs.state, "fps": round(cs.measured_fps, 1), "frames": cs.frames}
        print("camera client:", results["camera_client"], "| camera sent/dropped:", ctx.pipeline.stats().camera_sent,
              ctx.pipeline.stats().camera_dropped, flush=True)
        client.stop()
    ctx.stop_capture(); ctx.shutdown()
    (ROOT / "build" / out_name).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
