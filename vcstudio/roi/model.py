"""ROI data model.

Geometry is stored normalised (0..1 of frame width/height) so profiles are resolution-independent.
Rectangles are stored as centre + size + rotation (degrees, clockwise in image coordinates).
Rotation is applied in *pixel* space, so a rotated rectangle stays rectangular at any aspect ratio.

Modes
  manual  the region is the drawn geometry
  auto    every tracked object that passes the ROI's class filter becomes a region (geometry unused)
  hybrid  the drawn geometry is a search area; eligible tracked objects inside it become regions
"""
from __future__ import annotations

import copy
import math
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

KINDS = ("rect", "polygon")
MODES = ("manual", "auto", "hybrid")
INCLUSION = ("center", "overlap")


class ROIError(ValueError):
    pass


@dataclass
class ROI:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = "Region"
    kind: str = "rect"
    mode: str = "manual"
    enabled: bool = True
    priority: int = 0                        # higher = composited later (on top)
    # rect geometry (normalised)
    cx: float = 0.5
    cy: float = 0.5
    w: float = 0.25
    h: float = 0.25
    rotation: float = 0.0                    # degrees
    # polygon geometry (normalised), >= 3 points
    points: list[list[float]] = field(default_factory=list)
    # AI selection (auto / hybrid)
    classes: list[str] = field(default_factory=list)          # empty => any class
    exclude_classes: list[str] = field(default_factory=list)
    min_confidence: float = 0.0
    inclusion: str = "center"                                  # hybrid: center inside | overlap >= threshold
    overlap_threshold: float = 0.5
    max_objects: int = 0                                       # 0 => unlimited
    padding: float = 0.05                                      # grow object boxes by this fraction
    clip_to_area: bool = False                                 # hybrid: clip object regions to the area
    include_coasting: bool = True                              # keep regions for briefly-lost objects
    # Replacement settings (Stage 6/7) live here so each ROI is self-contained
    replacement: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ROI":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        r = cls(**copy.deepcopy(known))
        r.validate()
        return r

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise ROIError(f"kind must be one of {KINDS}")
        if self.mode not in MODES:
            raise ROIError(f"mode must be one of {MODES}")
        if self.inclusion not in INCLUSION:
            raise ROIError(f"inclusion must be one of {INCLUSION}")
        if not self.name.strip():
            raise ROIError("name must not be empty")
        if self.kind == "rect" and (self.w <= 0 or self.h <= 0):
            raise ROIError("rectangle width and height must be positive")
        if self.kind == "polygon" and self.mode != "auto":
            if len(self.points) < 3:
                raise ROIError("polygon needs at least 3 points")
            if polygon_area(np.array(self.points)) < 1e-6:
                raise ROIError("polygon has no area")
        if not 0 <= self.padding <= 2:
            raise ROIError("padding must be within 0..2")
        if self.max_objects < 0:
            raise ROIError("max_objects must be >= 0")

    def copy(self, **changes) -> "ROI":
        d = self.to_dict()
        d.update(changes)
        return ROI.from_dict(d)

    # -------------------------------------------------------------- geometry
    def polygon_px(self, width: int, height: int) -> np.ndarray:
        """(N, 2) polygon in pixel coordinates."""
        if self.kind == "polygon":
            return np.array(self.points, dtype=float) * [width, height]
        return rect_corners(self.cx * width, self.cy * height, self.w * width, self.h * height, self.rotation)

    def set_from_polygon_px(self, pts: np.ndarray, width: int, height: int) -> None:
        self.points = [[float(x) / width, float(y) / height] for x, y in pts]

    def bbox_px(self, width: int, height: int) -> tuple[float, float, float, float]:
        p = self.polygon_px(width, height)
        return float(p[:, 0].min()), float(p[:, 1].min()), float(p[:, 0].max()), float(p[:, 1].max())

    def translate(self, dx: float, dy: float) -> None:
        """Move by a normalised offset."""
        if self.kind == "polygon":
            self.points = [[x + dx, y + dy] for x, y in self.points]
        else:
            self.cx += dx
            self.cy += dy

    def eligible(self, label: str, confidence: float) -> bool:
        if self.classes and label not in self.classes:
            return False
        if label in self.exclude_classes:
            return False
        return confidence >= self.min_confidence


def rect_corners(cx: float, cy: float, w: float, h: float, rotation_deg: float) -> np.ndarray:
    """Corners (TL, TR, BR, BL) of a rotated rectangle in pixel space."""
    a = math.radians(rotation_deg)
    c, s = math.cos(a), math.sin(a)
    local = np.array([[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]])
    rot = np.array([[c, -s], [s, c]])
    return local @ rot.T + [cx, cy]


def polygon_area(p: np.ndarray) -> float:
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def new_rect(name: str, x0: float, y0: float, x1: float, y1: float) -> ROI:
    """From a normalised drag rectangle."""
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return ROI(name=name, kind="rect", cx=(x0 + x1) / 2, cy=(y0 + y1) / 2, w=x1 - x0, h=y1 - y0)


def new_polygon(name: str, pts: list[list[float]]) -> ROI:
    r = ROI(name=name, kind="polygon", points=[list(map(float, p)) for p in pts])
    r.validate()
    return r


def rois_from_profile(body: dict) -> list[ROI]:
    out = []
    for d in body.get("rois", []):
        try:
            out.append(ROI.from_dict(d))
        except (ROIError, TypeError):
            continue
    return out
