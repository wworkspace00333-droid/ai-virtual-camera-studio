"""Development/runtime environment inspection (macOS, Apple Silicon, Metal, Core ML, CMIO)."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any


def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def metal_info() -> dict[str, Any]:
    try:
        import Metal  # type: ignore
        d = Metal.MTLCreateSystemDefaultDevice()
        if d is None:
            return {"available": False}
        return {"available": True, "name": str(d.name()),
                "recommended_working_set_gb": round(d.recommendedMaxWorkingSetSize() / 2**30, 2),
                "unified_memory": bool(d.hasUnifiedMemory())}
    except Exception as e:  # pragma: no cover - depends on host
        return {"available": False, "error": str(e)}


def coreml_info() -> dict[str, Any]:
    try:
        import CoreML  # type: ignore  # noqa: F401
        return {"available": True}
    except Exception as e:
        return {"available": False, "error": str(e)}


def camera_extensions() -> list[dict[str, str]]:
    """Parse `systemextensionsctl list` for CMIO camera extensions."""
    out = _run(["/usr/bin/systemextensionsctl", "list"])
    exts, in_cmio = [], False
    for line in out.splitlines():
        if line.startswith("---"):
            in_cmio = "cmio" in line
            continue
        if in_cmio and "\t" in line and not line.startswith("enabled"):
            parts = line.split("\t")
            if len(parts) >= 6:
                exts.append({"enabled": parts[0].strip() == "*", "active": parts[1].strip() == "*",
                             "team_id": parts[2], "bundle_id": parts[3].split(" ")[0],
                             "name": parts[4], "state": parts[5].strip("[] ")})
    return exts


def signing_identities() -> int:
    out = _run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"])
    for line in out.splitlines():
        if "valid identities found" in line:
            try:
                return int(line.strip().split()[0])
            except ValueError:
                return 0
    return 0


def inspect_environment() -> dict[str, Any]:
    dev_dir = _run(["/usr/bin/xcode-select", "-p"])
    return {
        "macos": platform.mac_ver()[0],
        "arch": platform.machine(),
        "cpu": _run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]),
        "memory_gb": round(int(_run(["/usr/sbin/sysctl", "-n", "hw.memsize"]) or 0) / 2**30, 1),
        "python": sys.version.split()[0],
        "apple_silicon": platform.machine() == "arm64",
        "metal": metal_info(),
        "coreml": coreml_info(),
        "developer_dir": dev_dir,
        "xcode_installed": "Xcode" in dev_dir or bool(_run(["/usr/bin/mdfind", "kMDItemCFBundleIdentifier == 'com.apple.dt.Xcode'"])),
        "swift": _run(["/usr/bin/swift", "--version"]).splitlines()[0:1],
        "codesign_identities": signing_identities(),
        "ffmpeg": shutil.which("ffmpeg"),
        "camera_extensions": camera_extensions(),
    }
