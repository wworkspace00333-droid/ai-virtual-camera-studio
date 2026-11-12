"""Long run of the real GUI on the real OBS video (developer tool).

The full window runs with OBS input (vcstudio-obs plugin) and OBS Virtual Camera output, Hide face and a game
card on. Every 5 s it switches to another page (what a person clicking around does), a 50 ms UI timer
measures event-loop stalls (GUI hangs), and every 10 s it samples memory, output fps and capture state. OBS is
quit and reopened at the given minutes to measure reconnection. Writes <out>/samples.jsonl, events.jsonl and
summary.json.

    open -W -n "build/AI Virtual Camera Studio Dev.app" --args --run-script gui_soak.py --out <dir> \\
        [--minutes 60] [--obs-restarts 20,40]

Close the app's own window first (both would feed the same camera).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    a = sys.argv[1:]
    out = Path(a[a.index("--out") + 1]); out.mkdir(parents=True, exist_ok=True)
    minutes = float(a[a.index("--minutes") + 1]) if "--minutes" in a else 60.0
    restarts = [float(x) for x in a[a.index("--obs-restarts") + 1].split(",")] if "--obs-restarts" in a else [20.0, 40.0]
    home = Path(tempfile.mkdtemp(prefix="vcs-guisoak-"))
    real = Path.home() / "Library" / "Application Support" / "AIVirtualCameraStudio"
    if (real / "settings.json").exists():                       # the user's own settings and profiles, as a copy
        shutil.copy(real / "settings.json", home / "settings.json")
        shutil.copytree(real / "profiles", home / "profiles")
    os.environ["VCSTUDIO_HOME"] = str(home)
    os.environ["VCS_DISABLE_GLOBAL_HOTKEYS"] = "1"
    os.environ["VCS_NO_AUTOSTART"] = "1"

    import psutil
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from vcstudio.app.application import AppContext
    from vcstudio.game import cardframe as cf
    from vcstudio.i18n import set_language
    from vcstudio.roi.model import set_quick_face
    from vcstudio.ui.main_window import MainWindow

    from vcstudio.ui.theme import QSS
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(QSS)                 # same look as the real app (main.py applies the theme too)
    set_language("ru")
    ctx = AppContext()
    s = ctx.settings
    s.set("input.method", "obs_ipc")
    s.set("output.camera_device", "OBS Virtual Camera")
    s.set("output.camera_sink", "OBS Camera Extension Stream Sink")
    s.set("output.pixel_format", "NV12")
    img = home / "card.png"
    import cv2
    import numpy as np
    cv2.imwrite(str(img), np.full((300, 200, 3), (30, 160, 30), np.uint8))
    asset = ctx.assets.import_file(img).to_dict()

    def mut(b):
        b["assets"].append(asset)
        set_quick_face(b, True)
        cf.add_cards(b, [asset["id"]])
        cf.set_game_card(b, slot=1, rect=(0.75, 0.4, 0.15, 0.35, 0.0), asset_id=asset["id"], fit="stretch")
    ctx.update_active_profile(mut)
    win = MainWindow(ctx)
    win.resize(1360, 860)
    win.show()
    ctx.start_capture()

    proc = psutil.Process()
    t0 = time.monotonic()
    state = {"last_tick": time.monotonic(), "max_stall": 0.0, "stalls_over_250": 0, "page": 0, "restart_i": 0,
             "obs_down_at": None, "obs_up_at": None, "events": []}
    samples = open(out / "samples.jsonl", "w")
    events = open(out / "events.jsonl", "w")

    def event(kind, **kw):
        row = {"t": round(time.monotonic() - t0, 1), "event": kind, **kw}
        events.write(json.dumps(row) + "\n"); events.flush()
        state["events"].append(row)

    def tick():                                             # UI responsiveness: gap between 50 ms timer ticks
        now = time.monotonic()
        gap = now - state["last_tick"] - 0.05
        state["last_tick"] = now
        if gap > state["max_stall"]:
            state["max_stall"] = gap
        if gap > 0.25:
            state["stalls_over_250"] += 1
            event("ui_stall", ms=round(gap * 1000), page=win.page_keys()[state["page"]])

    def footprint_mb():
        try:
            r = subprocess.run(["/usr/bin/footprint", "-p", str(os.getpid()), "--noCategories"], capture_output=True,
                               text=True, timeout=10).stdout
            for ln in r.splitlines():
                if "Footprint:" in ln or "phys_footprint:" in ln:
                    v = ln.split(":")[-1].strip().split()
                    num = float(v[0]); unit = v[1].upper() if len(v) > 1 else "MB"
                    return round(num / 1024 if unit.startswith("K") else num * 1024 if unit.startswith("G") else num, 1)
        except Exception:  # noqa: BLE001
            pass
        return None

    import threading
    fp_box = {"mb": None}

    def fp_worker():                                        # off the UI thread: `footprint` takes ~0.3 s
        while True:
            fp_box["mb"] = footprint_mb()
            time.sleep(10)
    threading.Thread(target=fp_worker, daemon=True).start()

    def sample():
        el = time.monotonic() - t0
        p, c, e = ctx.pipeline.stats(), ctx.capture.stats(), ctx.ai.stats()
        row = {"t": round(el, 1), "rss_mb": round(proc.memory_info().rss / 2**20, 1), "footprint_mb": fp_box["mb"],
               "threads": proc.num_threads(), "fds": proc.num_fds(), "cpu_pct": proc.cpu_percent(None),
               "capture": c.state, "input_fps": round(c.measured_fps, 1), "output_fps": round(p.output_fps, 1),
               "latency_ms": round(p.latency_ms, 2), "render_errors": p.render_errors, "camera": p.camera_state,
               "camera_sent": p.camera_sent, "camera_dropped": p.camera_dropped, "ai_state": e.state,
               "ai_fps": round(e.achieved_fps, 1), "ai_errors": e.errors, "regions": p.regions,
               "max_stall_ms": round(state["max_stall"] * 1000), "page": win.page_keys()[state["page"]]}
        samples.write(json.dumps(row) + "\n"); samples.flush()
        # OBS restarts and recovery
        if state["obs_down_at"] is None and state["restart_i"] < len(restarts) and el >= restarts[state["restart_i"]] * 60:
            state["restart_i"] += 1
            subprocess.Popen(["osascript", "-e", 'tell application "OBS" to quit'])   # async: never block the UI being measured
            state["obs_down_at"] = time.monotonic()
            event("obs_quit")
        elif state["obs_down_at"] is not None and state["obs_up_at"] is None and time.monotonic() - state["obs_down_at"] > 15:
            subprocess.Popen(["open", "-g", "-a", "OBS"])
            state["obs_up_at"] = time.monotonic()
            event("obs_open", capture=c.state)
        elif state["obs_up_at"] is not None:
            if c.state == "running" and c.measured_fps > 5 and p.output_fps > 5:
                event("recovered", seconds_after_obs_open=round(time.monotonic() - state["obs_up_at"], 1))
                state["obs_down_at"] = state["obs_up_at"] = None
            elif time.monotonic() - state["obs_up_at"] > 120:
                event("not_recovered", capture=c.state)
                state["obs_down_at"] = state["obs_up_at"] = None
        if el >= minutes * 60:
            finish()

    def next_page():
        keys = win.page_keys()
        state["page"] = (state["page"] + 1) % len(keys)
        win.goto(keys[state["page"]])

    def finish():
        for tmr in timers:
            tmr.stop()
        rows = [json.loads(x) for x in open(out / "samples.jsonl")]
        steady = [r for r in rows if r["capture"] == "running" and r["t"] > 60]
        fp = [r["footprint_mb"] for r in rows if r["footprint_mb"]]
        q = max(1, len(fp) // 4)
        summary = {"minutes": minutes, "samples": len(rows), "max_ui_stall_ms": round(state["max_stall"] * 1000),
                   "ui_stalls_over_250ms": state["stalls_over_250"],
                   "output_fps_median": sorted(r["output_fps"] for r in steady)[len(steady) // 2] if steady else None,
                   "input_fps_median": sorted(r["input_fps"] for r in steady)[len(steady) // 2] if steady else None,
                   "render_errors": rows[-1]["render_errors"], "ai_errors": rows[-1]["ai_errors"],
                   "camera_sent": rows[-1]["camera_sent"], "camera_dropped": rows[-1]["camera_dropped"],
                   "footprint_first_quarter_mb": round(sum(fp[:q]) / q, 1) if fp else None,
                   "footprint_last_quarter_mb": round(sum(fp[-q:]) / q, 1) if fp else None,
                   "footprint_max_mb": max(fp) if fp else None,
                   "threads_min_max": [min(r["threads"] for r in rows), max(r["threads"] for r in rows)],
                   "fds_min_max": [min(r["fds"] for r in rows), max(r["fds"] for r in rows)],
                   "obs_restarts": [e for e in state["events"] if e["event"] in ("obs_quit", "obs_open", "recovered", "not_recovered")]}
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        ctx.shutdown()
        app.quit()

    timers = []
    for ms, fn in ((50, tick), (10000, sample), (5000, next_page)):
        t = QTimer(); t.timeout.connect(fn); t.start(ms); timers.append(t)
    event("start", home=str(home))
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
