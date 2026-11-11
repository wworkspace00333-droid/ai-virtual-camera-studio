"""Generate small test videos whose frame index is encoded as a 10-bit black/white bar code.

    .venv/bin/python scripts/make_test_clips.py      (needs ffmpeg on PATH)

tests/data/clip_h264.mp4      320x180, 30 fps, 60 frames, H.264 (hardware-decodable)
tests/data/clip_alpha.mov     320x180, 30 fps, 30 frames, ProRes 4444 with alpha (left half transparent)
tests/data/clip_h264.mkv      same as the mp4 in Matroska (AVFoundation cannot open it -> FFmpeg fallback)
"""
import shutil
import subprocess
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "tests" / "data"
W, H, FPS = 320, 180, 30
BITS, CELL = 10, 32


def frame(i: int, alpha: bool = False) -> np.ndarray:
    hue = (i * 37) % 255
    img = np.zeros((H, W, 4), np.uint8)
    img[..., 0], img[..., 1], img[..., 2], img[..., 3] = hue, 128, 255 - hue, 255
    for b in range(BITS):                             # bar code in the bottom rows
        v = 255 if (i >> b) & 1 else 0
        img[H - CELL:, b * CELL:(b + 1) * CELL, :3] = v
    if alpha:
        img[: H - CELL, : W // 2, 3] = 0                # transparent left half (above the bar code)
    return img


def decode_index(bgra: np.ndarray) -> int:
    h, w = bgra.shape[:2]
    cell = w / BITS
    row = bgra[int(h - h * CELL / H / 2), :, :3].astype(int).mean(axis=1)
    return sum(1 << b for b in range(BITS) if row[int((b + 0.5) * cell)] > 127)


def encode(path: Path, n: int, args: list[str], alpha=False) -> None:
    ff = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    p = subprocess.Popen([ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}",
                          "-r", str(FPS), "-i", "-", *args, str(path)], stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(frame(i, alpha).tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit(f"ffmpeg failed for {path}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    encode(OUT / "clip_h264.mp4", 60, ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12", "-g", "15", "-movflags", "+faststart"])
    encode(OUT / "clip_alpha.mov", 30, ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"], alpha=True)
    encode(OUT / "clip_h264.mkv", 60, ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12", "-g", "15"])
    for f in ("clip_h264.mp4", "clip_alpha.mov", "clip_h264.mkv"):
        print(f, (OUT / f).stat().st_size, "bytes")


if __name__ == "__main__":
    main()
