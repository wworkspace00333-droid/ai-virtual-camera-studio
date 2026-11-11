"""Resolve ROIs + tracked objects into concrete regions for one output frame.

Output regions are ordered by ROI priority (low first, so higher priority composites on top),
and within an ROI by track ID (stable order => no flicker). Stage 6+ consumes these.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from vcstudio.roi.geometry import box_polygon, clip_polygon, is_convex, overlap_fraction, point_in_polygon
from vcstudio.roi.model import ROI, rois_from_profile


@dataclass(frozen=True, eq=False)
class ActiveRegion:
    roi_id: str
    roi_name: str
    mode: str
    priority: int
    polygon: np.ndarray                      # (N, 2) pixels
    bbox: tuple[float, float, float, float]  # pixels
    rotation: float = 0.0                    # degrees (manual/hybrid rect geometry)
    track_id: int | None = None
    label: str | None = None
    confidence: float | None = None
    state: str = "static"                    # static | tracked | coasting
    mask: object = None                      # uint8 box-local silhouette (object regions with segmentation)
    object_box: tuple[float, float, float, float] | None = None   # unpadded object box the mask belongs to

    @property
    def is_object(self) -> bool:
        return self.track_id is not None


def _pad_box(o, pad: float, width: int, height: int) -> tuple[float, float, float, float]:
    w, h = o.x1 - o.x0, o.y1 - o.y0
    x0, y0 = o.x0 - w * pad, o.y0 - h * pad
    x1, y1 = o.x1 + w * pad, o.y1 + h * pad
    return max(0.0, x0), max(0.0, y0), min(float(width), x1), min(float(height), y1)


def resolve(rois: list[ROI], tracked: list, width: int, height: int) -> list[ActiveRegion]:
    regions: list[ActiveRegion] = []
    ordered = sorted((r for r in rois if r.enabled), key=lambda r: (r.priority, r.name))
    for roi in ordered:
        if roi.mode == "manual":
            poly = roi.polygon_px(width, height)
            regions.append(ActiveRegion(roi.id, roi.name, roi.mode, roi.priority, poly,
                                        (poly[:, 0].min(), poly[:, 1].min(), poly[:, 0].max(), poly[:, 1].max()),
                                        roi.rotation if roi.kind == "rect" else 0.0))
            continue
        area = roi.polygon_px(width, height) if roi.mode == "hybrid" else None
        candidates = []
        for o in sorted(tracked, key=lambda o: o.track_id):
            if not roi.eligible(o.label, o.confidence):
                continue
            if o.state == "coasting" and not roi.include_coasting:
                continue
            if area is not None:
                if roi.inclusion == "center":
                    if not point_in_polygon(*o.center, area):
                        continue
                elif overlap_fraction((o.x0, o.y0, o.x1, o.y1), area) < roi.overlap_threshold:
                    continue
            candidates.append(o)
        if roi.max_objects:
            # keep the most confident, then restore stable ID order
            candidates = sorted(sorted(candidates, key=lambda o: -o.confidence)[: roi.max_objects],
                                key=lambda o: o.track_id)
        for o in candidates:
            box = _pad_box(o, roi.padding, width, height)
            poly = box_polygon(*box)
            if area is not None and roi.clip_to_area and is_convex(area):
                poly = clip_polygon(poly, area)
                if len(poly) < 3:
                    continue
            if poly[:, 0].max() - poly[:, 0].min() < 1 or poly[:, 1].max() - poly[:, 1].min() < 1:
                continue
            regions.append(ActiveRegion(roi.id, roi.name, roi.mode, roi.priority, poly,
                                        (poly[:, 0].min(), poly[:, 1].min(), poly[:, 0].max(), poly[:, 1].max()),
                                        0.0, o.track_id, o.label, o.confidence, o.state,
                                        getattr(o, "mask", None), (o.x0, o.y0, o.x1, o.y1)))
    return regions


class RoiService:
    """Holds the active profile's ROIs and resolves them on demand (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rois: list[ROI] = []

    def load_profile(self, body: dict) -> None:
        rois = rois_from_profile(body)
        with self._lock:
            self._rois = rois

    def rois(self) -> list[ROI]:
        with self._lock:
            return list(self._rois)

    def resolve(self, tracked: list, width: int, height: int) -> list[ActiveRegion]:
        return resolve(self.rois(), tracked, width, height)
