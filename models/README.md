# Models

The runtime app loads Core ML packages (`<name>.mlpackage` plus `<name>.json` metadata) from this folder and from `~/Library/Application Support/AIVirtualCameraStudio/models/`. They are not committed to git. Generate them with the separate conversion environment, so PyTorch never becomes a runtime dependency:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv-convert
.venv-convert/bin/pip install "ultralytics>=8.3" coremltools
.venv-convert/bin/python scripts/convert_models.py            # yolo11n, yolo11s (NMS pipeline) + yolo11n-seg (raw)
.venv-convert/bin/python scripts/convert_models.py yolo11m yolo11m-seg    # Maximum Quality models
```

| Model | Size | Use |
|---|---|---|
| yolo11n | 5.2 MB | Low Resource mode ("Auto" there always takes the smallest model) |
| yolo11s | 18.2 MB | "Auto" in Performance / Balanced when the larger model doesn't fit the budget (the M2 Pro case) |
| yolo11m | 39 MB | "Auto" first choice in Performance (p95 ≤ 20 ms), Balanced and Maximum Quality (p95 ≤ 25 ms) |
| yolo11m-seg | 43 MB | Maximum Quality with object replacement |
| yolo11n-seg | ~6 MB | Instance segmentation. Loaded instead of the detector only while an ROI uses object (silhouette) replacement |

**Detection:** 80 COCO classes, including person, car, truck, bus, bicycle, motorcycle, apple, banana, orange, animals, furniture and electronics. Faces come from Apple Vision (`VNDetectFaceRectanglesRequest`), which finds face rectangles only; there is no identity recognition.

**License:** Ultralytics YOLO code and weights are **AGPL-3.0**. Distributing the app with these weights requires AGPL compliance or an Ultralytics Enterprise license. Any detector exported as a Core ML pipeline with `confidence`/`coordinates` outputs (Vision `VNRecognizedObjectObservation`) can replace them.
