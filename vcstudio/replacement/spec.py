"""Per-ROI replacement settings (stored in roi.replacement)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

MODES = {"rect": "Region replacement", "object": "Object replacement (segmentation mask)", "overlay": "Overlay",
         "effect": "Effect (blur / pixelate / colour)"}
EFFECTS = {"blur": "Gaussian blur", "pixelate": "Pixelate", "color": "Colour correction"}
VIDEO_SYNC = {"shared": "Shared (all regions in sync)", "per_roi": "Per ROI",
              "per_object": "Per object (restarts for each new object)"}
FITS = {"fit": "Fit (contain)", "fill": "Fill (cover)", "stretch": "Stretch", "original": "Original size"}


@dataclass
class ReplacementSpec:
    enabled: bool = False
    mode: str = "rect"                 # rect | object | overlay
    asset_id: str = ""
    fit: str = "fill"
    scale: float = 1.0
    offset_x: float = 0.0              # fraction of region width
    offset_y: float = 0.0              # fraction of region height
    rotation: float = 0.0              # degrees, added to the region's rotation
    flip_h: bool = False               # mirror the asset (for apps that show the camera mirrored)
    opacity: float = 1.0
    crop: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])   # normalised asset crop
    feather_px: float = 0.0
    background: list[int] = field(default_factory=lambda: [0, 0, 0, 255])   # BGRA fill behind the asset
    mask_threshold: float = 0.5
    refine_edges: bool = True
    fallback_no_mask: str = "skip"     # skip | box   (object mode without a mask)
    # effects (mode == "effect"); object regions use the silhouette when a mask exists, else the box
    effect: str = "blur"
    blur_sigma: float = 12.0
    pixel_size: int = 24
    brightness: float = 0.0            # -1..1 (added)
    contrast: float = 1.0              # 0..3
    saturation: float = 1.0            # 0..3
    # video assets
    video_loop: bool = True
    video_start_offset_s: float = 0.0
    video_rate: float = 1.0
    video_end_action: str = "hold"     # hold | hide (when not looping)
    video_interpolation: str = "nearest"   # nearest | blend
    video_sync: str = "shared"         # shared (one timeline per asset) | per_roi | per_object
    video_max_side: int = 1280

    @classmethod
    def from_dict(cls, d: dict | None) -> "ReplacementSpec":
        d = d or {}
        s = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if s.mode not in MODES:
            s.mode = "rect"
        if s.fit not in FITS:
            s.fit = "fill"
        s.opacity = min(max(float(s.opacity), 0.0), 1.0)
        s.scale = max(0.01, float(s.scale))
        c = [float(v) for v in (s.crop or [0, 0, 1, 1])][:4]
        x0, y0, x1, y1 = (min(max(v, 0.0), 1.0) for v in c)
        if x1 - x0 < 0.01 or y1 - y0 < 0.01:
            x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
        s.crop = [x0, y0, x1, y1]
        bg = list(s.background or [0, 0, 0, 255])[:4] + [255] * (4 - len(s.background or []))
        s.background = [int(min(max(v, 0), 255)) for v in bg[:4]]
        if s.effect not in EFFECTS:
            s.effect = "blur"
        s.blur_sigma = min(max(float(s.blur_sigma), 0.5), 100.0)
        s.pixel_size = int(min(max(int(s.pixel_size), 2), 256))
        s.brightness = min(max(float(s.brightness), -1.0), 1.0)
        s.contrast = min(max(float(s.contrast), 0.0), 3.0)
        s.saturation = min(max(float(s.saturation), 0.0), 3.0)
        s.video_rate = min(max(float(s.video_rate), 0.1), 8.0)
        s.video_start_offset_s = max(0.0, float(s.video_start_offset_s))
        s.video_max_side = int(min(max(int(s.video_max_side), 160), 3840))
        if s.video_sync not in VIDEO_SYNC:
            s.video_sync = "shared"
        if s.video_end_action not in ("hold", "hide"):
            s.video_end_action = "hold"
        if s.video_interpolation not in ("nearest", "blend"):
            s.video_interpolation = "nearest"
        return s

    def playback(self):
        from vcstudio.replacement.video import PlaybackSettings
        return PlaybackSettings(self.video_loop, self.video_start_offset_s, self.video_rate, self.video_end_action,
                                self.video_interpolation, self.video_max_side)

    def to_dict(self) -> dict:
        return asdict(self)
