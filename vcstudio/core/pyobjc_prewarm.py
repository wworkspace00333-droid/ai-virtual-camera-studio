"""Resolve every PyObjC symbol the app uses once, on the main thread, before worker threads start.

PyObjC framework wrappers resolve functions and constants lazily on first attribute access, and that
lazy resolution is not safe when several threads touch the same symbol at the same moment (found by the
6-video-player stress test: one decoder thread failed with AttributeError 'CMTimeMakeWithSeconds').
tests/test_pyobjc_prewarm.py fails if code starts using a symbol that is missing here.
"""
from __future__ import annotations

import importlib
import logging
import threading

log = logging.getLogger(__name__)

SYMBOLS: dict[str, tuple[str, ...]] = {
    "AVFoundation": (
        "AVAssetReader",
        "AVAssetReaderTrackOutput",
        "AVCaptureDevice",
        "AVCaptureDeviceDiscoverySession",
        "AVCaptureDeviceInput",
        "AVCaptureDevicePositionUnspecified",
        "AVCaptureDeviceTypeBuiltInWideAngleCamera",
        "AVCaptureDeviceTypeExternal",
        "AVCaptureDeviceWasDisconnectedNotification",
        "AVCaptureSession",
        "AVCaptureSessionErrorKey",
        "AVCaptureSessionRuntimeErrorNotification",
        "AVCaptureSessionWasInterruptedNotification",
        "AVCaptureVideoDataOutput",
        "AVMediaTypeVideo",
        "AVURLAsset",
    ),
    "CoreMedia": (
        "CMClockGetHostTimeClock",
        "CMClockGetTime",
        "CMFormatDescriptionGetExtension",
        "CMFormatDescriptionGetMediaSubType",
        "CMSampleBufferGetImageBuffer",
        "CMSampleBufferGetPresentationTimeStamp",
        "CMTimeGetSeconds",
        "CMTimeMake",
        "CMTimeMakeWithSeconds",
        "CMTimeRangeMake",
        "CMVideoFormatDescriptionGetDimensions",
        "kCMFormatDescriptionExtension_ContainsAlphaChannel",
        "kCMTimePositiveInfinity",
    ),
    "CoreML": (
        "MLComputeUnitsAll",
        "MLComputeUnitsCPUAndGPU",
        "MLComputeUnitsCPUAndNeuralEngine",
        "MLComputeUnitsCPUOnly",
        "MLDictionaryFeatureProvider",
        "MLFeatureValue",
        "MLModel",
        "MLModelConfiguration",
        "MLMultiArrayDataTypeFloat32",
    ),
    "Foundation": (
        "NSURL",
    ),
    "Quartz": (
        "CGBitmapContextCreate",
        "CGColorSpaceCreateDeviceRGB",
        "CGColorSpaceCreateWithName",
        "CGContextDrawImage",
        "CGContextSetInterpolationQuality",
        "CGImageGetHeight",
        "CGImageGetWidth",
        "CGImageSourceCreateImageAtIndex",
        "CGImageSourceCreateWithURL",
        "CGImageSourceGetCount",
        "CGRectMake",
        "CVPixelBufferGetBaseAddress",
        "CVPixelBufferGetBytesPerRow",
        "CVPixelBufferGetHeight",
        "CVPixelBufferGetPixelFormatType",
        "CVPixelBufferGetWidth",
        "CVPixelBufferLockBaseAddress",
        "CVPixelBufferPoolCreate",
        "CVPixelBufferPoolCreatePixelBuffer",
        "CVPixelBufferUnlockBaseAddress",
        "kCGBitmapByteOrder32Little",
        "kCGColorSpaceSRGB",
        "kCGImageAlphaPremultipliedFirst",
        "kCGInterpolationHigh",
        "kCVPixelBufferHeightKey",
        "kCVPixelBufferIOSurfacePropertiesKey",
        "kCVPixelBufferLock_ReadOnly",
        "kCVPixelBufferMetalCompatibilityKey",
        "kCVPixelBufferPixelFormatTypeKey",
        "kCVPixelBufferPoolMinimumBufferCountKey",
        "kCVPixelBufferWidthKey",
        "kCVPixelFormatType_32BGRA",
    ),
    "VideoToolbox": (
        "VTIsHardwareDecodeSupported",
    ),
    "Vision": (
        "VNCoreMLModel",
        "VNCoreMLRequest",
        "VNDetectFaceRectanglesRequest",
        "VNImageCropAndScaleOptionScaleFit",
        "VNImageRequestHandler",
    ),
}
# module aliases used in the source -> framework module
ALIASES = {"AV": "AVFoundation", "CM": "CoreMedia", "Q": "Quartz", "CoreML": "CoreML", "Vision": "Vision",
           "Foundation": "Foundation", "VT": "VideoToolbox"}

_done = False
_lock = threading.Lock()


def prewarm() -> list[str]:
    """Idempotent. Returns symbols that could not be resolved (missing frameworks are tolerated)."""
    global _done
    with _lock:
        if _done:
            return []
        missing = []
        for mod_name, names in SYMBOLS.items():
            try:
                mod = importlib.import_module(mod_name)
            except ImportError:
                missing.extend(f"{mod_name}.{n}" for n in names)
                continue
            for n in names:
                try:
                    getattr(mod, n)
                except AttributeError:
                    missing.append(f"{mod_name}.{n}")
        _done = True
    if missing:
        log.warning("PyObjC symbols not available: %s", ", ".join(missing))
    return missing
