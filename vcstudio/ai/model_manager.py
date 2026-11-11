"""Core ML model registry: discovery, compilation cache, loading with compute-unit selection.

Models live in <repo>/models (bundled) and <app data>/models (user-added). Each model is a
`<name>.mlpackage` with a sibling `<name>.json` (see scripts/convert_models.py). Compiled
`.mlmodelc` bundles are cached under <app data>/models/compiled/ keyed by package mtime.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

from vcstudio.core.resources import models_dir

REPO_MODELS = models_dir()          # bundled models (repository models/ in development)

COMPUTE_UNITS = {
    "all": "CPU + GPU + Neural Engine",
    "cpu_and_ne": "CPU + Neural Engine",
    "cpu_and_gpu": "CPU + GPU",
    "cpu_only": "CPU only",
}


def _cu_constant(key: str):
    import CoreML  # type: ignore
    return {
        "all": CoreML.MLComputeUnitsAll,
        "cpu_and_ne": CoreML.MLComputeUnitsCPUAndNeuralEngine,
        "cpu_and_gpu": CoreML.MLComputeUnitsCPUAndGPU,
        "cpu_only": CoreML.MLComputeUnitsCPUOnly,
    }[key]


class ModelError(RuntimeError):
    pass


@dataclass
class ModelInfo:
    name: str
    package: Path
    task: str = "detect"
    imgsz: int = 640
    classes: list[str] = field(default_factory=list)
    source: str = ""
    license: str = ""
    size_mb: float = 0.0

    @classmethod
    def from_package(cls, pkg: Path) -> "ModelInfo":
        meta_path = pkg.with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        size = sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file()) / 2**20
        return cls(name=pkg.stem, package=pkg, task=meta.get("task", "detect"), imgsz=meta.get("imgsz", 640),
                   classes=meta.get("classes", []), source=meta.get("source", ""),
                   license=meta.get("license", ""), size_mb=round(size, 1))


@dataclass
class LoadedModel:
    info: ModelInfo
    mlmodel: object
    compute_units: str
    load_seconds: float


class ModelManager:
    def __init__(self, user_models_dir: Path, extra_dirs: list[Path] | None = None):
        self.user_dir = user_models_dir
        self.dirs = [REPO_MODELS, user_models_dir] + list(extra_dirs or [])
        self.cache_dir = user_models_dir / "compiled"
        self._lock = threading.Lock()
        self._loaded: dict[str, LoadedModel] = {}

    def available(self, max_age_s: float = 5.0) -> dict[str, ModelInfo]:
        """Installed models (directory scan cached for `max_age_s`; it was measured on the hot path)."""
        now = time.monotonic()
        cached = getattr(self, "_avail_cache", None)
        if cached and now - cached[0] < max_age_s:
            return dict(cached[1])
        out: dict[str, ModelInfo] = {}
        for d in self.dirs:
            if d.is_dir():
                for pkg in sorted(d.glob("*.mlpackage")):
                    try:
                        out.setdefault(pkg.stem, ModelInfo.from_package(pkg))
                    except Exception as e:
                        log.warning("Ignoring model %s: %s", pkg, e)
        self._avail_cache = (now, out)
        return dict(out)

    def resolve(self, name: str, task: str = "detect", prefer_large: bool = False) -> ModelInfo:
        models = {k: v for k, v in self.available().items() if v.task == task}
        if not models:
            raise ModelError(f"No {task} models found. Run: .venv-convert/bin/python scripts/convert_models.py")
        if name in ("auto", "", None):
            ranked = sorted(models.values(), key=lambda m: m.size_mb)
            return ranked[-1] if prefer_large else ranked[0]
        if name not in models:
            raise ModelError(f"Model {name!r} not found (available: {', '.join(sorted(models))})")
        return models[name]

    def ranked(self, task: str = "detect") -> list[ModelInfo]:
        """Models of a task from largest (highest quality) to smallest."""
        return sorted((m for m in self.available().values() if m.task == task), key=lambda m: -m.size_mb)

    def _compiled_path(self, info: ModelInfo) -> Path:
        import CoreML  # type: ignore
        import Foundation  # type: ignore
        mtime = int(max(f.stat().st_mtime for f in info.package.rglob("*") if f.is_file()))
        target = self.cache_dir / f"{info.name}-{mtime}.mlmodelc"
        if target.exists():
            return target
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        url, err = CoreML.MLModel.compileModelAtURL_error_(Foundation.NSURL.fileURLWithPath_(str(info.package)), None)
        if url is None:
            raise ModelError(f"Core ML compile failed for {info.name}: {err}")
        for old in self.cache_dir.glob(f"{info.name}-*.mlmodelc"):
            shutil.rmtree(old, ignore_errors=True)
        shutil.move(str(url.path()), target)
        log.info("Compiled %s in %.2f s", info.name, time.perf_counter() - t0)
        return target

    def load(self, info: ModelInfo, compute_units: str = "all") -> LoadedModel:
        import CoreML  # type: ignore
        import Foundation  # type: ignore
        key = f"{info.name}:{compute_units}"
        with self._lock:
            if key in self._loaded:
                return self._loaded[key]
            path = self._compiled_path(info)
            cfg = CoreML.MLModelConfiguration.alloc().init()
            cfg.setComputeUnits_(_cu_constant(compute_units))
            t0 = time.perf_counter()
            model, err = CoreML.MLModel.modelWithContentsOfURL_configuration_error_(
                Foundation.NSURL.fileURLWithPath_(str(path)), cfg, None)
            if model is None:
                raise ModelError(f"Core ML load failed for {info.name}: {err}")
            lm = LoadedModel(info, model, compute_units, time.perf_counter() - t0)
            self._loaded[key] = lm
            log.info("Loaded %s (%s) in %.2f s", info.name, COMPUTE_UNITS[compute_units], lm.load_seconds)
            return lm

    def unload(self, name: str | None = None) -> None:
        """Drop references so Core ML can free memory (all models when name is None)."""
        with self._lock:
            for k in list(self._loaded):
                if name is None or k.split(":")[0] == name:
                    del self._loaded[k]

    def loaded(self) -> list[str]:
        with self._lock:
            return list(self._loaded)
