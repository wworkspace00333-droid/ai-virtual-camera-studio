"""Headless self-tests that need real hardware/permissions (run through the dev app bundle).

  open -W -n "build/AI Virtual Camera Studio Dev.app" --args --capture-selftest /path/out.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from vcstudio.core.frame import host_time_s


def capture_selftest(out_path: str, seconds: float = 10.0, name_hint: str = "OBS Virtual Camera",
                     ai_mode: str | None = None) -> int:
    from vcstudio.core.capture_supervisor import CaptureSupervisor
    from vcstudio.core.devices import camera_authorization_status, list_video_devices
    from vcstudio.core.video_capture import AVFoundationSource, request_camera_access

    result: dict = {"started": time.time(), "device_hint": name_hint, "ok": False}
    try:
        result["permission_before"] = camera_authorization_status()
        result["permission_granted"] = request_camera_access(timeout=120)
        result["devices"] = [d.name for d in list_video_devices()]
        if not result["permission_granted"]:
            result["error"] = "camera permission not granted"
            return 2
        events: list = []
        sup = CaptureSupervisor(event_sink=lambda lv, c, m: events.append([round(time.time(), 3), lv, m]))
        sup.start(lambda: AVFoundationSource(None, name_hint, 1920, 1080, 60))
        eng = None
        if ai_mode:
            import tempfile
            from vcstudio.ai.detection import DetectionConfig
            from vcstudio.ai.engine import AIEngine
            from vcstudio.ai.model_manager import ModelManager
            eng = AIEngine(sup, ModelManager(Path(tempfile.mkdtemp())))
            eng.configure(DetectionConfig(confidence=0.25, face_detection=True), ai_mode)
            eng.start()
        samples, preview_ms, means = [], [], []
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            time.sleep(1.0)
            s = sup.stats()
            sample = {"t": round(time.monotonic() - (t_end - seconds), 1), "state": s.state,
                      "fps": round(s.measured_fps, 2), "latency_ms": round(s.latency_ms, 2)}
            if eng is not None:
                a = eng.stats()
                res = eng.results.peek()[0]
                sample.update(ai_state=a.state, ai_fps=round(a.achieved_fps, 1), ai_ms=round(a.inference_ms_avg, 2),
                              ai_p95=round(a.inference_ms_p95, 2), face_ms=round(a.face_ms_avg, 2),
                              ai_age_ms=round(a.frame_age_ms, 1), model=a.model,
                              labels=sorted(d.label for d in res.detections) if res else [])
            samples.append(sample)
            f, _ = sup.latest()
            if f is not None:
                t0 = host_time_s()
                small = f.downscaled_copy(960)
                preview_ms.append((host_time_s() - t0) * 1000)
                means.append(float(small[..., :3].mean()))
        s = sup.stats()
        if eng is not None:
            result["ai_final"] = eng.stats().as_dict()
            eng.stop()
        sup.stop()
        steady = [x["fps"] for x in samples[2:]] or [0]
        result.update(final=s.as_dict(), samples=samples, events=events,
                      preview_convert_ms_avg=round(sum(preview_ms) / max(1, len(preview_ms)), 2),
                      mean_pixel_values=[round(m, 1) for m in means],
                      steady_fps_min=min(steady), steady_fps_avg=round(sum(steady) / len(steady), 2))
        result["ok"] = s.frames > 0 and s.width > 0
        return 0 if result["ok"] else 1
    except Exception as e:  # report, don't crash
        import traceback
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
        return 1
    finally:
        result["finished"] = time.time()
        Path(out_path).write_text(json.dumps(result, indent=2))


def camera_loopback_selftest(out_path: str, seconds: float = 6.0, device: str = "AI Virtual Camera",
                             sink: str = "AI Virtual Camera Input", fps: float = 60.0) -> int:
    """Send bar-coded frames into a virtual camera's sink and capture them back via AVFoundation.

    Measures what a third-party app would receive: delivered frames, frame rate, latency, order.
    """
    import sys
    import threading
    import numpy as np
    from vcstudio.camera.output import VirtualCameraOutput, list_cmio_devices, list_streams
    from vcstudio.core.capture_supervisor import CaptureSupervisor
    from vcstudio.core.frame import Frame
    from vcstudio.core.pixelbuffer import PixelBufferPool
    from vcstudio.core.video_capture import AVFoundationSource, request_camera_access
    from vcstudio.core.barcode import BITS

    W, H = 1920, 1080
    cell = W // BITS
    result: dict = {"device": device, "sink": sink, "ok": False}
    sent_at: dict[int, float] = {}
    got: list[tuple[int, float]] = []
    try:
        result["permission"] = request_camera_access(timeout=120)
        result["cmio_devices"] = list_cmio_devices()
        result["streams"] = list_streams(device)
        out = VirtualCameraOutput(device, sink)
        out.open()
        pool = PixelBufferPool(W, H)
        base = np.zeros((H, W, 4), np.uint8); base[..., 3] = 255; base[..., 1] = 90

        def decode(a) -> int:
            row = a[H - 40, :, :3].astype(int).mean(axis=1)
            return sum(1 << b for b in range(BITS) if row[int((b + 0.5) * cell)] > 127)

        sup = CaptureSupervisor(missing_timeout_ms=3000)
        def on_frame_watch():
            last = -1
            while not stop.is_set():
                f, v = sup.slot.wait_newer(last, 0.5)
                if f is None or v == last:
                    continue
                last = v
                with f.bgra() as a:
                    got.append((decode(a), host_time_s()))
        stop = threading.Event()
        sup.start(lambda: AVFoundationSource(None, device, W, H, 60, allow_own_output=True))
        watcher = threading.Thread(target=on_frame_watch, daemon=True); watcher.start()
        time.sleep(1.5)                                   # let the client stream start
        t0 = time.perf_counter()
        n = 0
        while time.perf_counter() - t0 < seconds:
            idx = (n % 1000) + 1
            img = base.copy()
            for b in range(BITS):
                img[H - 80:, b * cell:(b + 1) * cell, :3] = 255 if (idx >> b) & 1 else 0
            pb = pool.from_array(img)
            now = host_time_s()
            out.send(pb, now)
            sent_at[idx] = now
            n += 1
            nxt = t0 + n / fps
            time.sleep(max(0.0, nxt - time.perf_counter()))
        time.sleep(0.5)
        stop.set(); watcher.join(2)
        cap = sup.stats(); sup.stop()
        sent, dropped = out.stats(); out.close()
        uniq = sorted({i for i, _ in got if i in sent_at})
        lat = [(t - sent_at[i]) * 1000 for i, t in got if i in sent_at and t >= sent_at[i]]
        first = {}
        for i, t in got:
            first.setdefault(i, t)
        order_ok = all(a < b for a, b in zip(uniq, uniq[1:]))
        result.update(sent=sent, dropped_by_sender=dropped, distinct_received=len(uniq), captured_frames=len(got),
                      delivery_ratio=round(len(uniq) / max(1, sent), 3), capture_fps=round(cap.measured_fps, 1),
                      capture_state=cap.state, latency_ms_median=round(float(np.median(lat)), 2) if lat else None,
                      latency_ms_p95=round(float(np.percentile(lat, 95)), 2) if lat else None,
                      order_ok=order_ok, capture_resolution=f"{cap.width}x{cap.height}")
        result["ok"] = len(uniq) > 0.5 * sent and order_ok
        return 0 if result["ok"] else 1
    except Exception as e:
        import traceback
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
        return 1
    finally:
        Path(out_path).write_text(json.dumps(result, indent=2))


def pipeline_e2e_selftest(out_path: str, seconds: float = 8.0, out_device: str = "AI Virtual Camera",
                          out_sink: str = "AI Virtual Camera Input", in_device: str | None = None) -> int:
    """Full chain: input -> AI detection -> tracking -> object replacement -> Metal -> CMIO camera -> client capture.

    Input: `in_device` (e.g. OBS Virtual Camera) or, by default, a synthetic 1080p60 bus scene whose bottom rows
    carry a frame-index bar code (so the client side can measure latency and order). The replacement paints the
    silhouettes of people magenta; the client verifies magenta pixels where people are.
    """
    import os
    import sys
    import tempfile
    import threading
    import numpy as np
    import cv2
    os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-e2e-")
    from vcstudio.ai.testimages import load_bgra
    from vcstudio.app.application import AppContext
    from vcstudio.core.capture_supervisor import CaptureSupervisor
    from vcstudio.core.frame import Frame
    from vcstudio.core.video_capture import AVFoundationSource, TestPatternSource, request_camera_access
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI
    from vcstudio.core.barcode import BITS

    W, H = 1920, 1080
    cell = W // BITS
    result: dict = {"out_device": out_device, "in_device": in_device or "synthetic bus 1080p60", "ok": False}
    ctx = None
    try:
        result["permission"] = request_camera_access(timeout=120)
        ctx = AppContext(configure_logging=False)
        from vcstudio.core.resources import sample
        bus = load_bgra(sample("bus.jpg"))
        scene = np.zeros((H, W, 4), np.uint8); scene[..., 3] = 255
        scene[:, 555:555 + 810] = cv2.resize(bus, (810, 1080))

        class BarCodedScene(TestPatternSource):
            def _run(self, on_frame):
                n, t0 = 0, time.perf_counter()
                while not self._stop.is_set():
                    n += 1
                    img = scene.copy()
                    idx = n % 1000
                    for b in range(BITS):
                        img[H - 60:, b * cell:(b + 1) * cell, :3] = 255 if (idx >> b) & 1 else 0
                    now = host_time_s()
                    on_frame(Frame(n, now, now, W, H, array=img, source="bus"))
                    d = t0 + n / self.fps - time.perf_counter()
                    if d > 0:
                        self._stop.wait(d)
        ctx.settings.set("output.camera_device", out_device)
        ctx.settings.set("output.camera_sink", out_sink)
        people = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
                     replacement=ReplacementSpec(enabled=True, mode="object", asset_id="", background=[255, 0, 255, 255]).to_dict())
        # object mode needs an asset: use a fully transparent 2x2 PNG so only the magenta fill shows
        tp = Path(os.environ["VCSTUDIO_HOME"]) / "clear.png"
        cv2.imwrite(str(tp), np.zeros((2, 2, 4), np.uint8))
        a = ctx.assets.import_file(tp)
        people.replacement["asset_id"] = a.id
        ctx.update_active_profile(lambda b: (b.__setitem__("assets", [a.to_dict()]), b.__setitem__("rois", [people.to_dict()]),
                                            b["detection"].__setitem__("frequency_mode", "fixed"),
                                            b["detection"].__setitem__("fixed_fps", 20)))
        obs_in = in_device == "obs"
        if obs_in:
            # real OBS content through the vcstudio-obs plugin; the scene is unknown, so a fixed manual region
            # filled with an opaque magenta image is what the client checks (top-left 5%..25% of the frame)
            from vcstudio.roi.model import new_rect
            mp = Path(os.environ["VCSTUDIO_HOME"]) / "magenta.png"
            cv2.imwrite(str(mp), np.full((8, 8, 4), (255, 0, 255, 255), np.uint8))
            ma = ctx.assets.import_file(mp)
            marker = new_rect("Marker", 0.05, 0.05, 0.25, 0.25)
            marker.replacement = ReplacementSpec(enabled=True, asset_id=ma.id, fit="stretch").to_dict()
            ctx.update_active_profile(lambda b: (b.__setitem__("assets", b["assets"] + [ma.to_dict()]),
                                                b.__setitem__("rois", b["rois"] + [marker.to_dict()])))
            ctx.settings.set("input.method", "obs_ipc")
        elif in_device:
            ctx.settings.set("input.method", "avfoundation"); ctx.settings.set("input.device_name_hint", in_device)
            ctx.settings.set("input.device_unique_id", None)
        else:
            ctx.settings.set("input.method", "test_pattern")          # synthetic input (not a camera device)
            ctx.capture_factory = lambda: (lambda: BarCodedScene(W, H, 60))
        ctx.start_capture()
        # ---- third-party client: capture the output camera
        client = CaptureSupervisor(missing_timeout_ms=3000)
        client.start(lambda: AVFoundationSource(None, out_device, W, H, 60, allow_own_output=True))
        got: list[tuple[int, float, float]] = []
        stop = threading.Event()

        def watch():
            last = -1
            while not stop.is_set():
                f, v = client.slot.wait_newer(last, 0.5)
                if f is None or v == last:
                    continue
                last = v
                with f.bgra() as img:
                    row = img[H - 30, :, :3].astype(int).mean(axis=1)
                    idx = sum(1 << b for b in range(BITS) if row[int((b + 0.5) * cell)] > 127)
                    if obs_in:      # centre of the marker region, scaled to the client's frame size
                        hh, ww = img.shape[:2]
                        person = img[int(hh * .1):int(hh * .2), int(ww * .1):int(ww * .2), :3].reshape(-1, 3).astype(int)
                    else:
                        person = img[620:860, 650:800, :3].reshape(-1, 3).astype(int)  # left person's torso (bus scene)
                    magenta = float(((person[:, 0] > 200) & (person[:, 1] < 60) & (person[:, 2] > 200)).mean())
                got.append((idx, host_time_s(), magenta))
        th = threading.Thread(target=watch, daemon=True); th.start()
        time.sleep(seconds)
        stop.set(); th.join(2)
        ps, cs, ai = ctx.pipeline.stats(), client.stats(), ctx.ai.stats()
        ctx_capture_fps = ctx.capture.stats().measured_fps
        client.stop()
        ctx.stop_capture()
        steady = got[len(got) // 3:]
        mag = [m for _, _, m in steady]
        idxs = [i for i, _, _ in steady]
        uniq = len(set(idxs))
        dur = (steady[-1][1] - steady[0][1]) if len(steady) > 1 else 1
        result.update(
            pipeline_state=ps.state, pipeline_fps=round(ps.output_fps, 1), pipeline_latency_ms=round(ps.latency_ms, 2),
            pipeline_latency_p95_ms=round(ps.latency_ms_p95, 2), process_ms=round(ps.process_ms, 2), backend=ps.backend,
            render_errors=ps.render_errors, camera_state=ps.camera_state, camera_sent=ps.camera_sent,
            camera_dropped=ps.camera_dropped, ai_model=ai.model, ai_fps=round(ai.achieved_fps, 1),
            ai_ms=round(ai.inference_ms_avg, 2), tracks=ai.tracks_active,
            client_frames=len(got),
            client_distinct_fps=round((len(steady) if obs_in else uniq) / max(dur, 1e-3), 1),
            input_fps=round(ctx_capture_fps, 1),
            client_resolution=f"{cs.width}x{cs.height}",
            magenta_fraction_median=round(float(np.median(mag)), 3) if mag else 0.0,
            frames_with_replacement=round(float(np.mean([m > 0.3 for m in mag])), 3) if mag else 0.0)
        result["ok"] = (ps.camera_state == "connected" and len(got) > 0 and result["frames_with_replacement"] > 0.9
                        and ps.render_errors == 0)
        return 0 if result["ok"] else 1
    except Exception as e:
        import traceback
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
        return 1
    finally:
        if ctx is not None:
            try:
                ctx.shutdown()
            except Exception:
                pass
        Path(out_path).write_text(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------------------------- self-check
def bundle_selfcheck(out: str | None = None) -> int:
    """Check every bundled component really works in this installation (packaged or source tree).

    Loads each Core ML model and runs it on the sample image, creates a Metal context, loads the
    shared-memory reader, looks for the OBS plugin and sample data. Prints one line per check and
    returns 0 only if all required checks pass. Optional components (OBS plugin, ffmpeg) are reported
    but never fail the check.
    """
    import json
    import shutil
    import sys
    import tempfile
    import time

    from vcstudio.ai.coreml_engine import CoreMLDetector, VisionFaceDetector
    from vcstudio.ai.model_manager import ModelManager
    from vcstudio.ai.segmentation import SegmentationDetector
    from vcstudio.ai.testimages import load_bgra
    from vcstudio.core.frame import Frame
    from vcstudio.core.pyobjc_prewarm import prewarm
    from vcstudio.core.resources import bundle_root, frozen, models_dir, sample

    prewarm()
    results: list[dict] = []

    def check(name: str, fn, required: bool = True) -> None:
        t = time.perf_counter()
        try:
            detail = fn() or ""
            ok = True
        except Exception as e:  # noqa: BLE001 - report every failure
            detail, ok = f"{type(e).__name__}: {e}", False
        ms = (time.perf_counter() - t) * 1000
        results.append({"check": name, "ok": ok, "required": required, "detail": str(detail), "ms": round(ms, 1)})
        tag = "PASS" if ok else ("FAIL" if required else "WARN")
        print(f"[{tag}] {name:<28} {detail}  ({ms:.0f} ms)", flush=True)

    print(f"AI Virtual Camera Studio self-check — {'packaged' if frozen() else 'source tree'}; "
          f"resources: {bundle_root()}", flush=True)

    def samples():
        missing = [n for n in ("bus.jpg", "logo_alpha.png", "clip_h264.mp4") if not sample(n).exists()]
        if missing:
            raise FileNotFoundError(", ".join(missing))
        return "bus.jpg, logo_alpha.png, clip_h264.mp4"
    check("sample data", samples)

    img = load_bgra(sample("bus.jpg"))
    frame = Frame(1, 0, 0, img.shape[1], img.shape[0], array=img)
    mm = ModelManager(Path(tempfile.mkdtemp(prefix="vcs-selfcheck-")))
    infos = mm.available()

    def models_present():
        if not infos:
            raise FileNotFoundError(f"no Core ML models in {models_dir()}")
        return ", ".join(sorted(infos))
    check("models", models_present)

    for name, info in sorted(infos.items()):
        def run(info=info):
            loaded = mm.load(info)
            det = SegmentationDetector(loaded) if info.task == "segment" else CoreMLDetector(loaded)
            det.detect(frame)                          # warm-up (first call compiles / plans)
            dets, ms = det.detect(frame)
            people = sum(d.label == "person" for d in dets)
            if people < 3:
                raise AssertionError(f"expected >= 3 people in bus.jpg, got {people} ({len(dets)} objects)")
            mm.unload(info.name)
            return f"{len(dets)} objects, {people} people, {ms:.1f} ms/frame"
        check(f"model {name}", run)

    def faces():
        dets, ms = VisionFaceDetector().detect(frame)
        return f"{len(dets)} faces, {ms:.1f} ms"
    check("Vision face detection", faces)

    def metal():
        from vcstudio.native import metal as m
        ctx = m.MetalContext()
        name = ctx.device_name
        ctx.close()
        return name
    check("Metal compositor", metal)

    def shm():
        from vcstudio.core.obs_shm_source import obs_sharing_status, shm_lib
        if shm_lib() is None:
            raise RuntimeError("libVCSShm.dylib not loadable")
        st = obs_sharing_status()
        return "reader OK; OBS sharing: " + ("live" if st["available"] else f"off ({st['reason'][:40]})")
    check("OBS shared-memory reader", shm)

    def plugin():
        from vcstudio.core.obs_plugin import built_plugin, installed_plugin
        p = built_plugin()
        if p is None:
            raise FileNotFoundError("plugin not bundled (scripts/build_obs_plugin.sh)")
        return f"bundled; installed in OBS: {'yes' if installed_plugin() else 'no'}"
    check("OBS plugin", plugin, required=False)

    def ffmpeg():
        p = shutil.which("ffmpeg") or next((c for c in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                                            if Path(c).exists()), None)
        if not p:
            raise FileNotFoundError("ffmpeg not found (only needed for MKV/WebM replacement videos)")
        return p
    check("ffmpeg (optional)", ffmpeg, required=False)

    failed = [r for r in results if r["required"] and not r["ok"]]
    print(f"{'OK' if not failed else 'FAILED'}: {sum(r['ok'] for r in results)}/{len(results)} checks passed",
          flush=True)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps({"frozen": frozen(), "python": sys.version, "results": results}, indent=2))
    return 1 if failed else 0
