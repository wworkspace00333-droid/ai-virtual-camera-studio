# coco16: accuracy regression subset

16 images (53 of the 80 COCO classes) with YOLO-format labels, chosen from Ultralytics COCO128
(https://github.com/ultralytics/assets/releases/download/v0.0.0/coco128.zip), which is the first 128
images of COCO train2017.

- Images: COCO dataset (https://cocodataset.org), Flickr images under their individual licenses;
  COCO annotations are CC BY 4.0.
- YOLO-format labels: Ultralytics COCO128, distributed under GPL-3.0.

Used by `tests/test_accuracy.py` and the AI Engine page's accuracy check, so a broken preprocessing
step (letterbox, colour order, coordinate mapping) shows up as a drop in mAP.
