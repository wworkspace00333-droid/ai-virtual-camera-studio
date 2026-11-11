"""Render the application icon (packaging/AppIcon.icns) with OpenCV + iconutil. No external art."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np


def render(n: int) -> np.ndarray:
    s = 1024
    img = np.zeros((s, s, 4), np.uint8)
    # rounded-square dark plate with a subtle vertical gradient
    plate = np.zeros((s, s), np.uint8)
    r, m = 200, 80
    cv2.rectangle(plate, (m + r, m), (s - m - r, s - m), 255, -1)
    cv2.rectangle(plate, (m, m + r), (s - m, s - m - r), 255, -1)
    for cx, cy in ((m + r, m + r), (s - m - r, m + r), (m + r, s - m - r), (s - m - r, s - m - r)):
        cv2.circle(plate, (cx, cy), r, 255, -1, cv2.LINE_AA)
    grad = np.linspace(46, 22, s, dtype=np.float32)[:, None]
    img[..., 0] = (grad * 1.25).astype(np.uint8)
    img[..., 1] = grad.astype(np.uint8)
    img[..., 2] = (grad * 0.9).astype(np.uint8)
    img[..., 3] = plate
    accent = (255, 140, 60, 255)      # BGRA: blue-ish accent matching the UI theme
    # camera body + lens
    cv2.rectangle(img, (250, 380), (680, 700), accent, 34, cv2.LINE_AA)
    pts = np.array([[680, 480], [800, 400], [800, 680], [680, 600]], np.int32)
    cv2.polylines(img, [pts], True, accent, 34, cv2.LINE_AA)
    # detection box corners around a "subject"
    c = (90, 220, 120, 255)
    for (x, y, dx, dy) in ((330, 450, 1, 1), (600, 450, -1, 1), (330, 630, 1, -1), (600, 630, -1, -1)):
        cv2.line(img, (x, y), (x + 60 * dx, y), c, 22, cv2.LINE_AA)
        cv2.line(img, (x, y), (x, y + 60 * dy), c, 22, cv2.LINE_AA)
    cv2.circle(img, (465, 540), 48, c, -1, cv2.LINE_AA)
    img[..., 3] = np.minimum(img[..., 3], plate)
    return cv2.resize(img, (n, n), interpolation=cv2.INTER_AREA)


def main(out: Path) -> None:
    tmp = Path(tempfile.mkdtemp())
    iconset = tmp / "AppIcon.iconset"
    iconset.mkdir()
    for base in (16, 32, 128, 256, 512):
        cv2.imwrite(str(iconset / f"icon_{base}x{base}.png"), render(base))
        cv2.imwrite(str(iconset / f"icon_{base}x{base}@2x.png"), render(base * 2))
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    shutil.rmtree(tmp)
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("AppIcon.icns"))
