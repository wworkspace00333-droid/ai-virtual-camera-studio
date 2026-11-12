"""Detection accuracy metrics against labelled ground truth (COCO/YOLO-style).

Pure NumPy, no framework dependency, so the app's own Core ML detectors can be measured exactly
as they run (see scripts/eval_accuracy.py).

Definitions (standard COCO/VOC practice):
  - A detection is a true positive at IoU threshold t if it overlaps an unmatched ground-truth
    box of the same class by IoU >= t; detections are matched greedily by descending score.
  - AP is the area under the precision/recall curve with all-point interpolation (VOC 2010+).
  - mAP50 is AP at IoU 0.5 averaged over classes present in the ground truth;
    mAP50-95 averages AP over IoU 0.50, 0.55, ..., 0.95.
  - Precision / recall / F1 at a confidence threshold use IoU 0.5 over all classes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

IOU_THRESHOLDS = np.round(np.arange(0.5, 0.96, 0.05), 2)


@dataclass
class Box:
    cls: str
    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 1.0


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU between every box in a (N,4) and b (M,4), boxes as x0,y0,x1,y1."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    x0 = np.maximum(a[:, None, 0], b[None, :, 0]); y0 = np.maximum(a[:, None, 1], b[None, :, 1])
    x1 = np.minimum(a[:, None, 2], b[None, :, 2]); y1 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x1 - x0, 0, None) * np.clip(y1 - y0, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1]); area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-9)


def match_image(dets: list[Box], gts: list[Box], iou_t: float) -> list[tuple[str, float, bool]]:
    """(class, score, is_true_positive) for every detection of one image."""
    out: list[tuple[str, float, bool]] = []
    for cls in {d.cls for d in dets}:
        d = sorted((x for x in dets if x.cls == cls), key=lambda x: -x.score)
        g = [x for x in gts if x.cls == cls]
        if not g:
            out += [(cls, x.score, False) for x in d]
            continue
        ious = iou_matrix(np.array([[x.x0, x.y0, x.x1, x.y1] for x in d]), np.array([[x.x0, x.y0, x.x1, x.y1] for x in g]))
        used = np.zeros(len(g), bool)
        for i, x in enumerate(d):
            cand = np.where(~used & (ious[i] >= iou_t))[0]
            if len(cand):
                j = cand[np.argmax(ious[i, cand])]
                used[j] = True
                out.append((cls, x.score, True))
            else:
                out.append((cls, x.score, False))
    return out


def average_precision(scores: np.ndarray, tp: np.ndarray, n_gt: int) -> float:
    if n_gt == 0:
        return float("nan")
    if len(scores) == 0:
        return 0.0
    order = np.argsort(-scores)
    tp = tp[order].astype(float)
    ctp, cfp = np.cumsum(tp), np.cumsum(1 - tp)
    recall = ctp / n_gt
    precision = ctp / np.maximum(ctp + cfp, 1e-9)
    mrec = np.concatenate(([0.0], recall, [1.0]))
    mpre = np.concatenate(([1.0], precision, [0.0]))
    mpre = np.maximum.accumulate(mpre[::-1])[::-1]
    idx = np.where(mrec[1:] != mrec[:-1])[0]
    return float(np.sum((mrec[idx + 1] - mrec[idx]) * mpre[idx + 1]))


@dataclass
class EvalResult:
    images: int = 0
    gt_boxes: int = 0
    map50: float = 0.0
    map50_95: float = 0.0
    per_class_ap50: dict[str, float] = field(default_factory=dict)
    # precision / recall / F1 at IoU 0.5 for each confidence threshold
    curve: list[dict] = field(default_factory=list)
    best_threshold: float = 0.0
    best_f1: float = 0.0

    def at(self, threshold: float) -> dict:
        return min(self.curve, key=lambda r: abs(r["conf"] - threshold))


def evaluate(samples: list[tuple[list[Box], list[Box]]],
             thresholds: np.ndarray = np.round(np.arange(0.05, 0.96, 0.05), 2)) -> EvalResult:
    """samples: (detections, ground truth) per image. Detections should be produced at a low
    confidence (e.g. 0.01) so the precision/recall curve and AP cover the whole range."""
    res = EvalResult(images=len(samples))
    gt_count: dict[str, int] = {}
    for _, gts in samples:
        for g in gts:
            gt_count[g.cls] = gt_count.get(g.cls, 0) + 1
    res.gt_boxes = sum(gt_count.values())
    aps_by_t = []
    for t in IOU_THRESHOLDS:
        rows = [m for dets, gts in samples for m in match_image(dets, gts, float(t))]
        aps = {}
        for cls, n in gt_count.items():
            r = [(s, p) for c, s, p in rows if c == cls]
            aps[cls] = average_precision(np.array([s for s, _ in r]), np.array([p for _, p in r], bool), n)
        aps_by_t.append(aps)
        if t == 0.5:
            res.per_class_ap50 = {k: round(v, 4) for k, v in sorted(aps.items())}
            all_s = np.array([s for _, s, _ in rows]); all_tp = np.array([p for _, _, p in rows], bool)
            for th in thresholds:
                keep = all_s >= th
                tp = int(all_tp[keep].sum()); fp = int(keep.sum() - tp)
                p = tp / max(tp + fp, 1); r = tp / max(res.gt_boxes, 1)
                f1 = 2 * p * r / max(p + r, 1e-9)
                res.curve.append({"conf": float(th), "precision": round(p, 4), "recall": round(r, 4),
                                  "f1": round(f1, 4), "tp": tp, "fp": fp})
    res.map50 = round(float(np.nanmean(list(aps_by_t[0].values()))), 4) if gt_count else 0.0
    res.map50_95 = round(float(np.nanmean([np.nanmean(list(a.values())) for a in aps_by_t])), 4) if gt_count else 0.0
    if res.curve:
        best = max(res.curve, key=lambda r: r["f1"])
        res.best_threshold, res.best_f1 = best["conf"], best["f1"]
    return res


def load_yolo_labels(path, names: list[str], width: int, height: int) -> list[Box]:
    """YOLO txt: `class cx cy w h` normalised -> pixel Boxes with class names."""
    out = []
    try:
        lines = open(path).read().split("\n")
    except FileNotFoundError:
        return out
    for ln in lines:
        p = ln.split()
        if len(p) < 5:
            continue
        c, cx, cy, w, h = int(p[0]), *map(float, p[1:5])
        out.append(Box(names[c], (cx - w / 2) * width, (cy - h / 2) * height, (cx + w / 2) * width, (cy + h / 2) * height))
    return out


def evaluate_models(data: Path, models_dir: Path, model_names: list[str] | None = None, conf: float = 0.01,
                    progress=None) -> dict:
    """Run the app's detectors (as the app runs them) over a YOLO-format dataset; see scripts/eval_accuracy.py."""
    from vcstudio.ai.coreml_engine import CoreMLDetector
    from vcstudio.ai.model_manager import ModelManager
    from vcstudio.ai.segmentation import SegmentationDetector
    from vcstudio.ai.testimages import load_bgra
    from vcstudio.core.frame import Frame
    from vcstudio.core.pyobjc_prewarm import prewarm

    prewarm()
    imgs = sorted(p for p in (data / "images").rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not imgs:
        raise ValueError(f"no images under {data}/images")
    mm = ModelManager(Path(models_dir))
    avail = mm.available()
    names = model_names or sorted(avail, key=lambda n: (avail[n].task, avail[n].size_mb))
    out: dict = {"dataset": str(data), "images": len(imgs), "models": {}}
    for name in names:
        info = avail[name]
        lm = mm.load(info, "all")
        det = SegmentationDetector(lm) if info.task == "segment" else CoreMLDetector(lm)
        samples, ms = [], []
        for p in imgs:
            img = load_bgra(p)
            h, w = img.shape[:2]
            frame = Frame(0, 0.0, 0.0, w, h, array=img, source="eval")
            dets, t = det.detect(frame, conf=conf, iou=0.5)
            ms.append(t)
            label = data / "labels" / p.relative_to(data / "images").with_suffix(".txt")
            gts = load_yolo_labels(label, list(info.classes), w, h)
            samples.append(([Box(d.label, d.x0, d.y0, d.x1, d.y1, d.confidence) for d in dets], gts))
        r = evaluate(samples)
        ms.sort()
        out["models"][name] = {
            "task": info.task, "size_mb": info.size_mb, "map50": r.map50, "map50_95": r.map50_95,
            "best_threshold": r.best_threshold, "best_f1": r.best_f1, "gt_boxes": r.gt_boxes,
            "median_ms": round(ms[len(ms) // 2], 2), "curve": r.curve, "per_class_ap50": r.per_class_ap50,
        }
        mm.unload()
        if progress:
            progress(name, out["models"][name])
    return out
