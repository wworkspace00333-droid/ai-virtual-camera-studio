"""Long-running stability (soak) test with scheduled fault injection.

Runs the real pipeline (AI -> tracking -> ROIs -> Metal -> CMIO camera) for N minutes while a fault
schedule repeats every 5 minutes, and an independent AVFoundation client watches the output camera.
Samples are written every 5 s to <out>/samples.jsonl; events to <out>/events.jsonl; a summary to
<out>/summary.json (see scripts/analyse_soak.py).

    open -W -n "build/AI Virtual Camera Studio Dev.app" --args --soak <out_dir> --minutes 120 \
        [--device "AI Virtual Camera" --sink "AI Virtual Camera Input"] [--input "OBS Virtual Camera"]

Input: the zero-copy benchmark scene (default) or a real camera (--input). Output: the given CMIO
camera (default AI Virtual Camera; falls back to running without camera output if absent).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

CYCLE_S = 300.0

# (offset in cycle, action, duration) ; recovery is verified after each fault
SCHEDULE = [
    (30, "input_stall", 3.0),         # missing input -> no_signal -> reconnect
    (60, "input_freeze", 4.0),        # frozen frames detected, no reconnect
    (90, "passthrough", 5.0),
    (120, "profile_switch", 30.0),    # switch to the alternative profile and back
    (180, "ai_off", 5.0),
    (210, "pause", 3.0),
    (240, "roi_edit", 0.0),           # move a manual ROI
    (270, "cpu_backend", 10.0),       # compositor on CPU then back to Metal
]


def run_soak(out_dir: str, minutes: float, out_device: str | None, out_sink: str | None,
             in_device: str | None = None) -> int:
    import psutil
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Forensics: native crashes dump all thread stacks; termination signals are recorded.
    import faulthandler
    import signal
    import traceback as _tb
    crash_f = open(out / "fatal.txt", "a")
    faulthandler.enable(crash_f, all_threads=True)

    def on_term(sig, frame):
        with open(out / "fatal.txt", "a") as f:
            f.write(f"received signal {sig} at {time.strftime('%H:%M:%S')}\n")
            f.write("".join(_tb.format_stack(frame)))
        os._exit(128 + sig)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_term)
    os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-soak-")
    from vcstudio.app.application import AppContext
    from vcstudio.camera.output import list_cmio_devices
    from vcstudio.core.bench_source import BenchSource, bus_scene
    from vcstudio.core.capture_supervisor import CaptureSupervisor
    from vcstudio.core.performance import phys_footprint, read_gpu_stats
    from vcstudio.core.video_capture import AVFoundationSource, request_camera_access
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI, new_rect

    ev_f = open(out / "events.jsonl", "a")
    smp_f = open(out / "samples.jsonl", "a")
    t_start = time.monotonic()

    def event(kind, **kw):
        ev_f.write(json.dumps({"t": round(time.monotonic() - t_start, 2), "event": kind, **kw}) + "\n")
        ev_f.flush()

    request_camera_access(timeout=60)
    ctx = AppContext(configure_logging=True)
    proc = psutil.Process()
    devices = list_cmio_devices()
    camera_on = bool(out_device and out_device in devices)
    ctx.settings.set("output.camera_enabled", camera_on)
    if camera_on:
        ctx.settings.set("output.camera_device", out_device)
        ctx.settings.set("output.camera_sink", out_sink)
    source_holder = {}
    if in_device:
        ctx.settings.set("input.method", "avfoundation")
        ctx.settings.set("input.device_name_hint", in_device)
        ctx.settings.set("input.device_unique_id", None)
    else:
        scene = bus_scene()
        ctx.settings.set("input.method", "test_pattern")

        def factory():
            s = BenchSource(scene, 60)
            source_holder["src"] = s
            return s
        ctx.capture_factory = lambda: factory
    # ---- profiles: main (segmentation + video + image + effect) and alternative (detection + pixelate)
    from vcstudio.core.resources import sample
    logo = ctx.assets.import_file(sample("logo_alpha.png"))
    clip = ctx.assets.import_file(sample("clip_h264.mp4"))
    img = new_rect("Logo", 0.6, 0.08, 0.9, 0.38); img.rotation = 8
    img.replacement = ReplacementSpec(enabled=True, asset_id=logo.id, feather_px=4).to_dict()
    vid = new_rect("Video", 0.36, 0.48, 0.66, 0.72)
    vid.replacement = ReplacementSpec(enabled=True, asset_id=clip.id, fit="fill").to_dict()
    obj = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
              replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id, feather_px=2,
                                          background=[255, 0, 255, 255]).to_dict())
    eff = new_rect("Grey", 0.3, 0.0, 0.7, 0.3)
    eff.replacement = ReplacementSpec(enabled=True, mode="effect", effect="color", saturation=0.0).to_dict()
    assets = [logo.to_dict(), clip.to_dict()]
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", assets),
                                         b.__setitem__("rois", [img.to_dict(), vid.to_dict(), obj.to_dict(), eff.to_dict()])))
    main_name = ctx.profiles.active_name
    alt_body = ctx.profiles.get(main_name)["body"]
    pix = ROI(name="People blur", mode="auto", classes=["person"], padding=0.05,
              replacement=ReplacementSpec(enabled=True, mode="effect", effect="pixelate", pixel_size=16).to_dict())
    alt_body["rois"] = [pix.to_dict()]
    alt_body["performance_mode"] = "performance"
    ctx.profiles.create("Soak alt", alt_body)
    ctx.start_capture()
    event("started", camera=camera_on, out_device=out_device if camera_on else None, input=in_device or "bench")

    # ---- independent client watching the output camera
    client = None
    client_frames = {"n": 0, "distinct": set(), "window_start": time.monotonic()}
    if camera_on:
        client = CaptureSupervisor(missing_timeout_ms=4000)
        client.start(lambda: AVFoundationSource(None, out_device, 1920, 1080, 60, allow_own_output=True))
        stop_client = threading.Event()

        def watch():
            last = -1
            while not stop_client.is_set():
                f, v = client.slot.wait_newer(last, 0.5)
                if f is None or v == last:
                    continue
                last = v
                client_frames["n"] += 1
        threading.Thread(target=watch, daemon=True).start()

    # ---- fault actions
    def fault(action, dur):
        src = source_holder.get("src")
        p = ctx.pipeline
        event("fault_begin", action=action, duration=dur)
        if action == "input_stall" and src:
            src.stalled.set(); time.sleep(dur); src.stalled.clear()
        elif action == "input_freeze" and src:
            src.frozen.set(); time.sleep(dur); src.frozen.clear()
        elif action == "passthrough":
            p.set_passthrough(True, "soak"); time.sleep(dur); p.set_passthrough(False)
        elif action == "profile_switch":
            ctx.profiles.switch("Soak alt"); time.sleep(dur); ctx.profiles.switch(main_name)
        elif action == "ai_off":
            ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", False)); time.sleep(dur)
            ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", True))
        elif action == "pause":
            p.set_paused(True); time.sleep(dur); p.set_paused(False)
        elif action == "roi_edit":
            def mv(b):
                for r in b["rois"]:
                    if r["name"] == "Logo":
                        r["cx"] = 0.75 if r["cx"] < 0.75 else 0.7
            ctx.update_active_profile(mv)
        elif action == "cpu_backend":
            ctx.settings.set("processing.compositor", "cpu"); time.sleep(dur)
            ctx.settings.set("processing.compositor", "auto")
        # recovery check: pipeline producing frames again within 10 s
        t0 = time.monotonic()
        n0 = p.stats().frames_out
        ok = False
        while time.monotonic() - t0 < 10:
            time.sleep(0.25)
            s = p.stats()
            # "frozen" = static content, frames still flowing (e.g. OBS placeholder): counts as recovered
            if s.state == "running" and s.frames_out > n0 + 30 and ctx.capture.stats().state in ("running", "frozen"):
                ok = True
                break
        event("fault_end", action=action, recovered=ok, recovery_s=round(time.monotonic() - t0, 2))

    done = threading.Event()

    def scheduler():
        cycle = 0
        while not done.is_set():
            base = t_start + cycle * CYCLE_S
            for off, action, dur in SCHEDULE:
                while not done.is_set() and time.monotonic() < base + off:
                    time.sleep(0.2)
                if done.is_set():
                    return
                try:
                    fault(action, dur)
                except Exception as e:
                    event("fault_error", action=action, error=f"{type(e).__name__}: {e}")
            cycle += 1
            while not done.is_set() and time.monotonic() < t_start + cycle * CYCLE_S:
                time.sleep(0.2)
    sched = threading.Thread(target=scheduler, daemon=True)
    sched.start()

    # ---- sampling loop
    proc.cpu_percent(None)
    last_client = 0
    end = t_start + minutes * 60
    try:
        while time.monotonic() < end:
            time.sleep(5.0)
            s, a, c = ctx.pipeline.stats(), ctx.ai.stats(), ctx.capture.stats()
            w = ctx.watchdog.stats
            g, gmem = read_gpu_stats()
            cf = client_frames["n"]
            try:
                fds = proc.num_fds()
            except Exception:
                fds = -1
            row = {
                "t": round(time.monotonic() - t_start, 1),
                "footprint_mb": round((phys_footprint() or 0) / 2**20, 1), "rss_mb": round(proc.memory_info().rss / 2**20, 1),
                "cpu_pct": proc.cpu_percent(None), "threads": proc.num_threads(), "fds": fds,
                "gpu_pct": g, "gpu_mem_mb": round(gmem / 2**20) if gmem else None,
                "capture_state": c.state, "capture_fps": round(c.measured_fps, 1), "reconnects": c.reconnects,
                "pipe_state": s.state, "out_fps": round(s.output_fps, 1), "latency_ms": round(s.latency_ms, 2),
                "latency_p95_ms": round(s.latency_ms_p95, 2), "process_ms": round(s.process_ms, 2),
                "backend": s.backend, "render_errors": s.render_errors, "frames_out": s.frames_out,
                "camera_state": s.camera_state, "camera_sent": s.camera_sent, "camera_dropped": s.camera_dropped,
                "client_fps": round((cf - last_client) / 5.0, 1) if client else None,
                "ai_state": a.state, "ai_model": a.model, "ai_fps": round(a.achieved_fps, 1),
                "ai_ms": round(a.inference_ms_avg, 2), "ai_errors": a.errors, "tracks": a.tracks_active,
                "track_ids": a.track_ids_created, "pipeline_restarts": w.pipeline_restarts, "ai_restarts": w.ai_restarts,
                "memory_state": w.memory_state, "video_players": ctx.replacement.compositor.stats.video_players,
                "profile": ctx.profiles.active_name,
            }
            last_client = cf
            smp_f.write(json.dumps(row) + "\n")
            smp_f.flush()
    finally:
        done.set()
        sched.join(15)
        if client:
            stop_client.set()
            client.stop()
        ctx.stop_capture()
        event("finished")
        ctx.shutdown()
        ev_f.close(); smp_f.close()
    return 0


def main_entry(argv) -> int:
    i = argv.index("--soak")
    opt = lambda k, d: argv[argv.index(k) + 1] if k in argv else d
    return run_soak(argv[i + 1], float(opt("--minutes", 120)), opt("--device", "AI Virtual Camera"),
                    opt("--sink", "AI Virtual Camera Input"), opt("--input", None))
