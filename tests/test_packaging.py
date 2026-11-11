"""Resource location (source tree vs packaged) and the packaged bundle, if it has been built."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from vcstudio.core import resources

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dist" / "AI Virtual Camera Studio.app"


def test_source_tree_resources():
    assert not resources.frozen()
    assert resources.models_dir() == ROOT / "models"
    assert resources.sample("bus.jpg").exists()
    assert resources.scripts_dir() == ROOT / "scripts"
    assert resources.native_lib("libVCSNative.dylib") == ROOT / "build" / "native" / "libVCSNative.dylib"


def test_frozen_layout(monkeypatch, tmp_path):
    fw = tmp_path / "X.app" / "Contents" / "Frameworks"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(fw), raising=False)
    assert resources.models_dir() == fw / "models"
    assert resources.sample("bus.jpg") == fw / "sample_data" / "bus.jpg"
    assert resources.native_lib("libVCSShm.dylib") == fw / "native" / "libVCSShm.dylib"
    assert resources.obs_plugin_bundle() == fw.parent / "Resources" / "obs-plugin" / "vcstudio-obs.plugin"


def test_no_repo_relative_paths_left():
    """Runtime code must resolve bundled files through vcstudio.core.resources only."""
    offenders = []
    for p in (ROOT / "vcstudio").rglob("*.py"):
        if p.name == "resources.py":
            continue
        s = p.read_text()
        if "parents[2]" in s or '"tests/data' in s:
            offenders.append(str(p.relative_to(ROOT)))
    assert offenders == []


@pytest.mark.skipif(not (APP / "Contents" / "MacOS" / "vcstudio").exists(), reason="app not built (scripts/build_app.sh)")
def test_built_bundle_layout_and_signature():
    info = plistlib.loads((APP / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "AIVirtualCameraStudio"
    assert info["CFBundleIdentifier"] == "com.aivcstudio.app"
    assert "NSCameraUsageDescription" in info
    fw = APP / "Contents" / "Frameworks"
    assert (fw / "native" / "libVCSNative.dylib").exists() and (fw / "native" / "libVCSShm.dylib").exists()
    assert list((fw / "models").glob("*.mlpackage"))
    assert (fw / "sample_data" / "bus.jpg").exists()
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(APP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not os.environ.get("VCS_LIVE") or not (APP / "Contents" / "MacOS" / "vcstudio").exists(),
                    reason="VCS_LIVE=1 and a built app required")
def test_built_bundle_selfcheck(tmp_path):
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"}   # no venv, no repo on the path
    r = subprocess.run([str(APP / "Contents" / "MacOS" / "AIVirtualCameraStudio"), "--self-check"],
                       capture_output=True, text=True, timeout=300, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "OK:" in r.stdout
