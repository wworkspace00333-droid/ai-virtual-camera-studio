"""“Check the whole chain” from inside the app (no Terminal, no file paths).

Runs the existing end-to-end self-test (`--pipeline-e2e-test`) as a child process of the app itself, so
macOS asks camera permission for the app (not for Terminal) and the report goes to the app's own
folder (no Desktop/Documents permission). Two runs: a synthetic 1080p60 scene (exact frame rate,
drops, processing) and, if OBS is sharing frames, the real OBS video. The app's own capture is stopped
for the check (both would feed the same camera) and restarted afterwards.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from vcstudio.i18n import tr

SECONDS = 12
TIMEOUT_S = 150


def self_command() -> list[str]:
    """How to start this app's own entry point (packaged payload or `python -m vcstudio`)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "vcstudio"]


def summarize(r: dict) -> str:
    if not r:
        return tr("no report")
    if r.get("error") and not r.get("client_frames"):
        return tr("FAIL: {e}", e=r["error"])
    sent, dropped = r.get("camera_sent") or 0, r.get("camera_dropped") or 0
    return tr("{verdict}: {fps} fps at the client, {res}, processing visible in {rep}% of frames, input→camera "
              "{lat} ms (p95 {p95}), dropped by the camera queue {d} of {s}, render errors {err}, AI {model} "
              "{aifps}×/s",
              verdict=tr("PASS") if r.get("ok") else tr("FAIL"), fps=r.get("client_distinct_fps"),
              res=r.get("client_resolution"), rep=round(100 * (r.get("frames_with_replacement") or 0)),
              lat=r.get("pipeline_latency_ms"), p95=r.get("pipeline_latency_p95_ms"), d=dropped, s=sent,
              err=r.get("render_errors"), model=r.get("ai_model"), aifps=r.get("ai_fps")) + (
        ". " + tr("Error: {e}", e=r["error"]) if r.get("error") else "")


class ChainCheck(QObject):
    progress = Signal(str)
    finished = Signal(list)          # [(title, report dict, path)]

    def __init__(self, ctx, device: str, sink: str, parent=None):
        super().__init__(parent)
        self.ctx, self.device, self.sink = ctx, device, sink
        self.reports_dir = ctx.paths.root / "reports"
        self.results: list = []
        self._queue: list = []
        self._proc: QProcess | None = None
        self._was_running = False
        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.timeout.connect(self._timeout)

    def start(self) -> None:
        from vcstudio.core.obs_shm_source import obs_sharing_status
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._queue = [(tr("Test scene 1080p60"), [], self.reports_dir / f"e2e-synthetic-{stamp}.json")]
        if obs_sharing_status().get("available"):
            self._queue.append((tr("Your OBS video"), ["--input", "obs"], self.reports_dir / f"e2e-obs-{stamp}.json"))
        self._was_running = self.ctx.capture.stats().state not in ("stopped", "error")
        if self._was_running:
            self.ctx.stop_capture()
        self.results = []
        self._next()

    def _next(self) -> None:
        if not self._queue:
            self._done()
            return
        title, extra, path = self._queue.pop(0)
        self._current = (title, path)
        self.progress.emit(tr("Checking: {what} ({s} s)…", what=title, s=SECONDS))
        args = self_command()[1:] + ["--pipeline-e2e-test", str(path), "--seconds", str(SECONDS),
                                     "--device", self.device, "--sink", self.sink] + extra
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.finished.connect(self._proc_finished)
        self._proc = p
        self._timer.start(TIMEOUT_S * 1000)
        p.start(self_command()[0], args)

    def _proc_finished(self, *_):
        self._timer.stop()
        title, path = self._current
        try:
            r = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            out = bytes(self._proc.readAll()).decode(errors="replace")[-400:] if self._proc else ""
            r = {"ok": False, "error": tr("the check did not write a report") + (f": {out}" if out.strip() else "")}
        self.results.append((title, r, str(path)))
        self._next()

    def _timeout(self):
        if self._proc is not None:
            self._proc.kill()

    def _done(self) -> None:
        if self._was_running:
            try:
                self.ctx.start_capture()
            except Exception:  # noqa: BLE001 - reported on the pages as usual
                pass
        self.finished.emit(self.results)
