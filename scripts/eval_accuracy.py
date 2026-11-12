"""Measure detection accuracy of the app's own Core ML models on a labelled dataset.

    .venv/bin/python scripts/eval_accuracy.py [--data build/datasets/coco128] [--models yolo11n,yolo11s]

Dataset layout (YOLO format, e.g. Ultralytics COCO128):
    <data>/images/<split>/*.jpg   <data>/labels/<split>/*.txt   (class cx cy w h, normalised)
COCO128 (128 images, ~900 boxes, CC BY 4.0) can be fetched with:
    mkdir -p build/datasets && curl -L -o /tmp/coco128.zip \
      https://github.com/ultralytics/assets/releases/download/v0.0.0/coco128.zip && unzip /tmp/coco128.zip -d build/datasets

Runs each model exactly as the app does (Vision + Core ML, compute units "all"), at a low
confidence so the precision/recall curve covers the whole range, then prints mAP50, mAP50-95,
precision/recall/F1 at several confidence thresholds and the threshold with the best F1.
Writes build/accuracy.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(data: Path, model_names: list[str] | None, conf: float = 0.01) -> dict:
    from vcstudio.ai.evaluation import evaluate_models
    return evaluate_models(data, ROOT / "models", model_names, conf,
                           progress=lambda n, m: print(f"{n}: mAP50 {m['map50']:.3f}  mAP50-95 {m['map50_95']:.3f}  "
                                                      f"best F1 {m['best_f1']:.3f} at conf {m['best_threshold']:.2f}  "
                                                      f"({m['median_ms']:.1f} ms median)", flush=True))


def table(res: dict) -> str:
    rows = ["| Model | mAP50 | mAP50-95 | P / R @0.25 | P / R @0.35 | P / R @0.50 | Best F1 (conf) | Median ms |",
            "|---|---|---|---|---|---|---|---|"]
    for n, m in res["models"].items():
        def pr(c):
            r = min(m["curve"], key=lambda x: abs(x["conf"] - c))
            return f"{r['precision']:.2f} / {r['recall']:.2f}"
        rows.append(f"| {n} | {m['map50']:.3f} | {m['map50_95']:.3f} | {pr(.25)} | {pr(.35)} | {pr(.5)} | "
                    f"{m['best_f1']:.3f} ({m['best_threshold']:.2f}) | {m['median_ms']:.1f} |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "build" / "datasets" / "coco128"))
    ap.add_argument("--models", default="")
    a = ap.parse_args()
    t0 = time.time()
    res = run(Path(a.data).resolve(), [m for m in a.models.split(",") if m] or None)
    res["seconds"] = round(time.time() - t0, 1)
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "accuracy.json").write_text(json.dumps(res, indent=1))
    print()
    print(table(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
