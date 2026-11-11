"""Logging: rotating file + in-memory ring buffer that the Logs page subscribes to."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import threading
from pathlib import Path
from typing import Callable

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted records and notifies subscribers (from any thread)."""

    def __init__(self, capacity: int = 5000):
        super().__init__()
        self.records: collections.deque[tuple[int, str]] = collections.deque(maxlen=capacity)
        self._subs: list[Callable[[int, str], None]] = []
        self._sub_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.records.append((record.levelno, line))
        with self._sub_lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(record.levelno, line)
            except Exception:
                pass

    def subscribe(self, cb: Callable[[int, str], None]) -> None:
        with self._sub_lock:
            self._subs.append(cb)

    def unsubscribe(self, cb: Callable[[int, str], None]) -> None:
        with self._sub_lock:
            if cb in self._subs:
                self._subs.remove(cb)


_ring: RingBufferHandler | None = None


def setup_logging(log_dir: Path, level: str = "INFO", max_bytes: int = 5_000_000,
                  backup_count: int = 5) -> RingBufferHandler:
    global _ring
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_vcstudio", False):
            root.removeHandler(h)
            h.close()
    fmt = logging.Formatter(FORMAT, DATEFMT)
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "studio.log", maxBytes=max_bytes,
                                              backupCount=backup_count, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    ring = RingBufferHandler()
    ring.setFormatter(fmt)
    for h in (fh, sh, ring):
        h._vcstudio = True  # type: ignore[attr-defined]
        root.addHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    _ring = ring
    return ring


def ring_handler() -> RingBufferHandler | None:
    return _ring
