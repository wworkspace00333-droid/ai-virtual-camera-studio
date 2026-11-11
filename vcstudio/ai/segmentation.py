"""Instance segmentation (YOLO11-seg, Core ML) producing per-object silhouette masks.

The model is a detector too (boxes + classes + masks), so when any ROI needs object masks the
AI engine uses it *instead of* the plain detector: one model resident, one inference per frame.

Pipeline: letterbox to 640x640 (OpenCV) -> Core ML predict (raw outputs) -> class-aware NMS
(NumPy) -> mask = sigmoid(coeffs @ prototypes) cropped to the box -> mapped back to frame pixels.
Masks are stored box-local (uint8, at most MASK_MAX px on the long side) so a tracked object's
mask can be re-projected onto its predicted box at every output frame.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from vcstudio.ai.detection import Detection
from vcstudio.ai.model_manager import LoadedModel
from vcstudio.core.pixelbuffer import PixelBufferPool

MASK_MAX = 256           # box-local mask resolution cap (memory/CPU bound)
PAD_VALUE = 114


def _ml_to_numpy(fv) -> np.ndarray:
    import CoreML  # type: ignore
    a = fv.multiArrayValue()
    shape = [int(x) for x in a.shape()]
    n = int(np.prod(shape))
    dt = int(a.dataType())
    if dt == int(CoreML.MLMultiArrayDataTypeFloat32):
        dtype, size = np.float32, 4
    elif dt == int(getattr(CoreML, "MLMultiArrayDataTypeFloat16", 65552)):
        dtype, size = np.float16, 2
    else:
        raise RuntimeError(f"unsupported MLMultiArray data type {dt}")
    buf = a.dataPointer().as_buffer(n * size)
    return np.frombuffer(buf, dtype=dtype, count=n).reshape(shape).astype(np.float32)


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx0 = np.maximum(boxes[i, 0], boxes[rest, 0]); yy0 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx1 = np.minimum(boxes[i, 2], boxes[rest, 2]); yy1 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        a = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        b = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        order = rest[inter / np.maximum(a + b - inter, 1e-9) <= iou]
    return keep


class SegmentationDetector:
    max_det = 50

    def __init__(self, loaded: LoadedModel):
        self.loaded = loaded
        self.name = loaded.info.name
        self.class_names = loaded.info.classes
        self.size = loaded.info.imgsz
        self._pool = PixelBufferPool(self.size, self.size)
        outs = loaded.mlmodel.modelDescription().outputDescriptionsByName()
        self._out_names = list(outs.keys())
        self._canvas = np.full((self.size, self.size, 4), PAD_VALUE, np.uint8)
        self._canvas[..., 3] = 255

    def _letterbox(self, img: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        h, w = img.shape[:2]
        s = self.size
        r = min(s / w, s / h)
        nw, nh = int(round(w * r)), int(round(h * r))
        px, py = (s - nw) // 2, (s - nh) // 2
        c = self._canvas
        c[..., :3] = PAD_VALUE
        c[py:py + nh, px:px + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        return c, r, px, py

    def _letterbox_gpu(self, frame):
        """Letterbox on the GPU straight into a 640x640 IOSurface (no CPU read of the 1080p frame)."""
        from vcstudio.native.metal import scaler_context
        ctx = scaler_context()
        if ctx is None or frame.pixel_buffer is None:
            return None
        import Quartz as Q  # type: ignore
        s = self.size
        r = min(s / frame.width, s / frame.height)
        nw, nh = int(round(frame.width * r)), int(round(frame.height * r))
        px, py = (s - nw) // 2, (s - nh) // 2
        err, pb = Q.CVPixelBufferPoolCreatePixelBuffer(None, self._pool._pool, None)
        if err != 0:
            return None
        ctx.scale(frame.pixel_buffer, pb, px, py, nw, nh, PAD_VALUE / 255.0)
        return pb, r, px, py

    def detect(self, frame, conf: float = 0.25, iou: float = 0.5) -> tuple[list[Detection], float]:
        import CoreML  # type: ignore
        t0 = time.perf_counter()
        gpu = None
        try:
            gpu = self._letterbox_gpu(frame)
        except RuntimeError:
            gpu = None
        if gpu is not None:
            pb, r, px, py = gpu
        else:
            with frame.bgra() as img:
                lb, r, px, py = self._letterbox(img)
            pb = self._pool.from_array(lb)
        fp, err = CoreML.MLDictionaryFeatureProvider.alloc().initWithDictionary_error_(
            {"image": CoreML.MLFeatureValue.featureValueWithPixelBuffer_(pb)}, None)
        out, err = self.loaded.mlmodel.predictionFromFeatures_error_(fp, None)
        if out is None:
            raise RuntimeError(f"Core ML segmentation failed: {err}")
        arrays = [_ml_to_numpy(out.featureValueForName_(n)) for n in self._out_names]
        pred = next(a for a in arrays if a.ndim == 3)[0]      # (4 + nc + nm, N)
        protos = next(a for a in arrays if a.ndim == 4)[0]    # (nm, mh, mw)
        dets = self.postprocess(pred, protos, conf, iou, r, px, py, frame.width, frame.height)
        return dets, (time.perf_counter() - t0) * 1000

    def postprocess(self, pred, protos, conf, iou, r, px, py, W, H) -> list[Detection]:
        nm = protos.shape[0]
        nc = pred.shape[0] - 4 - nm
        cls_scores = pred[4:4 + nc]
        score = cls_scores.max(0)
        keep = score >= conf
        if not keep.any():
            return []
        cls = cls_scores[:, keep].argmax(0)
        xywh = pred[:4, keep].T
        score, coeffs = score[keep], pred[4 + nc:, keep].T
        boxes = np.empty_like(xywh)
        boxes[:, 0] = xywh[:, 0] - xywh[:, 2] / 2; boxes[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
        boxes[:, 2] = xywh[:, 0] + xywh[:, 2] / 2; boxes[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
        # class-aware NMS via coordinate offset trick
        offs = boxes + (cls[:, None] * 4096.0)
        idx = nms(offs, score, iou)[: self.max_det]
        mh, mw = protos.shape[1:]
        s = self.size
        out = []
        flat = protos.reshape(nm, -1)
        for i in idx:
            b = boxes[i]
            # frame-pixel box
            x0 = float(np.clip((b[0] - px) / r, 0, W)); y0 = float(np.clip((b[1] - py) / r, 0, H))
            x1 = float(np.clip((b[2] - px) / r, 0, W)); y1 = float(np.clip((b[3] - py) / r, 0, H))
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            # prototype-space crop of the (letterboxed) box
            m = 1.0 / (1.0 + np.exp(-(coeffs[i] @ flat))).reshape(mh, mw)
            sx, sy = mw / s, mh / s
            cx0, cy0 = max(0, int(np.floor(b[0] * sx))), max(0, int(np.floor(b[1] * sy)))
            cx1, cy1 = min(mw, int(np.ceil(b[2] * sx))), min(mh, int(np.ceil(b[3] * sy)))
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            crop = m[cy0:cy1, cx0:cx1]
            # map the crop window (letterbox px) to frame px, then resample to the frame box
            fx0 = (cx0 / sx - px) / r; fy0 = (cy0 / sy - py) / r
            fx1 = (cx1 / sx - px) / r; fy1 = (cy1 / sy - py) / r
            bw, bh = x1 - x0, y1 - y0
            scale = min(1.0, MASK_MAX / max(bw, bh))
            ow, oh = max(2, int(round(bw * scale))), max(2, int(round(bh * scale)))
            # affine: output pixel (u, v) in box-local mask -> crop coordinates
            ax = (crop.shape[1] / (fx1 - fx0)) / scale
            ay = (crop.shape[0] / (fy1 - fy0)) / scale
            M = np.array([[ax, 0, (x0 - fx0) * crop.shape[1] / (fx1 - fx0) + 0.5 * ax - 0.5],
                          [0, ay, (y0 - fy0) * crop.shape[0] / (fy1 - fy0) + 0.5 * ay - 0.5]], np.float32)
            local = cv2.warpAffine(crop.astype(np.float32), M, (ow, oh),
                                   flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT)
            mask = (np.clip(local, 0, 1) * 255).astype(np.uint8)
            name = self.class_names[int(cls[i])] if int(cls[i]) < len(self.class_names) else str(int(cls[i]))
            out.append(Detection(name, float(score[i]), x0, y0, x1, y1, int(cls[i]), "yolo-seg", mask=mask))
        return out
