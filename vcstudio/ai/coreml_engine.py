"""Core ML (via Apple Vision) YOLO detector and Vision face detector.

Vision takes the CVPixelBuffer directly (no copy) and letterboxes it to the model input
(scaleFit), then maps boxes back to source-image coordinates. Verified against Ultralytics
PyTorch output on the reference image (tests/test_ai.py).
"""
from __future__ import annotations

import logging
import time

from vcstudio.ai.detection import Detection
from vcstudio.ai.labels import FACE
from vcstudio.ai.model_manager import LoadedModel
from vcstudio.core.pixelbuffer import pixel_buffer_for

log = logging.getLogger(__name__)


def _to_pixels(bb, w: int, h: int) -> tuple[float, float, float, float]:
    """Vision normalized rect (bottom-left origin) -> pixel x0, y0, x1, y1 (top-left origin)."""
    x0 = bb.origin.x * w
    x1 = (bb.origin.x + bb.size.width) * w
    y0 = (1.0 - bb.origin.y - bb.size.height) * h
    y1 = (1.0 - bb.origin.y) * h
    return max(0.0, x0), max(0.0, y0), min(float(w), x1), min(float(h), y1)


class CoreMLDetector:
    def __init__(self, loaded: LoadedModel):
        import Vision  # type: ignore
        self.loaded = loaded
        self.name = loaded.info.name
        vm, err = Vision.VNCoreMLModel.modelForMLModel_error_(loaded.mlmodel, None)
        if vm is None:
            raise RuntimeError(f"VNCoreMLModel failed: {err}")
        self._vm = vm
        self._req = Vision.VNCoreMLRequest.alloc().initWithModel_(vm)
        self._req.setImageCropAndScaleOption_(Vision.VNImageCropAndScaleOptionScaleFit)
        self._thresholds: tuple[float, float] | None = None
        self.class_names = loaded.info.classes

    def _set_thresholds(self, conf: float, iou: float) -> None:
        if self._thresholds == (conf, iou):
            return
        import CoreML  # type: ignore
        inputs = set(self.loaded.mlmodel.modelDescription().inputDescriptionsByName().keys())
        vals = {}
        if "confidenceThreshold" in inputs:
            vals["confidenceThreshold"] = CoreML.MLFeatureValue.featureValueWithDouble_(conf)
        if "iouThreshold" in inputs:
            vals["iouThreshold"] = CoreML.MLFeatureValue.featureValueWithDouble_(iou)
        if vals:
            fp, err = CoreML.MLDictionaryFeatureProvider.alloc().initWithDictionary_error_(vals, None)
            if fp is None:
                raise RuntimeError(f"threshold feature provider failed: {err}")
            self._vm.setFeatureProvider_(fp)
        self._thresholds = (conf, iou)

    def detect(self, frame, conf: float = 0.25, iou: float = 0.5) -> tuple[list[Detection], float]:
        import Vision  # type: ignore
        self._set_thresholds(conf, iou)
        pb = pixel_buffer_for(frame)
        handler = Vision.VNImageRequestHandler.alloc().initWithCVPixelBuffer_options_(pb, None)
        t0 = time.perf_counter()
        ok, err = handler.performRequests_error_([self._req], None)
        ms = (time.perf_counter() - t0) * 1000
        if not ok:
            raise RuntimeError(f"Vision request failed: {err}")
        out = []
        index = {n: i for i, n in enumerate(self.class_names)}
        for o in self._req.results() or []:
            labels = o.labels() if hasattr(o, "labels") else None
            if not labels:
                continue
            top = labels[0]
            name = str(top.identifier())
            x0, y0, x1, y1 = _to_pixels(o.boundingBox(), frame.width, frame.height)
            # observation.confidence() is the detection score; label confidences are normalised
            # across labels by Vision (always ~1.0 for single-label NMS output).
            out.append(Detection(name, float(o.confidence()), x0, y0, x1, y1, index.get(name, -1), "yolo"))
        return out, ms


class VisionFaceDetector:
    """Apple Vision face rectangles. Detection only: no identity recognition."""

    def __init__(self):
        import Vision  # type: ignore
        self._req = Vision.VNDetectFaceRectanglesRequest.alloc().init()

    def detect(self, frame) -> tuple[list[Detection], float]:
        import Vision  # type: ignore
        pb = pixel_buffer_for(frame)
        handler = Vision.VNImageRequestHandler.alloc().initWithCVPixelBuffer_options_(pb, None)
        t0 = time.perf_counter()
        ok, err = handler.performRequests_error_([self._req], None)
        ms = (time.perf_counter() - t0) * 1000
        if not ok:
            raise RuntimeError(f"Vision face request failed: {err}")
        out = []
        for o in self._req.results() or []:
            x0, y0, x1, y1 = _to_pixels(o.boundingBox(), frame.width, frame.height)
            out.append(Detection(FACE, float(o.confidence()), x0, y0, x1, y1, -1, "vision_face"))
        return out, ms
