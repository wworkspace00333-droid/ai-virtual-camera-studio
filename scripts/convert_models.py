"""Convert Ultralytics YOLO detection weights to Core ML (FP16, NMS pipeline) for the app.

Run with the conversion environment (keeps PyTorch out of the runtime app):
    .venv-convert/bin/python scripts/convert_models.py                # yolo11n + yolo11s
    .venv-convert/bin/python scripts/convert_models.py yolo11m --imgsz 640

Output: models/<name>.mlpackage + models/<name>.json (class names, input size, provenance).
Weights are fetched by Ultralytics from its GitHub releases on first use.
License note: Ultralytics YOLO weights/code are AGPL-3.0 (or Ultralytics Enterprise license).
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


def convert(name: str, imgsz: int, work: Path) -> None:
    from ultralytics import YOLO
    import ultralytics

    work.mkdir(parents=True, exist_ok=True)
    pt = work / f"{name}.pt"
    model = YOLO(str(pt) if pt.exists() else f"{name}.pt")
    if not pt.exists() and Path(f"{name}.pt").exists():
        shutil.move(f"{name}.pt", pt)
    t0 = time.time()
    task = getattr(model, "task", "detect")
    # Detection: NMS inside the Core ML pipeline (Vision-compatible). Segmentation: raw outputs
    # (boxes+coeffs, prototypes); NMS and mask assembly run in vcstudio/ai/segmentation.py.
    out = Path(model.export(format="coreml", nms=(task == "detect"), half=True, imgsz=imgsz))
    dest = MODELS / f"{name}.mlpackage"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(out), dest)
    names = model.names if isinstance(model.names, dict) else dict(enumerate(model.names))
    meta = {
        "name": name,
        "task": task,
        "format": "coreml-pipeline-nms" if task == "detect" else "coreml-raw-seg",
        "imgsz": imgsz,
        "precision": "fp16",
        "classes": [names[i] for i in sorted(names)],
        "source": f"Ultralytics {ultralytics.__version__} {name}.pt",
        "license": "AGPL-3.0 (Ultralytics)",
        "converted": time.strftime("%Y-%m-%d %H:%M:%S"),
        "convert_seconds": round(time.time() - t0, 1),
    }
    (MODELS / f"{name}.json").write_text(json.dumps(meta, indent=2))
    print(f"OK {dest} ({len(meta['classes'])} classes)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*", default=["yolo11n", "yolo11s", "yolo11n-seg"])
    ap.add_argument("--imgsz", type=int, default=640)
    a = ap.parse_args()
    MODELS.mkdir(exist_ok=True)
    for m in a.models:
        convert(m, a.imgsz, ROOT / "build" / "weights")


if __name__ == "__main__":
    main()
