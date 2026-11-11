"""Class taxonomy used for filtering in the UI. COCO-80 names (Ultralytics) + Vision 'face'."""
from __future__ import annotations

FACE = "face"

GROUPS: dict[str, list[str]] = {
    "People": ["person", FACE],
    "Vehicles": ["bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat"],
    "Animals": ["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"],
    "Fruit & food": ["banana", "apple", "orange", "broccoli", "carrot", "sandwich", "hot dog", "pizza",
                     "donut", "cake"],
    "Furniture": ["chair", "couch", "potted plant", "bed", "dining table", "toilet", "bench"],
    "Electronics": ["tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave", "oven",
                    "toaster", "refrigerator"],
    "Kitchen": ["bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "sink"],
    "Street": ["traffic light", "fire hydrant", "stop sign", "parking meter"],
    "Accessories": ["backpack", "umbrella", "handbag", "tie", "suitcase"],
    "Sports": ["frisbee", "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
               "skateboard", "surfboard", "tennis racket"],
    "Household": ["book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush"],
}


def group_of(label: str) -> str:
    for g, names in GROUPS.items():
        if label in names:
            return g
    return "Other"
