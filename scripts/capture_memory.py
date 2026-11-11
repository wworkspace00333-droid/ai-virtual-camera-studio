"""Measure footprint over time while capturing a real AVFoundation device (default OBS Virtual Camera).

    open -W -n "build/AI Virtual Camera Studio Dev.app" --args --run-script capture_memory.py 180 out.json
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 180
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("capture_memory.json")
    device = sys.argv[3] if len(sys.argv) > 3 else "OBS Virtual Camera"
    import gc
    from vcstudio.core.capture_supervisor import CaptureSupervisor
    from vcstudio.core.performance import phys_footprint
    from vcstudio.core.video_capture import AVFoundationSource, request_camera_access
    request_camera_access(60)
    sup = CaptureSupervisor()
    sup.start(lambda: AVFoundationSource(None, device, 1920, 1080, 60))
    time.sleep(5)
    samples = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        time.sleep(10)
        gc.collect()
        st = sup.stats()
        samples.append({"t": round(time.monotonic() - t0), "footprint_mb": round(phys_footprint() / 2**20, 1),
                        "fps": round(st.measured_fps, 1), "frames": st.frames})
    sup.stop()
    fp = [s["footprint_mb"] for s in samples]
    res = {"device": device,
           "samples": samples, "growth_mb": round(fp[-1] - fp[0], 1),
           "growth_mb_per_min": round((fp[-1] - fp[0]) / max(1e-6, (samples[-1]["t"] - samples[0]["t"]) / 60), 2)}
    out.write_text(json.dumps(res, indent=2))
    print(json.dumps({k: v for k, v in res.items() if k != "samples"}))


if __name__ == "__main__":
    main()
