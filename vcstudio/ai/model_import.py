"""Import a user's own Core ML model: validate, measure, describe, install.

Supported layouts (the two the app's detectors run):
  detect   a Core ML pipeline with NMS whose outputs are `coordinates` (N x 4) and `confidence` (N x C),
           as exported by Ultralytics `format=coreml, nms=True` (any YOLO version/size/class list).
           Vision returns object observations with class labels.
  segment  raw YOLO-seg outputs: (1, 4 + C + M, N) predictions and (1, M, H, W) mask prototypes.

Validation actually runs the model the way the app does (Vision / Core ML on the sample image) and
measures it, so a model that loads but cannot run, or whose class list does not match its outputs,
is rejected before it is installed. Nothing is changed in the models folder until install().
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vcstudio.ai.model_manager import LoadedModel, ModelError, ModelInfo

NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


class ModelImportError(ModelError):
    pass


@dataclass
class ModelReport:
    source: str
    task: str
    format: str
    imgsz: int
    classes: list[str]
    inputs: dict[str, str]
    outputs: dict[str, list[int]]
    sample_detections: int = 0
    sample_labels: list[str] = field(default_factory=list)
    median_ms: float = 0.0
    p95_ms: float = 0.0
    author: str = ""
    license: str = ""
    description: str = ""
    suggested_name: str = ""
    suggested_confidence: float = 0.25

    def as_dict(self) -> dict:
        return asdict(self)


def _shape(desc) -> list[int]:
    try:
        c = desc.multiArrayConstraint()
        return [int(x) for x in c.shape()] if c is not None else []
    except Exception:  # noqa: BLE001 - non-array output
        return []


def _class_names(md, user_meta: dict) -> list[str]:
    labels = md.classLabels()
    if labels:
        return [str(x) for x in labels]
    names = user_meta.get("names")
    if names:
        try:
            d = ast.literal_eval(str(names))
            if isinstance(d, dict):
                return [str(d[k]) for k in sorted(d, key=int)]
            if isinstance(d, (list, tuple)):
                return [str(x) for x in d]
        except (ValueError, SyntaxError):
            pass
    return []


def _compile(src: Path) -> Path:
    import CoreML  # type: ignore
    import Foundation  # type: ignore
    url, err = CoreML.MLModel.compileModelAtURL_error_(Foundation.NSURL.fileURLWithPath_(str(src)), None)
    if url is None:
        raise ModelImportError(f"not a Core ML model, or it does not compile: {err}")
    return Path(str(url.path()))


def inspect_model(src: Path, sample=None, runs: int = 20) -> ModelReport:
    """Compile, load and run `src` (.mlpackage directory or .mlmodel file); raise ModelImportError if
    the app cannot use it."""
    import CoreML  # type: ignore
    import Foundation  # type: ignore
    from vcstudio.core.pyobjc_prewarm import prewarm
    prewarm()
    src = Path(src).expanduser()
    if not src.exists():
        raise ModelImportError(f"{src} does not exist")
    if src.suffix not in (".mlpackage", ".mlmodel"):
        raise ModelImportError("choose a .mlpackage or .mlmodel file (Core ML)")
    compiled = _compile(src)
    try:
        cfg = CoreML.MLModelConfiguration.alloc().init()
        model, err = CoreML.MLModel.modelWithContentsOfURL_configuration_error_(
            Foundation.NSURL.fileURLWithPath_(str(compiled)), cfg, None)
        if model is None:
            raise ModelImportError(f"Core ML cannot load it: {err}")
        md = model.modelDescription()
        ins = {str(k): v for k, v in md.inputDescriptionsByName().items()}
        outs = {str(k): v for k, v in md.outputDescriptionsByName().items()}
        images = [v for v in ins.values() if v.imageConstraint() is not None]
        if not images:
            raise ModelImportError("the model has no image input")
        ic = images[0].imageConstraint()
        imgsz = int(max(ic.pixelsWide(), ic.pixelsHigh()))
        meta = dict(md.metadata() or {})
        user_meta = {str(k): str(v) for k, v in dict(meta.get("MLModelCreatorDefinedKey") or {}).items()}
        classes = _class_names(md, user_meta)
        shapes = {k: _shape(v) for k, v in outs.items()}
        if {"coordinates", "confidence"} <= set(outs):
            task, fmt = "detect", "coreml-pipeline-nms"
            if not classes:
                raise ModelImportError("detector without class names (no classLabels and no 'names' metadata)")
            c = shapes.get("confidence") or []
            if c and c[-1] > 0 and c[-1] != len(classes):
                raise ModelImportError(f"confidence output has {c[-1]} classes but the model names {len(classes)}")
        else:
            arrays = sorted(((k, s) for k, s in shapes.items() if s), key=lambda kv: len(kv[1]))
            pred = next((s for _, s in arrays if len(s) == 3), None)
            protos = next((s for _, s in arrays if len(s) == 4), None)
            if pred is None or protos is None:
                raise ModelImportError(
                    "unsupported outputs " + ", ".join(f"{k}{s}" for k, s in shapes.items()) +
                    ": export a detector with NMS (Ultralytics format=coreml nms=True) or a raw YOLO-seg model")
            nm = protos[1]
            nc = pred[1] - 4 - nm
            if nc <= 0:
                raise ModelImportError(f"segmentation outputs do not fit YOLO-seg: {pred} and {protos}")
            if not classes:
                raise ModelImportError("segmentation model without class names ('names' metadata)")
            if nc != len(classes):
                raise ModelImportError(f"outputs have {nc} classes but the model names {len(classes)}")
            task, fmt = "segment", "coreml-raw-seg"
        rep = ModelReport(
            source=str(src), task=task, format=fmt, imgsz=imgsz, classes=classes,
            inputs={k: str(v.type()) for k, v in ins.items()}, outputs=shapes,
            author=str(meta.get("MLModelAuthorKey", "") or ""), license=str(meta.get("MLModelLicenseKey", "") or ""),
            description=str(meta.get("MLModelDescriptionKey", "") or ""),
            suggested_name=_clean_name(src.stem),
            suggested_confidence=float(user_meta.get("Confidence threshold", 0.25) or 0.25))
        _trial_run(rep, src, model, sample, runs)
        return rep
    finally:
        shutil.rmtree(compiled, ignore_errors=True)


def _trial_run(rep: ModelReport, src: Path, model, sample, runs: int) -> None:
    """Run the model exactly as the app does on a sample frame and time it."""
    from vcstudio.ai.coreml_engine import CoreMLDetector
    from vcstudio.ai.segmentation import SegmentationDetector
    from vcstudio.core.frame import Frame
    if sample is None:
        sample = _sample()
    h, w = sample.shape[:2]
    info = ModelInfo(name=rep.suggested_name or "import", package=src, task=rep.task, imgsz=rep.imgsz,
                     classes=rep.classes)
    lm = LoadedModel(info, model, "all", 0.0)
    try:
        det = SegmentationDetector(lm) if rep.task == "segment" else CoreMLDetector(lm)
        frame = Frame(0, 0.0, 0.0, w, h, array=sample, source="import")
        ms = []
        dets = []
        for _ in range(max(3, runs)):
            dets, t = det.detect(frame, conf=0.25, iou=0.5)
            ms.append(t)
    except ModelImportError:
        raise
    except Exception as e:  # noqa: BLE001 - any failure here means the app could not run it
        raise ModelImportError(f"the model loads but fails when run on a frame: {type(e).__name__}: {e}") from e
    ms = sorted(ms[2:])                        # first runs include warm-up
    rep.median_ms = round(ms[len(ms) // 2], 2)
    rep.p95_ms = round(ms[int(0.95 * (len(ms) - 1))], 2)
    rep.sample_detections = len(dets)
    rep.sample_labels = sorted({d.label for d in dets})
    bad = [lbl for lbl in rep.sample_labels if lbl not in rep.classes]
    if bad:
        raise ModelImportError(f"the model returned labels that are not in its class list: {bad}")


def _clean_name(stem: str) -> str:
    s = "".join(ch if ch in NAME_OK else "-" for ch in stem.strip()).strip("-.") or "model"
    return s[:60]


def install(rep: ModelReport, models_dir: Path, name: str | None = None, class_map: dict[str, str] | None = None,
            confidence: float | None = None, builtin_names: set[str] | None = None) -> ModelInfo:
    """Copy the validated model into the user's models folder with its metadata (.json beside it)."""
    name = _clean_name(name or rep.suggested_name)
    if builtin_names and name in builtin_names:
        raise ModelImportError(f"“{name}” is the name of a built-in model; choose another name")
    src = Path(rep.source)
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = models_dir / f"{name}{src.suffix}"
    if dest.exists() or dest.with_suffix(".json").exists():
        raise ModelImportError(f"a model named “{name}” is already installed; remove it first or choose another name")
    class_map = {str(k): str(v).strip() for k, v in (class_map or {}).items() if str(v).strip() and k in rep.classes}
    conf = float(confidence if confidence is not None else rep.suggested_confidence)
    if not 0.01 <= conf <= 0.99:
        raise ModelImportError("confidence threshold must be between 0.01 and 0.99")
    tmp = Path(tempfile.mkdtemp(dir=models_dir)) / dest.name
    try:
        if src.is_dir():
            shutil.copytree(src, tmp)
        else:
            shutil.copy2(src, tmp)
        meta = {"name": name, "task": rep.task, "format": rep.format, "imgsz": rep.imgsz,
                "classes": [class_map.get(c, c) for c in rep.classes], "source_classes": rep.classes,
                "class_map": class_map, "confidence": round(conf, 3), "origin": "user",
                "source": Path(rep.source).name, "author": rep.author, "license": rep.license,
                "description": rep.description, "imported": _dt.datetime.now().isoformat(timespec="seconds"),
                "benchmark": {"median_ms": rep.median_ms, "p95_ms": rep.p95_ms, "machine": _machine()}}
        tmp.rename(dest)          # atomic within the folder: a half-copied model is never visible
        dest.with_suffix(".json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return ModelInfo.from_package(dest)


def remove(info: ModelInfo) -> None:
    """Delete an imported model (built-in models cannot be removed)."""
    if info.origin != "user":
        raise ModelImportError(f"{info.name} is a built-in model")
    if info.package.is_dir():
        shutil.rmtree(info.package)
    else:
        info.package.unlink(missing_ok=True)
    info.package.with_suffix(".json").unlink(missing_ok=True)


def _sample():
    from vcstudio.ai.testimages import load_bgra
    from vcstudio.core.resources import sample
    return load_bgra(sample("bus.jpg"))


def _machine() -> str:
    import subprocess
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def benchmark(lm: LoadedModel, sample=None, runs: int = 30) -> dict:
    """Median / p95 ms of a loaded model on the sample image (AI page "Benchmark")."""
    from vcstudio.ai.coreml_engine import CoreMLDetector
    from vcstudio.ai.segmentation import SegmentationDetector
    from vcstudio.core.frame import Frame
    if sample is None:
        sample = _sample()
    h, w = sample.shape[:2]
    det = SegmentationDetector(lm) if lm.info.task == "segment" else CoreMLDetector(lm)
    frame = Frame(0, 0.0, 0.0, w, h, array=sample, source="bench")
    ms = []
    t_end = time.perf_counter() + 30
    for _ in range(runs):
        ms.append(det.detect(frame, conf=0.25, iou=0.5)[1])
        if time.perf_counter() > t_end:
            break
    ms = sorted(ms[2:] or ms)
    return {"median_ms": round(ms[len(ms) // 2], 2), "p95_ms": round(ms[int(0.95 * (len(ms) - 1))], 2)}
