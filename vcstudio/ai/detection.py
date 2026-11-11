"""Detection data types and filtering."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, eq=False)
class Detection:
    label: str
    confidence: float
    x0: float          # pixel coordinates in the source frame (top-left origin)
    y0: float
    x1: float
    y1: float
    class_id: int = -1
    source: str = "yolo"   # yolo | yolo-seg | vision_face
    track_id: int | None = None
    mask: object = None    # uint8 (h, w) box-local silhouette (0..255), segmentation models only

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def normalized(self, width: int, height: int) -> tuple[float, float, float, float]:
        return (self.x0 / width, self.y0 / height, self.x1 / width, self.y1 / height)


@dataclass
class DetectionResult:
    frame_seq: int
    frame_pts: float
    width: int
    height: int
    detections: list[Detection]
    inference_ms: float
    face_ms: float = 0.0
    model: str = ""
    completed: float = 0.0     # host time
    raw_count: int = 0         # before class filtering
    tracked: list = field(default_factory=list)   # TrackedObject list at frame_pts (when tracking on)


@dataclass
class DetectionConfig:
    enabled: bool = True
    detector_model: str = "auto"
    compute_units: str = "all"
    confidence: float = 0.35
    iou: float = 0.5
    include: list[str] = field(default_factory=list)   # empty => all
    exclude: list[str] = field(default_factory=list)
    face_detection: bool = False
    frequency_mode: str = "auto"                      # auto (performance mode) | fixed
    fixed_fps: float = 15.0
    segmentation_model: str = "auto"
    need_masks: bool = False                          # some ROI uses object (silhouette) replacement

    @classmethod
    def from_profile(cls, body: dict) -> "DetectionConfig":
        ai, det = body.get("ai", {}), body.get("detection", {})
        return cls(
            enabled=bool(ai.get("enabled", True)),
            detector_model=ai.get("detector_model") or "auto",
            compute_units=ai.get("compute_units", "all"),
            confidence=float(det.get("confidence", 0.35)),
            iou=float(det.get("iou", 0.5)),
            include=list(det.get("include_classes", [])),
            exclude=list(det.get("exclude_classes", [])),
            face_detection=ai.get("face_detection") in ("vision", True),
            frequency_mode=det.get("frequency_mode", "auto"),
            fixed_fps=float(det.get("fixed_fps", 15)),
            segmentation_model=ai.get("segmentation_model") or "auto",
            need_masks=any(r.get("enabled", True) and (r.get("replacement") or {}).get("enabled")
                           and (r.get("replacement") or {}).get("mode") == "object" and r.get("mode") != "manual"
                           for r in body.get("rois", [])),
        )


def filter_detections(dets: list[Detection], cfg: DetectionConfig, min_conf: float | None = None) -> list[Detection]:
    inc = set(cfg.include)
    exc = set(cfg.exclude)
    thr = cfg.confidence if min_conf is None else min_conf
    out = []
    for d in dets:
        if d.confidence < thr:
            continue
        if inc and d.label not in inc:
            continue
        if d.label in exc:
            continue
        out.append(d)
    return out
