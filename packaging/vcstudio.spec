# PyInstaller spec: frozen payload of AI Virtual Camera Studio (built by scripts/build_app.sh).
# The bundle's real CFBundleExecutable is the Swift launcher (native/HostApp/main.swift) that
# build_app.sh adds afterwards; this spec only produces the Python payload `Contents/MacOS/vcstudio`.
import glob
import os
from pathlib import Path

ROOT = Path(SPECPATH).parent
VERSION = os.environ.get("VCS_VERSION", "0.1.0")

datas = []
for mp in sorted((ROOT / "models").glob("*.mlpackage")):
    datas.append((str(mp), f"models/{mp.name}"))
    if mp.with_suffix(".json").exists():
        datas.append((str(mp.with_suffix(".json")), "models"))
for name in ("bus.jpg", "logo_alpha.png", "clip_h264.mp4"):
    datas.append((str(ROOT / "tests" / "data" / name), "sample_data"))
for sub in ("images", "labels"):            # accuracy check data (AI Engine page)
    datas.append((str(ROOT / "tests" / "data" / "coco16" / sub), f"sample_data/coco16/{sub}"))
for py in glob.glob(str(ROOT / "scripts" / "*.py")):
    datas.append((py, "scripts"))
# The OBS plugin is itself a bundle; PyInstaller would flatten it, so build_app.sh copies it into
# Contents/Resources/obs-plugin/ after this step.

binaries = [(str(ROOT / "build" / "native" / n), "native") for n in ("libVCSNative.dylib", "libVCSShm.dylib")]

hidden = [
    "objc", "Foundation", "AppKit", "Quartz", "CoreMedia", "CoreML", "AVFoundation", "Vision", "Metal",
    "VideoToolbox", "libdispatch", "scipy.optimize", "psutil", "cv2",
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
]
from PyInstaller.utils.hooks import collect_submodules  # noqa: E402
hidden += collect_submodules("vcstudio")   # pages and dialogs are imported lazily inside functions

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick",
              "PySide6.QtQml", "PySide6.QtPdf", "PySide6.QtMultimedia", "PySide6.QtCharts"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="vcstudio", debug=False, strip=False,
          upx=False, console=False, target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="vcstudio")
app = BUNDLE(
    coll,
    name="AI Virtual Camera Studio.app",
    icon=str(ROOT / "packaging" / "AppIcon.icns"),
    bundle_identifier="com.aivcstudio.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "AI Virtual Camera Studio",
        "CFBundleDisplayName": "AI Virtual Camera Studio",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "LSArchitecturePriority": ["arm64"],
        "NSHighResolutionCapable": True,
        "NSCameraUsageDescription": ("AI Virtual Camera Studio reads video from OBS Virtual Camera (or another "
                                     "camera you select) to process it locally on this Mac."),
        "LSApplicationCategoryType": "public.app-category.video",
    },
)
