from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QGridLayout, QLabel, QRadioButton

from vcstudio.core.config import PERFORMANCE_MODES
from vcstudio.core.performance import ResourceSample
from vcstudio.ui import theme
from vcstudio.ui.widgets import Card, Sparkline, fmt_bytes, page_scaffold
from vcstudio.i18n import tr


class PerformancePage:
    key, title = "performance", "Performance"

    def __init__(self, ctx):
        self.ctx = ctx
        self.widget, lay = page_scaffold("Performance", "Operating mode and live resource usage. Mode FPS values are targets; "
                                         "measured results on this Mac are in docs/BENCHMARK.md (scripts/benchmark.py).")
        modes = Card("Operating mode")
        self.group = QButtonGroup(self.widget)
        cur = ctx.settings.get("performance.mode")
        for m in PERFORMANCE_MODES.values():
            rb = QRadioButton(f"{tr(m.label)} — {tr(m.description)}")
            rb.setProperty("mode", m.key)
            rb.setChecked(m.key == cur)
            self.group.addButton(rb)
            modes.layout_.addWidget(rb)
        self.group.buttonToggled.connect(self._mode_changed)
        lay.addWidget(modes)
        charts = Card("Live resources")
        g = QGridLayout()
        self.cpu = Sparkline("System CPU")
        self.pcpu = Sparkline("App CPU (per core sum)", max_value=400, color=theme.OK)
        self.mem = Sparkline("System memory", color=theme.WARN)
        self.gpu = Sparkline("GPU utilisation (system)", color="#b07cff")
        for i, w in enumerate((self.cpu, self.pcpu, self.mem, self.gpu)):
            g.addWidget(w, i // 2, i % 2)
        charts.layout_.addLayout(g)
        self.details = QLabel(tr("—")); self.details.setObjectName("muted")
        charts.layout_.addWidget(self.details)
        lay.addWidget(charts)
        pipe = Card("Pipeline timings (measured live)")
        g2 = QGridLayout()
        self.lat = Sparkline("Capture → output latency", max_value=50, unit=" ms", color=theme.ACCENT)
        self.proc = Sparkline("Per-frame processing", max_value=16.7, unit=" ms", color=theme.OK)
        self.outfps = Sparkline("Output FPS", max_value=60, unit="", color=theme.WARN)
        self.aims = Sparkline("AI inference", max_value=40, unit=" ms", color="#b07cff")
        for i, w in enumerate((self.lat, self.proc, self.outfps, self.aims)):
            g2.addWidget(w, i // 2, i % 2)
        pipe.layout_.addLayout(g2)
        self.pipe_info = QLabel(tr("Pipeline not running.")); self.pipe_info.setObjectName("muted"); self.pipe_info.setWordWrap(True)
        pipe.layout_.addWidget(self.pipe_info)
        lay.addWidget(pipe)
        lay.addStretch()

    def _mode_changed(self, btn, checked):
        if checked:
            mode = btn.property("mode")
            self.ctx.settings.set("performance.mode", mode)
            self.ctx.update_active_profile(lambda b: b.__setitem__("performance_mode", mode))

    def sync_mode(self):
        cur = self.ctx.settings.get("performance.mode")
        for b in self.group.buttons():
            if b.property("mode") == cur and not b.isChecked():
                b.blockSignals(True); b.setChecked(True); b.blockSignals(False)

    def on_pipeline(self, p, a, w):
        if p.state == "stopped":
            self.pipe_info.setText(tr("Pipeline not running. Watchdog: {p} pipeline / {a} AI restarts, memory {m}.",
                                      p=w.pipeline_restarts, a=w.ai_restarts, m=tr(w.memory_state)))
            return
        self.lat.push(p.latency_ms); self.proc.push(p.process_ms); self.outfps.push(p.output_fps)
        self.aims.push(a.inference_ms_avg if a.inferences else None)
        self.pipe_info.setText(tr("{state} · {backend} render {render} ms (GPU {gpu} ms) · latency p95 {p95} ms · skipped "
                                  "inputs {skipped} · render errors {errors} · AI {aifps} fps ({model}) · watchdog: {pr} "
                                  "pipeline / {ar} AI restarts, memory {mem} ({mb} MB)", state=tr(p.state),
                                  backend=p.backend or '—', render=f"{p.render_ms:.2f}", gpu=f"{p.gpu_ms:.2f}",
                                  p95=f"{p.latency_ms_p95:.1f}", skipped=p.inputs_skipped, errors=p.render_errors,
                                  aifps=f"{a.achieved_fps:.1f}", model=a.model, pr=w.pipeline_restarts, ar=w.ai_restarts,
                                  mem=tr(w.memory_state), mb=f"{w.rss_mb:.0f}")
                               + (" · " + tr("LOAD SHED") if w.load_shed else ""))

    def on_sample(self, s: ResourceSample):
        self.cpu.push(s.cpu_percent); self.pcpu.push(s.process_cpu_percent)
        self.mem.push(s.mem_percent); self.gpu.push(s.gpu_percent)
        self.details.setText(tr("App footprint {fp} (RSS {rss}) · threads {n} · memory pressure {pr} · GPU memory in use {gm}",
                                fp=fmt_bytes(s.footprint), rss=fmt_bytes(s.process_rss), n=s.threads, pr=tr(s.pressure),
                                gm=fmt_bytes(s.gpu_mem)))
