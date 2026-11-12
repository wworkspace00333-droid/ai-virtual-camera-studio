"""A stand-in "game" window for testing Card for a game (developer tool).

Shows a camera the way many games do (mirrored, cropped to a square, darkened around a card frame with
a caption) and stays on top. Run through the dev bundle, which owns camera permission:

    open -n "build/AI Virtual Camera Studio Dev.app" --args --run-script fake_game.py \
        [--device "OBS Virtual Camera"] [--seconds 120] [--no-mirror] [--frames 1|2] [--save <png>]

--save writes the window's last picture when it closes (to check where the card landed).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    import cv2
    import numpy as np
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QApplication, QLabel

    from vcstudio.core.pyobjc_prewarm import prewarm
    from vcstudio.core.video_capture import AVFoundationSource

    a = sys.argv[1:]
    opt = lambda k, d: a[a.index(k) + 1] if k in a else d
    device, seconds = opt("--device", "OBS Virtual Camera"), float(opt("--seconds", "120"))
    mirror, save = "--no-mirror" not in a, opt("--save", None)
    nframes = int(opt("--frames", "1"))
    prewarm()
    app = QApplication(sys.argv[:1])
    latest = {"img": None}
    lock = threading.Lock()

    def on_frame(f):
        img = f.downscaled_copy(1280)
        with lock:
            latest["img"] = img
    src = AVFoundationSource(None, device, 1920, 1080, 30.0, allow_own_output=True)
    src.start(on_frame, lambda *e: print("camera event", e, flush=True))

    view = QLabel()
    view.setWindowTitle("Fake card game")
    view.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    S = 540
    view.setFixedSize(S, S)
    view.move(120, 120)
    view.show()
    shown = {"img": None}

    def tick():
        with lock:
            img = latest["img"]
        if img is None:
            return
        h, w = img.shape[:2]
        sq = img[:, (w - h) // 2:(w - h) // 2 + h, :3]          # square crop of the centre
        if mirror:
            sq = cv2.flip(sq, 1)
        v = cv2.resize(sq, (S, S), interpolation=cv2.INTER_AREA)
        boxes = ([(0.3, 0.22, 0.7, 0.82)] if nframes == 1 else [(0.08, 0.3, 0.44, 0.8), (0.56, 0.3, 0.92, 0.8)])
        boxes = [tuple(int(S * c) for c in b) for b in boxes]
        keep = [v[y0:y1, x0:x1].copy() for x0, y0, x1, y1 in boxes]
        v = (v * 0.45).astype(np.uint8)
        for (x0, y0, x1, y1), k in zip(boxes, keep):
            v[y0:y1, x0:x1] = k
            cv2.rectangle(v, (x0, y0), (x1, y1), (255, 255, 255), 3)
        cv2.putText(v, "Show your card" + ("s" if nframes > 1 else ""), (boxes[0][0], boxes[0][1] - 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        shown["img"] = v
        rgb = np.ascontiguousarray(cv2.cvtColor(v, cv2.COLOR_BGR2RGB))
        view.setPixmap(QPixmap.fromImage(QImage(rgb.data, S, S, rgb.strides[0], QImage.Format_RGB888).copy()))

    t = QTimer(); t.timeout.connect(tick); t.start(33)
    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()
    src.stop()
    if save and shown["img"] is not None:
        cv2.imwrite(save, shown["img"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
