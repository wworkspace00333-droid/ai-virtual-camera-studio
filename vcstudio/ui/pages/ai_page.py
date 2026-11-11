from __future__ import annotations

import logging
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem)

from vcstudio.ai.model_manager import COMPUTE_UNITS, ModelManager
from vcstudio.core.frame import Frame
from vcstudio.core.system_info import coreml_info, metal_info
from vcstudio.ui.widgets import Card, StatCard, page_scaffold
from vcstudio.i18n import tr

log = logging.getLogger(__name__)
BENCH_ITERS = 40


class _BenchSignals(QObject):
    row = Signal(list)
    done = Signal(str)


def run_benchmark(models_dir, frame: Frame, emit_row, iters: int = BENCH_ITERS) -> list[dict]:
    """Median/p95 latency for every model x compute unit on `frame`. Loads one model at a time."""
    from vcstudio.ai.coreml_engine import CoreMLDetector
    mm = ModelManager(models_dir)
    out = []
    for info in mm.available().values():
        if info.task != "detect":
            continue
        for cu in COMPUTE_UNITS:
            lm = mm.load(info, cu)
            det = CoreMLDetector(lm)
            for _ in range(5):
                det.detect(frame, 0.35, 0.5)
            ts = []
            for _ in range(iters):
                t = time.perf_counter()
                dets, _ = det.detect(frame, 0.35, 0.5)
                ts.append((time.perf_counter() - t) * 1000)
            ts.sort()
            row = {"model": info.name, "compute_units": cu, "median_ms": round(ts[len(ts) // 2], 2),
                   "p95_ms": round(ts[int(0.95 * (len(ts) - 1))], 2), "max_fps": round(1000 / ts[len(ts) // 2], 1),
                   "load_s": round(lm.load_seconds, 2), "detections": len(dets),
                   "frame": f"{frame.width}x{frame.height}"}
            out.append(row)
            emit_row(row)
            mm.unload()
    return out


class AIEnginePage:
    key, title = "ai", "AI Engine"

    def __init__(self, ctx):
        self.ctx = ctx
        self.widget, lay = page_scaffold("AI Engine", "On-device object detection with Core ML through Apple Vision "
                                         "(CPU / GPU / Neural Engine). Frames never leave this Mac.")
        stats = Card("Engine status")
        g = QGridLayout(); g.setSpacing(10)
        names = ["State", "Model", "Inference rate", "Inference time", "Capture → result", "Detections",
                 "Tracked objects", "Face detection", "Errors"]
        self.cards = {n: StatCard(n) for n in names}
        for i, n in enumerate(names):
            g.addWidget(self.cards[n], i // 4, i % 4)
        stats.layout_.addLayout(g)
        self.err = QLabel(); self.err.setWordWrap(True); self.err.setStyleSheet("color: #f85149;")
        stats.layout_.addWidget(self.err)
        lay.addWidget(stats)

        cfg = Card("Configuration (active profile)")
        f = QFormLayout()
        self.enabled = QCheckBox(tr("Enable AI detection"))
        self.model = QComboBox()
        self.cu = QComboBox()
        for k, v in COMPUTE_UNITS.items():
            self.cu.addItem(tr(v), k)
        self.face = QCheckBox(tr("Apple Vision face detection (face rectangles only; no identity recognition)"))
        f.addRow(tr(""), self.enabled)
        f.addRow(tr("Detector model"), self.model)
        f.addRow(tr("Compute units"), self.cu)
        f.addRow(tr(""), self.face)
        cfg.layout_.addLayout(f)
        lay.addWidget(cfg)

        models = Card("Installed models")
        self.models = QTableWidget(0, 5)
        self.models.setHorizontalHeaderLabels([tr("Model"), tr("Size"), tr("Classes"), tr("Source"), tr("License")])
        self.models.horizontalHeader().setStretchLastSection(True)
        self.models.setMinimumHeight(110)
        models.layout_.addWidget(self.models)
        hint = QLabel(tr("Add models with: .venv-convert/bin/python scripts/convert_models.py <name>"))
        hint.setObjectName("muted")
        models.layout_.addWidget(hint)
        lay.addWidget(models)

        bench = Card("Benchmark (this Mac)")
        row = QHBoxLayout()
        self.bench_btn = QPushButton(tr("Run benchmark"))
        self.bench_info = QLabel(tr("Uses the current input frame, or a built-in sample image when not capturing."))
        self.bench_info.setObjectName("muted")
        row.addWidget(self.bench_btn); row.addWidget(self.bench_info); row.addStretch()
        bench.layout_.addLayout(row)
        self.bench = QTableWidget(0, 7)
        self.bench.setHorizontalHeaderLabels([tr("Model"), tr("Compute units"), tr("Median ms"), tr("p95 ms"), tr("Max FPS"), tr("Load s"), tr("Frame")])
        self.bench.horizontalHeader().setStretchLastSection(True)
        self.bench.setMinimumHeight(220)
        bench.layout_.addWidget(self.bench)
        lay.addWidget(bench)
        env = QLabel(self._env_text()); env.setObjectName("muted")
        lay.addWidget(env)
        lay.addStretch()

        self._sig = _BenchSignals()
        self._sig.row.connect(self._bench_row)
        self._sig.done.connect(self._bench_done)
        self.bench_btn.clicked.connect(self.start_benchmark)
        self._load_from_profile()
        for w in (self.enabled, self.face):
            w.toggled.connect(self._save)
        for w in (self.model, self.cu):
            w.currentIndexChanged.connect(self._save)
        ctx.profiles.add_listener(lambda e, n: self._load_from_profile() if e == "switched" else None)
        self.timer = QTimer(self.widget)
        self.timer.timeout.connect(self.update_stats)
        self.timer.start(500)

    def _env_text(self) -> str:
        m, c = metal_info(), coreml_info()
        return tr("Metal: {name} · GPU working set {gb} GB · Core ML: {coreml}", name=m.get('name', tr('unavailable')),
                  gb=m.get('recommended_working_set_gb', '?'),
                  coreml=tr('available') if c.get('available') else tr('unavailable'))

    def _load_from_profile(self):
        body = self.ctx.profiles.active()["body"]
        ai = body["ai"]
        for w in (self.enabled, self.face, self.model, self.cu):
            w.blockSignals(True)
        self.enabled.setChecked(ai.get("enabled", True))
        self.face.setChecked(ai.get("face_detection") == "vision")
        self.model.clear()
        self.model.addItem(tr("Auto (smallest; largest in Maximum Quality mode)"), "auto")
        avail = self.ctx.models.available()
        for name, info in avail.items():
            if info.task == "detect":
                self.model.addItem(f"{name} ({info.size_mb} MB)", name)
        idx = self.model.findData(ai.get("detector_model", "auto"))
        self.model.setCurrentIndex(max(0, idx))
        self.cu.setCurrentIndex(max(0, self.cu.findData(ai.get("compute_units", "all"))))
        for w in (self.enabled, self.face, self.model, self.cu):
            w.blockSignals(False)
        self.models.setRowCount(len(avail))
        for i, (name, info) in enumerate(avail.items()):
            for j, v in enumerate((name, f"{info.size_mb} " + tr("MB"), str(len(info.classes)), tr(info.source), info.license)):
                self.models.setItem(i, j, QTableWidgetItem(v))
        self.models.resizeColumnsToContents()

    def _save(self, *_):
        def mutate(b):
            b["ai"]["enabled"] = self.enabled.isChecked()
            b["ai"]["face_detection"] = "vision" if self.face.isChecked() else None
            b["ai"]["detector_model"] = self.model.currentData() or "auto"
            b["ai"]["compute_units"] = self.cu.currentData() or "all"
        self.ctx.update_active_profile(mutate)

    def update_stats(self):
        s = self.ctx.ai.stats()
        level = {"running": "ok", "degraded": "warn", "error": "error", "loading": "warn"}.get(s.state, "idle")
        self.cards["State"].set(s.state.title(), "starts with capture" if s.state == "stopped" else "", level)
        self.cards["Model"].set(s.model or "—", tr(s.quality_note or COMPUTE_UNITS.get(s.compute_units, "")) +
                                (" · " + tr("loaded in {s} s", s=f"{s.model_load_s:.2f}") if s.model_load_s else ""))
        tgt = tr("max sustainable") if s.target_fps is None else tr("target {fps} fps", fps=f"{s.target_fps:g}")
        live = s.state == "running"
        self.cards["Inference rate"].set(f"{s.achieved_fps:.1f} fps" if live else "—", tgt,
                                         "ok" if live and (s.target_fps is None or s.achieved_fps >= 0.85 * s.target_fps) else None)
        self.cards["Inference time"].set(f"{s.inference_ms_avg:.1f} " + tr("ms") if s.inferences else "—",
                                         f"p95 {s.inference_ms_p95:.1f} " + tr("ms") if s.inferences else "")
        self.cards["Capture → result"].set(f"{s.frame_age_ms:.1f} " + tr("ms") if s.inferences else "—", "frame arrival to detections")
        self.cards["Detections"].set(str(s.detections) if live else "—", tr("{n} inferences", n=f"{s.inferences:,}"))
        self.cards["Face detection"].set(f"{s.face_ms_avg:.1f} " + tr("ms") if s.face_ms_avg else ("on" if self.face.isChecked() else "off"),
                                         "Apple Vision")
        self.cards["Tracked objects"].set(str(s.tracks_active) if live and s.tracking else ("off" if not s.tracking else "—"),
                                          tr("{ids} IDs · {rec} recovered · {ms} ms", ids=s.track_ids_created,
                                             rec=s.tracks_recovered, ms=f"{s.tracker_ms:.2f}"))
        self.cards["Errors"].set(str(s.errors), tr("{n} consecutive", n=s.consecutive_errors), "error" if s.consecutive_errors else None)
        self.err.setText(s.last_error if s.state in ("error", "degraded") else "")

    # ---------------- benchmark
    def _bench_frame(self) -> Frame:
        f, _ = self.ctx.capture.latest()
        if f is not None:
            with f.bgra() as a:
                arr = np.array(a)  # private copy so capture buffers are not held
            return Frame(0, 0, 0, f.width, f.height, array=arr, source=f.source)
        from vcstudio.ai.testimages import load_bgra
        from vcstudio.core.resources import sample
        arr = load_bgra(sample("bus.jpg"))
        return Frame(0, 0, 0, arr.shape[1], arr.shape[0], array=arr, source="bus.jpg")

    def start_benchmark(self):
        self.bench_btn.setEnabled(False)
        self.bench.setRowCount(0)
        frame = self._bench_frame()
        self.bench_info.setText(tr("Running on {src} ({w}×{h}), {n} iterations each…", src=frame.source, w=frame.width,
                                   h=frame.height, n=BENCH_ITERS))

        def work():
            try:
                rows = run_benchmark(self.ctx.paths.models_dir, frame, self._sig.row.emit)
                from vcstudio.core.system_info import inspect_environment
                env = inspect_environment()
                hw = {k: env[k] for k in ("cpu", "memory_gb", "macos")}
                self.ctx.db.add_benchmark("detector_latency", hw, {"rows": rows, "ai_running": self.ctx.ai.running})
                self._sig.done.emit(tr("Done: {n} configurations, saved to the benchmark history.", n=len(rows)))
            except Exception as e:
                log.exception("benchmark failed")
                self._sig.done.emit(tr("Benchmark failed: {e}", e=e))
        threading.Thread(target=work, name="ai-benchmark", daemon=True).start()

    def _bench_row(self, row):
        r = self.bench.rowCount()
        self.bench.insertRow(r)
        vals = [row["model"], COMPUTE_UNITS[row["compute_units"]], row["median_ms"], row["p95_ms"], row["max_fps"],
                row["load_s"], row["frame"]]
        for j, v in enumerate(vals):
            self.bench.setItem(r, j, QTableWidgetItem(str(v)))
        self.bench.resizeColumnsToContents()

    def _bench_done(self, msg):
        self.bench_info.setText(msg)
        self.bench_btn.setEnabled(True)
