"""System resource monitoring: CPU, memory, process RSS, Apple GPU utilisation (IOAccelerator)."""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field

import psutil

_GPU_RE = re.compile(r'"Device Utilization %"=(\d+)')
_GPU_MEM_RE = re.compile(r'"In use system memory"=(\d+)')


class _RusageInfoV2(__import__("ctypes").Structure):
    import ctypes as _c
    _fields_ = [("ri_uuid", _c.c_uint8 * 16), ("ri_user_time", _c.c_uint64), ("ri_system_time", _c.c_uint64),
                ("ri_pkg_idle_wkups", _c.c_uint64), ("ri_interrupt_wkups", _c.c_uint64), ("ri_pageins", _c.c_uint64),
                ("ri_wired_size", _c.c_uint64), ("ri_resident_size", _c.c_uint64), ("ri_phys_footprint", _c.c_uint64),
                ("ri_proc_start_abstime", _c.c_uint64), ("ri_proc_exit_abstime", _c.c_uint64),
                ("ri_child_user_time", _c.c_uint64), ("ri_child_system_time", _c.c_uint64),
                ("ri_child_pkg_idle_wkups", _c.c_uint64), ("ri_child_interrupt_wkups", _c.c_uint64),
                ("ri_child_pageins", _c.c_uint64), ("ri_child_elapsed_abstime", _c.c_uint64),
                ("ri_diskio_bytesread", _c.c_uint64), ("ri_diskio_byteswritten", _c.c_uint64)]


_libproc = None


def phys_footprint(pid: int | None = None) -> int | None:
    """Physical footprint in bytes (what Activity Monitor's "Memory" column shows), or None.

    RSS over-counts on macOS (shared framework pages, mapped IOSurfaces); the footprint is the
    process's own dirty + compressed memory, which is what actually pressures a 16 GB machine.
    """
    global _libproc
    import ctypes
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = _RusageInfoV2()
        rc = _libproc.proc_pid_rusage(pid or os.getpid(), 2, ctypes.byref(info))
        return int(info.ri_phys_footprint) if rc == 0 else None
    except OSError:
        return None


def read_gpu_stats(timeout: float = 1.0) -> tuple[float | None, int | None]:
    """Return (device utilisation %, GPU in-use memory bytes) from ioreg, or (None, None)."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    u = _GPU_RE.search(out)
    m = _GPU_MEM_RE.search(out)
    return (float(u.group(1)) if u else None, int(m.group(1)) if m else None)


@dataclass
class ResourceSample:
    ts: float
    cpu_percent: float            # system-wide
    process_cpu_percent: float    # this process (can exceed 100 on multicore)
    mem_total: int
    mem_used: int
    mem_percent: float
    process_rss: int
    gpu_percent: float | None
    gpu_mem: int | None
    threads: int
    pressure: str = field(default="normal")  # normal | warning | critical
    footprint: int | None = None


class SystemMonitor:
    """Call sample() periodically (e.g. from a QTimer). Cheap: ~1 ms + ioreg every gpu_every calls."""

    def __init__(self, memory_warning_mb: int = 3072, gpu_every: int = 2):
        self._proc = psutil.Process(os.getpid())
        self._proc.cpu_percent(None)
        psutil.cpu_percent(None)
        self.memory_warning_bytes = memory_warning_mb * 1024 * 1024
        self._gpu_every = max(1, gpu_every)
        self._n = 0
        self._gpu: tuple[float | None, int | None] = (None, None)
        self.last: ResourceSample | None = None

    def sample(self) -> ResourceSample:
        if self._n % self._gpu_every == 0:
            self._gpu = read_gpu_stats()
        self._n += 1
        vm = psutil.virtual_memory()
        with self._proc.oneshot():
            rss = self._proc.memory_info().rss
            pcpu = self._proc.cpu_percent(None)
            threads = self._proc.num_threads()
        pressure = "normal"
        fp = phys_footprint()
        mem = fp if fp is not None else rss
        if mem > self.memory_warning_bytes or vm.percent > 85:
            pressure = "warning"
        if mem > 2 * self.memory_warning_bytes or vm.percent > 95:
            pressure = "critical"
        self.last = ResourceSample(time.time(), psutil.cpu_percent(None), pcpu, vm.total,
                                   vm.total - vm.available, vm.percent, rss, self._gpu[0],
                                   self._gpu[1], threads, pressure, fp)
        return self.last


class FpsCounter:
    """Sliding-window rate counter (used by capture/pipeline stages)."""

    def __init__(self, window_s: float = 1.0):
        self.window = window_s
        self._t: list[float] = []
        self._first: float | None = None

    def tick(self, now: float | None = None) -> None:
        now = time.perf_counter() if now is None else now
        if self._first is None:
            self._first = now
        self._t.append(now)
        cut = now - self.window
        while self._t and self._t[0] < cut:
            self._t.pop(0)

    def rate(self, now: float | None = None) -> float:
        now = time.perf_counter() if now is None else now
        recent = [t for t in self._t if t >= now - self.window]
        if self._first is None or len(recent) < 2:
            return 0.0
        # While the window is still filling, rate = intervals / elapsed (avoids startup under-reporting)
        span = min(self.window, now - self._first)
        if span < self.window:
            return (len(recent) - 1) / max(recent[-1] - recent[0], 1e-6)
        return len(recent) / self.window
