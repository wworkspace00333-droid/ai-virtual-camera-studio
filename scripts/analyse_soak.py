"""Summarise a soak run: stability, recovery, memory slope, resource drift.

    .venv/bin/python scripts/analyse_soak.py <soak_dir>      (writes <soak_dir>/summary.json)
"""
import json
import statistics
import sys
from pathlib import Path


def slope_per_hour(ts, ys):
    n = len(ts)
    if n < 3:
        return 0.0
    mt, my = sum(ts) / n, sum(ys) / n
    den = sum((t - mt) ** 2 for t in ts)
    return 0.0 if den == 0 else sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / den * 3600


def _quarter_median(rows, q):
    if len(rows) < 8:
        return None
    n = len(rows)
    part = rows[q * n // 4:(q + 1) * n // 4]
    return round(statistics.median(r["footprint_mb"] for r in part), 1)


def analyse(d: Path) -> dict:
    rows = [json.loads(l) for l in (d / "samples.jsonl").read_text().splitlines() if l.strip()]
    events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines() if l.strip()]
    warm = [r for r in rows if r["t"] >= 600] or rows            # skip the first 10 min for drift
    faults = [e for e in events if e["event"] == "fault_end"]
    # windows where a fault was deliberately injected (+5 s settling and one 5 s sample period)
    windows = []
    begin = {}
    for e in events:
        if e["event"] == "fault_begin":
            begin[e["action"]] = e["t"]
        elif e["event"] == "fault_end" and e["action"] in begin:
            windows.append((begin.pop(e["action"]) - 5.0, e["t"] + 10.0))
    in_fault = lambda t: any(a <= t <= b for a, b in windows)
    # "frozen" = identical content (e.g. a static scene) but frames still flowing at full rate
    running = [r for r in rows if r["pipe_state"] == "running" and r["capture_state"] in ("running", "frozen")
               and not in_fault(r["t"]) and r["t"] > 15]
    out_fps = [r["out_fps"] for r in running]
    client = [r["client_fps"] for r in running if r.get("client_fps") is not None]
    by_action = {}
    for e in faults:
        a = by_action.setdefault(e["action"], {"n": 0, "recovered": 0, "max_recovery_s": 0.0})
        a["n"] += 1
        a["recovered"] += bool(e["recovered"])
        a["max_recovery_s"] = max(a["max_recovery_s"], e["recovery_s"])
    last = rows[-1] if rows else {}
    s = {
        "duration_min": round(rows[-1]["t"] / 60, 1) if rows else 0,
        "samples": len(rows),
        "faults_injected": len(faults),
        "faults_recovered": sum(bool(e["recovered"]) for e in faults),
        "fault_errors": [e for e in events if e["event"] == "fault_error"],
        "by_fault": by_action,
        "output_fps_median": round(statistics.median(out_fps), 1) if out_fps else None,
        "output_fps_p05": round(sorted(out_fps)[int(0.05 * (len(out_fps) - 1))], 1) if out_fps else None,
        "client_fps_median": round(statistics.median(client), 1) if client else None,
        "client_fps_p05": round(sorted(client)[int(0.05 * (len(client) - 1))], 1) if client else None,
        "latency_ms_median": round(statistics.median(r["latency_ms"] for r in running), 2) if running else None,
        "latency_p95_ms_max": max((r["latency_p95_ms"] for r in running), default=None),
        "footprint_mb_start": warm[0]["footprint_mb"] if warm else None,
        "footprint_mb_end": last.get("footprint_mb"),
        "footprint_mb_max": max(r["footprint_mb"] for r in rows) if rows else None,
        "footprint_slope_mb_per_h": round(slope_per_hour([r["t"] for r in warm], [r["footprint_mb"] for r in warm]), 1),
        "footprint_q3_median_mb": _quarter_median(rows, 2),
        "footprint_q4_median_mb": _quarter_median(rows, 3),
        "threads_min_max": [min(r["threads"] for r in warm), max(r["threads"] for r in warm)] if warm else None,
        "threads_slope_per_h": round(slope_per_hour([r["t"] for r in warm], [r["threads"] for r in warm]), 2),
        "fds_slope_per_h": round(slope_per_hour([r["t"] for r in warm], [r["fds"] for r in warm]), 2),
        "samples_outside_faults": len(running),
        "fds_min_max": [min(r["fds"] for r in warm), max(r["fds"] for r in warm)] if warm else None,
        "render_errors": last.get("render_errors"), "ai_errors": last.get("ai_errors"),
        "pipeline_restarts": last.get("pipeline_restarts"), "ai_restarts": last.get("ai_restarts"),
        "capture_reconnects": last.get("reconnects"),
        "camera_sent": last.get("camera_sent"), "camera_dropped": last.get("camera_dropped"),
        "track_ids_created": last.get("track_ids"), "ai_model": last.get("ai_model"),
    }
    s["verdict"] = {
        "all_faults_recovered": s["faults_injected"] > 0 and s["faults_recovered"] == s["faults_injected"],
        # a leak keeps rising between the last two quarters; a pool/allocator high-water plateau does not
        "no_memory_growth": (s["footprint_q4_median_mb"] or 0) <= 1.05 * (s["footprint_q3_median_mb"] or 1)
                            or abs(s["footprint_slope_mb_per_h"]) < 50,
        "sustained_60fps": (s["output_fps_p05"] or 0) >= 55,
        "no_watchdog_restarts": (s["pipeline_restarts"] or 0) == 0 and (s["ai_restarts"] or 0) == 0,
        "threads_stable": abs(s["threads_slope_per_h"]) < 5,      # a leak shows as a trend, not a range
        "fds_stable": abs(s["fds_slope_per_h"]) < 5,
    }
    (d / "summary.json").write_text(json.dumps(s, indent=2))
    return s


if __name__ == "__main__":
    print(json.dumps(analyse(Path(sys.argv[1])), indent=2))
