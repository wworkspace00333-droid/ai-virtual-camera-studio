from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QLabel

from vcstudio.core.config import PERFORMANCE_MODES
from vcstudio.core.performance import ResourceSample
from vcstudio.core.system_info import camera_extensions
from vcstudio.i18n import tr
from vcstudio.ui.widgets import StatCard, fmt_bytes, page_scaffold

OUR_BUNDLE_HINT = "aivcstudio"


class DashboardPage:
    key, title = "dashboard", "Dashboard"

    def __init__(self, ctx):
        self.ctx = ctx
        self.widget, lay = page_scaffold("Dashboard", "Live status of input, AI, output and system resources.")
        grid = QGridLayout()
        grid.setSpacing(12)
        names = ["Input", "Output", "Output FPS", "AI inference", "CPU", "Memory", "GPU",
                 "Detections", "Active ROIs", "Camera extension", "Profile", "Mode"]
        self.cards = {n: StatCard(n) for n in names}
        for i, n in enumerate(names):
            grid.addWidget(self.cards[n], i // 4, i % 4)
        lay.addLayout(grid)
        self.note = QLabel(tr(""))
        self.note.setObjectName("muted")
        lay.addWidget(self.note)
        lay.addStretch()
        self.refresh_profile()
        self.refresh_extension()

    def refresh_profile(self) -> None:
        name = self.ctx.profiles.active_name or "—"
        body = self.ctx.profiles.active()["body"]
        rois = body.get("rois", [])
        enabled = sum(1 for r in rois if r.get("enabled", True))
        self.cards["Profile"].set(name, tr("{n} profile(s)", n=len(self.ctx.profiles.names())), "ok")
        self.cards["Active ROIs"].set(str(enabled), tr("{n} defined in profile", n=len(rois)))
        mode = PERFORMANCE_MODES[self.ctx.settings.get("performance.mode")]
        self.cards["Mode"].set(mode.label, mode.description)

    def refresh_extension(self) -> None:
        exts = camera_extensions()
        ours = [e for e in exts if OUR_BUNDLE_HINT in e["bundle_id"]]
        if ours:
            e = ours[0]
            st = "ok" if e["enabled"] and e["active"] else "warn"
            self.cards["Camera extension"].set("Installed", e["state"], st)
        else:
            others = ", ".join(e["name"] for e in exts) or tr("none")
            self.cards["Camera extension"].set("Not installed", tr("See Virtual Camera page. Other camera extensions: {others}", others=others), "idle")

    def on_capture(self, c) -> None:
        level = {"running": "ok", "frozen": "warn", "reconnecting": "warn", "no_signal": "error",
                 "error": "error"}.get(c.state, "idle")
        label = c.state.replace("_", " ").title()
        if c.state in ("running", "frozen") and c.width:
            self.cards["Input"].set(label, f"{c.source_name} · {c.width}×{c.height} @ {c.measured_fps:.1f} fps", level)
        else:
            self.cards["Input"].set(label, tr(c.last_error) if c.last_error else (c.source_name or tr("Not capturing")), level)

    def on_pipeline(self, p) -> None:
        if p.state == "stopped":
            self.cards["Output"].set("Stopped", "Start processing on the Preview page")
            self.cards["Output FPS"].set("—", "No pipeline running")
            return
        cam = {"connected": ("Camera connected", "ok"), "not_installed": ("Camera not installed", "warn"),
               "disabled": ("Camera output off", "idle"), "error": ("Camera error", "error")}[p.camera_state]
        level = {"running": cam[1], "paused": "warn", "passthrough": "error"}.get(p.state, "idle")
        self.cards["Output"].set(p.state.title(), tr(cam[0]) + " · " + tr("sent {n}", n=p.camera_sent) +
                                 (f" · {p.passthrough_reason}" if p.passthrough_reason else ""), level)
        self.cards["Output FPS"].set(f"{p.output_fps:.1f}", tr("latency {ms} ms", ms=f"{p.latency_ms:.1f}") +
                                     f" · {p.backend or '—'} {p.render_ms:.1f} " + tr("ms"), "ok" if p.output_fps > 0 else "warn")

    def on_ai(self, a) -> None:
        live = a.state == "running"
        level = {"running": "ok", "degraded": "warn", "error": "error", "loading": "warn"}.get(a.state, "idle")
        if a.inferences and live:
            self.cards["AI inference"].set(f"{a.inference_ms_avg:.1f} " + tr("ms"), f"{a.model} · {a.achieved_fps:.1f} fps", level)
        else:
            self.cards["AI inference"].set(a.state.title(), tr(a.last_error) if a.last_error else tr("starts with capture"), level)
        if a.tracking:
            self.cards["Detections"].set(str(a.tracks_active) if live else "—",
                                         tr("tracked · {d} detections · {i} IDs", d=a.detections, i=a.track_ids_created))
        else:
            self.cards["Detections"].set(str(a.detections) if live else "—", "after class filters")

    def on_sample(self, s: ResourceSample) -> None:
        cpu_state = "warn" if s.cpu_percent > 85 else "ok"
        self.cards["CPU"].set(f"{s.cpu_percent:.0f}%", tr("This app: {cpu}% · {n} threads", cpu=f"{s.process_cpu_percent:.0f}", n=s.threads), cpu_state)
        mem_state = {"normal": "ok", "warning": "warn", "critical": "error"}[s.pressure]
        self.cards["Memory"].set(fmt_bytes(s.footprint or s.process_rss),
                                 tr("footprint · system {p}% of {total}", p=f"{s.mem_percent:.0f}", total=fmt_bytes(s.mem_total)), mem_state)
        if s.gpu_percent is None:
            self.cards["GPU"].set("n/a", "IOAccelerator statistics unavailable")
        else:
            self.cards["GPU"].set(f"{s.gpu_percent:.0f}%", tr("System-wide GPU · in use {mem}", mem=fmt_bytes(s.gpu_mem)), "ok")
