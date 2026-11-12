"""One app window per data directory.

Two running copies would both send frames to the same output camera and fight over it (found live:
the Card for a game calibration saw the other copy's video instead of its markers). An exclusive
flock on <data dir>/app.lock is held for the life of the process; the OS releases it if the app
crashes, so a stale lock never blocks the next start. Command-line tools (self-check, soak, scripts)
do not take it.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

_held: dict[str, int] = {}


def acquire(root: Path) -> int | None:
    """Take the lock for `root`. Returns None if this process holds it now, else the other app's pid
    (0 if unknown)."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "app.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            other = int(os.pread(fd, 32, 0).decode().strip() or 0)
        except (ValueError, OSError):
            other = 0
        os.close(fd)
        return other
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    _held[str(path)] = fd
    return None


def release(root: Path) -> None:
    fd = _held.pop(str(root / "app.lock"), None)
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
