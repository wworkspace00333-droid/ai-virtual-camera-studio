"""Video assets: probing, decoding (AVFoundation/VideoToolbox, FFmpeg fallback) and playback timing.

Decoding
  AVAssetReader decodes through VideoToolbox (hardware on Apple Silicon for H.264, HEVC, ProRes)
  straight into BGRA at the output size we ask for (downscaled if the source is larger than needed).
  Containers/codecs AVFoundation cannot read (e.g. MKV, WebM) fall back to an `ffmpeg` subprocess
  (software decode) when ffmpeg is installed; the UI labels which decoder is in use.

Playback (VideoPlayer)
  A decode thread keeps a small look-ahead buffer (bounded, newest wins). The compositor asks for
  the frame at an *output* time t (host clock); media time = offset + (t - start - paused) * rate.
  Frame-rate conversion: nearest-lower frame (drop/duplicate) or linear blend of the two
  neighbouring frames. Looping restarts the reader; the buffer bridges the restart. frame_at()
  never blocks the output: if the decoder is behind, the most recent decoded frame is used.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

LOOKAHEAD_S = 0.25
MAX_BUFFER = 8          # ~29 MB per player at 1280x720 BGRA
RESEEK_GAP_S = 0.6


class VideoError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    codec: str
    has_alpha: bool
    decoder: str          # "avfoundation" | "ffmpeg"
    hardware: bool        # VideoToolbox reports hardware decode support for the codec
    rotation: int = 0     # degrees from the track's preferred transform

    def to_dict(self) -> dict:
        return asdict(self)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                                           if Path(p).exists()), None)


def _fourcc(v: int) -> str:
    try:
        return int(v).to_bytes(4, "big").decode("ascii").strip()
    except (UnicodeDecodeError, OverflowError):
        return str(v)


def _av_asset(path: Path):
    import AVFoundation as AV  # type: ignore
    import Foundation  # type: ignore
    asset = AV.AVURLAsset.URLAssetWithURL_options_(Foundation.NSURL.fileURLWithPath_(str(path)), None)
    tracks = asset.tracksWithMediaType_(AV.AVMediaTypeVideo) if asset.isReadable() else []
    return asset, (tracks[0] if tracks else None)


def probe(path: Path) -> VideoInfo:
    path = Path(path)
    if not path.exists():
        raise VideoError(f"File not found: {path.name}")
    import CoreMedia as CM  # type: ignore
    asset, track = _av_asset(path)
    if track is not None:
        fd = track.formatDescriptions()[0]
        codec_code = CM.CMFormatDescriptionGetMediaSubType(fd)
        alpha = bool(CM.CMFormatDescriptionGetExtension(fd, CM.kCMFormatDescriptionExtension_ContainsAlphaChannel))
        size = track.naturalSize()
        t = track.preferredTransform()
        rot = int(round(math.degrees(math.atan2(t.b, t.a)))) % 360
        try:
            import VideoToolbox as VT  # type: ignore
            hw = bool(VT.VTIsHardwareDecodeSupported(codec_code))
        except Exception:
            hw = False
        w, h = int(round(size.width)), int(round(size.height))
        return VideoInfo(w, h, float(track.nominalFrameRate() or 30.0), float(CM.CMTimeGetSeconds(asset.duration())),
                         _fourcc(codec_code), alpha, "avfoundation", hw, rot)
    ff = ffmpeg_path()
    if ff is None:
        raise VideoError(f"{path.name}: AVFoundation cannot read this file and FFmpeg is not installed")
    fp = str(Path(ff).with_name("ffprobe"))
    try:
        out = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height,avg_frame_rate,codec_name,pix_fmt:format=duration", "-of", "json",
                              str(path)], capture_output=True, text=True, timeout=15, check=True).stdout
        j = json.loads(out)
        s = j["streams"][0]
        num, den = (int(x) for x in s.get("avg_frame_rate", "30/1").split("/"))
        fps = num / den if den else 30.0
        alpha = "a" in s.get("pix_fmt", "").replace("yuva", "a").split("p")[0] or s.get("pix_fmt", "").startswith(("yuva", "rgba", "bgra", "argb"))
        return VideoInfo(int(s["width"]), int(s["height"]), fps, float(j["format"].get("duration", 0) or 0),
                         s.get("codec_name", "?"), bool(alpha), "ffmpeg", False)
    except (subprocess.SubprocessError, KeyError, IndexError, ValueError, OSError) as e:
        raise VideoError(f"{path.name}: cannot probe video ({e})")


def thumbnail(path: Path, max_side: int = 160) -> np.ndarray | None:
    """Premultiplied BGRA thumbnail of the first frame (None if it cannot be decoded)."""
    try:
        info = probe(path)
        dec = open_decoder(path, info, 0.0, max_side)
        for _, img in dec:
            dec.close()
            return img
    except Exception as e:
        log.debug("thumbnail failed for %s: %s", path, e)
    return None


def _target_size(info: VideoInfo, max_side: int | None) -> tuple[int, int]:
    w, h = (info.height, info.width) if info.rotation in (90, 270) else (info.width, info.height)
    if max_side and max(w, h) > max_side:
        s = max_side / max(w, h)
        w, h = max(2, int(round(w * s)) // 2 * 2), max(2, int(round(h * s)) // 2 * 2)
    return w, h


def _premultiply(a: np.ndarray) -> np.ndarray:
    alpha = a[..., 3:4].astype(np.uint16)
    out = a.copy()
    out[..., :3] = ((a[..., :3].astype(np.uint16) * alpha + 127) // 255).astype(np.uint8)
    return out


class AVFDecoder:
    """Iterates (pts_seconds, premultiplied BGRA ndarray) from start_s using AVAssetReader."""

    def __init__(self, path: Path, info: VideoInfo, start_s: float, max_side: int | None):
        import AVFoundation as AV  # type: ignore
        import CoreMedia as CM  # type: ignore
        import Quartz as Q  # type: ignore
        asset, track = _av_asset(path)
        if track is None:
            raise VideoError("no readable video track")
        reader, err = AV.AVAssetReader.assetReaderWithAsset_error_(asset, None)
        if reader is None:
            raise VideoError(f"AVAssetReader: {err}")
        tw, th = _target_size(info, max_side)
        sw, sh = (th, tw) if info.rotation in (90, 270) else (tw, th)   # decode size before rotation
        settings = {Q.kCVPixelBufferPixelFormatTypeKey: Q.kCVPixelFormatType_32BGRA}
        if (sw, sh) != (info.width, info.height):
            settings[Q.kCVPixelBufferWidthKey] = sw
            settings[Q.kCVPixelBufferHeightKey] = sh
        out = AV.AVAssetReaderTrackOutput.assetReaderTrackOutputWithTrack_outputSettings_(track, settings)
        out.setAlwaysCopiesSampleData_(False)
        if start_s > 0:
            reader.setTimeRange_(CM.CMTimeRangeMake(CM.CMTimeMakeWithSeconds(start_s, 60000), CM.kCMTimePositiveInfinity))
        if not reader.canAddOutput_(out):
            raise VideoError("cannot add reader output")
        reader.addOutput_(out)
        if not reader.startReading():
            raise VideoError(f"startReading failed: {reader.error()}")
        self._reader, self._out, self.info = reader, out, info

    def __iter__(self):
        import CoreMedia as CM  # type: ignore
        import Quartz as Q  # type: ignore
        rot = self.info.rotation
        while True:
            sb = self._out.copyNextSampleBuffer()
            if sb is None:
                if self._reader.status() == 3:     # AVAssetReaderStatusFailed
                    raise VideoError(f"decode failed: {self._reader.error()}")
                return
            pb = CM.CMSampleBufferGetImageBuffer(sb)
            if pb is None:
                continue
            pts = float(CM.CMTimeGetSeconds(CM.CMSampleBufferGetPresentationTimeStamp(sb)))
            Q.CVPixelBufferLockBaseAddress(pb, Q.kCVPixelBufferLock_ReadOnly)
            try:
                bpr, h, w = Q.CVPixelBufferGetBytesPerRow(pb), Q.CVPixelBufferGetHeight(pb), Q.CVPixelBufferGetWidth(pb)
                buf = Q.CVPixelBufferGetBaseAddress(pb).as_buffer(bpr * h)
                img = np.frombuffer(buf, np.uint8).reshape(h, bpr)[:, : w * 4].reshape(h, w, 4).copy()
            finally:
                Q.CVPixelBufferUnlockBaseAddress(pb, Q.kCVPixelBufferLock_ReadOnly)
            if rot:
                img = np.ascontiguousarray(np.rot90(img, k=-(rot // 90)))
            if self.info.has_alpha:
                img = _premultiply(img)
            yield pts, img

    def close(self) -> None:
        try:
            self._reader.cancelReading()
        except Exception:
            pass


class FFmpegDecoder:
    """Software fallback: ffmpeg subprocess emitting raw BGRA."""

    def __init__(self, path: Path, info: VideoInfo, start_s: float, max_side: int | None):
        ff = ffmpeg_path()
        if ff is None:
            raise VideoError("FFmpeg is not installed")
        self.w, self.h = _target_size(info, max_side)
        self.info, self.start = info, start_s
        self._p = subprocess.Popen([ff, "-v", "error", "-ss", f"{start_s:.4f}", "-i", str(path), "-an",
                                    "-vf", f"scale={self.w}:{self.h}", "-f", "rawvideo", "-pix_fmt", "bgra", "-"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def __iter__(self):
        n = 0
        size = self.w * self.h * 4
        while True:
            data = self._p.stdout.read(size)
            if len(data) < size:
                return
            img = np.frombuffer(data, np.uint8).reshape(self.h, self.w, 4).copy()
            yield self.start + n / max(self.info.fps, 1e-3), (_premultiply(img) if self.info.has_alpha else img)
            n += 1

    def close(self) -> None:
        try:
            self._p.kill()
            self._p.wait(timeout=2)
        except Exception:
            pass


def open_decoder(path: Path, info: VideoInfo, start_s: float, max_side: int | None):
    return (AVFDecoder if info.decoder == "avfoundation" else FFmpegDecoder)(path, info, start_s, max_side)


@dataclass
class PlaybackSettings:
    loop: bool = True
    start_offset_s: float = 0.0
    rate: float = 1.0
    end_action: str = "hold"         # when not looping: hold (last frame) | hide
    interpolation: str = "nearest"   # nearest | blend
    max_side: int = 1280


@dataclass
class PlayerStats:
    position_s: float = 0.0
    duration_s: float = 0.0
    buffered: int = 0
    decoded: int = 0
    late_frames: int = 0      # times the output needed a frame the decoder had not produced yet
    reseeks: int = 0
    loops: int = 0
    decoder: str = ""
    paused: bool = False
    ended: bool = False
    error: str = ""


class VideoPlayer:
    def __init__(self, path: Path, info: VideoInfo, settings: PlaybackSettings):
        from vcstudio.core.pyobjc_prewarm import prewarm
        prewarm()
        self.path, self.info, self.cfg = Path(path), info, settings
        self.stats = PlayerStats(duration_s=info.duration, decoder=f"{info.decoder}{' (hardware)' if info.hardware else ''}")
        self._buf: collections.deque[tuple[float, np.ndarray]] = collections.deque()
        self._cond = threading.Condition()
        self._start: float | None = None   # host time where unwrapped media time == start_offset
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._want = settings.start_offset_s   # unwrapped media time the output currently needs
        self._stop = threading.Event()
        self._restart = threading.Event()
        self.last_used = time.monotonic()
        self._thread = threading.Thread(target=self._run, name=f"video-{self.path.stem}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- timeline
    def _media_time(self, t: float) -> float:
        if self._start is None:
            self._start = t
        if self._paused_at is not None:
            t = self._paused_at
        return self.cfg.start_offset_s + max(0.0, t - self._start - self._paused_total) * self.cfg.rate

    def pause(self, t: float) -> None:
        if self._paused_at is None:
            self._paused_at = t
            self.stats.paused = True

    def resume(self, t: float) -> None:
        if self._paused_at is not None:
            self._paused_total += t - self._paused_at
            self._paused_at = None
            self.stats.paused = False

    def toggle(self, t: float) -> None:
        self.resume(t) if self._paused_at is not None else self.pause(t)

    def restart(self, t: float) -> None:
        with self._cond:
            self._start, self._paused_total = t, 0.0
            if self._paused_at is not None:
                self._paused_at = t
            self._want = self.cfg.start_offset_s
            self.stats.ended = False
            self._buf.clear()
            self._restart.set()
            self._cond.notify_all()

    # ------------------------------------------------------------- output side (never blocks)
    def frame_at(self, t: float) -> np.ndarray | None:
        self.last_used = time.monotonic()
        dur = max(self.info.duration, 1e-3)
        u = self._media_time(t)
        if not self.cfg.loop and u >= dur:
            self.stats.ended = True
            u = dur - 1e-6
            if self.cfg.end_action == "hide":
                return None
        with self._cond:
            self._want = u
            self._cond.notify_all()
            buf = list(self._buf)
        self.stats.position_s = u % dur if self.cfg.loop else u
        if not buf:
            return None
        before = [f for f in buf if f[0] <= u + 1e-6]
        if not before:
            return buf[0][1]           # startup / just after reseek
        p0, f0 = before[-1]
        if before[-1] is buf[-1] and u - p0 > 1.5 / max(self.info.fps, 1):
            self.stats.late_frames += 1
        if self.cfg.interpolation == "blend":
            after = [f for f in buf if f[0] > u]
            if after:
                p1, f1 = after[0]
                w = (u - p0) / max(p1 - p0, 1e-6)
                if 0.02 < w < 0.98 and f0.shape == f1.shape:
                    return (f0.astype(np.float32) * (1 - w) + f1.astype(np.float32) * w + 0.5).astype(np.uint8)
        return f0

    # ------------------------------------------------------------- decode thread
    def _run(self) -> None:
        dur = max(self.info.duration, 1e-3)
        loop_base = 0.0                          # unwrapped time of the current pass's media 0
        dec = None
        it = None
        try:
            while not self._stop.is_set():
                with self._cond:
                    want = self._want
                    last = self._buf[-1][0] if self._buf else None
                    first = self._buf[0][0] if self._buf else None
                restart = self._restart.is_set()
                behind = last is not None and want - last > RESEEK_GAP_S
                stale = first is not None and want < first - 1e-3
                if dec is None or restart or behind or stale:
                    if dec is not None:
                        dec.close()
                        self.stats.reseeks += 1
                    self._restart.clear()
                    with self._cond:
                        self._buf.clear()
                    target = want if not self.cfg.loop else want
                    passes = math.floor(target / dur) if self.cfg.loop else 0
                    loop_base = passes * dur
                    media_start = min(max(0.0, target - loop_base), max(0.0, dur - 1e-3))
                    dec = open_decoder(self.path, self.info, media_start, self.cfg.max_side)
                    it = iter(dec)
                    continue
                # enough buffered ahead? wait for the output to advance
                if last is not None and (last >= want + LOOKAHEAD_S or len(self._buf) >= MAX_BUFFER):
                    with self._cond:
                        self._cond.wait(0.02)
                        while self._buf and len(self._buf) > 2 and self._buf[1][0] <= self._want:
                            self._buf.popleft()          # drop frames the output has passed
                    continue
                try:
                    import objc  # type: ignore
                    with objc.autorelease_pool():      # sample buffers from AVAssetReader are autoreleased
                        pts, img = next(it)
                except StopIteration:
                    dec.close()
                    dec = None
                    if self.cfg.loop:
                        loop_base += dur
                        self.stats.loops += 1
                        dec = open_decoder(self.path, self.info, 0.0, self.cfg.max_side)
                        it = iter(dec)
                    else:
                        with self._cond:
                            self._cond.wait(0.05)
                        if not self._restart.is_set():
                            while not self._stop.is_set() and not self._restart.is_set():
                                self._stop.wait(0.05)
                    continue
                with self._cond:
                    self._buf.append((loop_base + pts, img))
                    while len(self._buf) > MAX_BUFFER:
                        self._buf.popleft()
                    self.stats.buffered = len(self._buf)
                self.stats.decoded += 1
        except Exception as e:
            self.stats.error = str(e)
            log.error("Video decode failed for %s: %s", self.path.name, e)
        finally:
            if dec is not None:
                dec.close()

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=2)
        self._buf.clear()

    @property
    def failed(self) -> bool:
        return bool(self.stats.error)


class PlayerPool:
    """Lazily created players keyed by sync scope; idle players are closed (bounded memory)."""
    MAX_PLAYERS = 6
    IDLE_S = 3.0

    def __init__(self):
        self._players: dict[tuple, VideoPlayer] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple, path: Path, info: VideoInfo, settings: PlaybackSettings) -> VideoPlayer:
        with self._lock:
            p = self._players.get(key)
            if p is not None and (p.cfg != settings or p.path != Path(path)):
                p.close()
                p = None
            if p is None:
                if len(self._players) >= self.MAX_PLAYERS:
                    oldest = min(self._players, key=lambda k: self._players[k].last_used)
                    self._players.pop(oldest).close()
                p = self._players[key] = VideoPlayer(path, info, settings)
            return p

    def players(self) -> dict[tuple, VideoPlayer]:
        with self._lock:
            return dict(self._players)

    def sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, p in self._players.items() if now - p.last_used > self.IDLE_S]:
                self._players.pop(k).close()

    def close_all(self) -> None:
        with self._lock:
            for p in self._players.values():
                p.close()
            self._players.clear()
