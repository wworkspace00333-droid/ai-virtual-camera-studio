"""Accuracy regression: the app's Core ML detectors on 16 labelled COCO images (tests/data/coco16).

Measured on an M2 Pro (2026-11-12): yolo11n mAP50 0.549, yolo11s 0.627, yolo11m 0.623,
yolo11n-seg 0.520, yolo11m-seg 0.623. Floors sit ~0.1 below, so FP16 / compute-unit noise passes
while a real preprocessing bug (colour order, letterbox, coordinate mapping) fails loudly.
"""
from pathlib import Path

import numpy as np
import pytest

from vcstudio.ai.evaluation import Box, average_precision, evaluate, iou_matrix, match_image
from vcstudio.ai.model_manager import REPO_MODELS

DATA = Path(__file__).parent / "data" / "coco16"
FLOORS = {"yolo11n": 0.45, "yolo11s": 0.52, "yolo11m": 0.52, "yolo11n-seg": 0.42, "yolo11m-seg": 0.52}


def test_metrics_on_hand_made_cases():
    a = np.array([[0, 0, 10, 10]], float)
    assert iou_matrix(a, np.array([[5, 0, 15, 10]], float))[0, 0] == pytest.approx(1 / 3)
    gt = [Box("cat", 0, 0, 10, 10)]
    dets = [Box("cat", 0, 0, 10, 10, 0.9), Box("cat", 1, 1, 11, 11, 0.8), Box("dog", 0, 0, 10, 10, 0.7)]
    m = match_image(dets, gt, 0.5)
    assert sorted(m) == [("cat", 0.8, False), ("cat", 0.9, True), ("dog", 0.7, False)]   # duplicate = FP
    assert average_precision(np.array([0.9, 0.8]), np.array([True, False]), 1) == pytest.approx(1.0)
    r = evaluate([(dets, gt)])
    assert r.map50 == pytest.approx(1.0) and r.gt_boxes == 1
    assert r.at(0.75)["precision"] == pytest.approx(0.5) and r.at(0.75)["recall"] == 1.0


@pytest.mark.parametrize("name", sorted(FLOORS))
def test_model_accuracy_floor(name):
    if not (REPO_MODELS / f"{name}.mlpackage").exists():
        pytest.skip(f"{name} not converted")
    from vcstudio.ai.evaluation import evaluate_models
    res = evaluate_models(DATA, REPO_MODELS, [name])["models"][name]
    assert res["gt_boxes"] > 100
    assert res["map50"] >= FLOORS[name], f"{name} mAP50 {res['map50']:.3f} < floor {FLOORS[name]}"
