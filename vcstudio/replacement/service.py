"""Holds the active profile's replacement specs + assets for the compositor (thread-safe)."""
from __future__ import annotations

import threading

from vcstudio.replacement.assets import Asset, AssetLibrary, assets_from_profile
from vcstudio.replacement.compositor import Compositor
from vcstudio.replacement.spec import ReplacementSpec


class ReplacementService:
    def __init__(self, library: AssetLibrary, backend: str = "auto"):
        self.library = library
        self.compositor = Compositor(library, backend)
        self._lock = threading.Lock()
        self._specs: dict[str, ReplacementSpec] = {}
        self._assets: dict[str, Asset] = {}

    def load_profile(self, body: dict) -> None:
        specs = {r["id"]: ReplacementSpec.from_dict(r.get("replacement")) for r in body.get("rois", []) if "id" in r}
        assets = assets_from_profile(body)
        with self._lock:
            self._specs, self._assets = specs, assets
        self.compositor.reset_failures()

    def state(self) -> tuple[dict[str, ReplacementSpec], dict[str, Asset]]:
        with self._lock:
            return dict(self._specs), dict(self._assets)

    def active(self) -> bool:
        specs, assets = self.state()
        # effects (blur / pixelate / colour) need no asset
        return any(s.enabled and (s.mode == "effect" or s.asset_id in assets) for s in specs.values())

    def render(self, frame, regions, t: float | None = None):
        specs, assets = self.state()
        return self.compositor.render(frame, regions, specs, assets, t)

    def render_pb(self, frame, regions, t: float | None = None):
        specs, assets = self.state()
        return self.compositor.render_pb(frame, regions, specs, assets, t)

    def players_for_roi(self, roi_id: str):
        """Video players currently serving an ROI (for UI transport controls)."""
        specs, assets = self.state()
        spec = specs.get(roi_id)
        if spec is None:
            return []
        out = []
        for key, p in self.compositor.videos.players().items():
            # key = (asset_id, sync, *scope); scope is () for shared, (roi,) or (roi, track) otherwise
            if key[0] != spec.asset_id or key[1] != spec.video_sync:
                continue
            if spec.video_sync == "shared" or key[2] == roi_id:
                out.append(p)
        return out

    def close(self) -> None:
        self.compositor.close()
