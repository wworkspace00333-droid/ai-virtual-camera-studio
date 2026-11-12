"""Importing your own Core ML models: validation, metadata, class mapping, auto choice, safe switching."""
import shutil
from pathlib import Path

import pytest

from vcstudio.ai import model_import as mi
from vcstudio.ai.model_manager import REPO_MODELS, ModelManager

DATA = Path(__file__).resolve().parent / "data" / "models"


@pytest.fixture(scope="module")
def custom_detector(tmp_path_factory):
    """A real YOLO detector under another name, standing in for a user's own export."""
    d = tmp_path_factory.mktemp("export") / "my_cards.mlpackage"
    shutil.copytree(REPO_MODELS / "yolo11n.mlpackage", d)
    return d


@pytest.fixture(scope="module")
def report(custom_detector):
    return mi.inspect_model(custom_detector, runs=5)


def test_valid_detector_is_inspected_and_run(report):
    assert report.task == "detect" and report.format == "coreml-pipeline-nms" and report.imgsz == 640
    assert len(report.classes) == 80 and report.classes[0] == "person"
    assert report.sample_detections >= 4 and "person" in report.sample_labels     # bus photo: 4 people
    assert 0 < report.median_ms <= report.p95_ms
    assert report.suggested_name == "my_cards" and report.suggested_confidence == pytest.approx(0.25)


@pytest.mark.parametrize("name,why", [("not_a_detector.mlpackage", "unsupported outputs"),
                                      ("garbage.mlmodel", "does not compile")])
def test_unusable_models_are_rejected(name, why):
    with pytest.raises(mi.ModelImportError, match=why):
        mi.inspect_model(DATA / name, runs=3)


def test_wrong_file_type_is_rejected(tmp_path):
    p = tmp_path / "x.onnx"; p.write_bytes(b"0")
    with pytest.raises(mi.ModelImportError, match="mlpackage or .mlmodel"):
        mi.inspect_model(p)


def test_install_with_class_map_and_threshold(report, tmp_path):
    user = tmp_path / "models"
    info = mi.install(report, user, "my_cards", {"person": "player", "bogus": "x"}, 0.4,
                      builtin_names={"yolo11n"})
    assert info.origin == "user" and info.confidence == pytest.approx(0.4)
    assert info.classes[0] == "player" and info.source_classes[0] == "person"
    assert info.label_map()["person"] == "player"
    with pytest.raises(mi.ModelImportError, match="already installed"):
        mi.install(report, user, "my_cards")
    with pytest.raises(mi.ModelImportError, match="built-in"):
        mi.install(report, user, "yolo11n", builtin_names={"yolo11n"})
    with pytest.raises(mi.ModelImportError, match="between"):
        mi.install(report, user, "other", confidence=1.5)
    assert not [p for p in user.iterdir() if p.name.startswith("tmp")]       # no half-copied leftovers

    mm = ModelManager(user)
    avail = mm.available()
    assert avail["my_cards"].origin == "user" and avail["yolo11n"].origin == "builtin"
    # "auto" never picks an imported model, even the largest
    assert mm.resolve("auto", "detect").origin == "builtin"
    assert mm.resolve("auto", "detect", prefer_large=True).name == "yolo11m"
    assert "my_cards" not in [m.name for m in mm.ranked("detect")]
    assert "my_cards" in [m.name for m in mm.ranked("detect", include_user=True)]

    # the mapped name reaches detections
    from vcstudio.ai.coreml_engine import CoreMLDetector
    from vcstudio.ai.testimages import load_bgra
    from vcstudio.core.frame import Frame
    from vcstudio.core.resources import sample
    img = load_bgra(sample("bus.jpg"))
    det = CoreMLDetector(mm.load(avail["my_cards"]))
    labels = {d.label for d in det.detect(Frame(0, 0, 0, img.shape[1], img.shape[0], array=img, source="t"), 0.4)[0]}
    assert "player" in labels and "person" not in labels

    mm.unload()
    mi.remove(avail["my_cards"])
    mm.invalidate()
    assert "my_cards" not in mm.available()
    with pytest.raises(mi.ModelImportError, match="built-in"):
        mi.remove(avail["yolo11n"])


def test_failed_model_falls_back_to_auto(tmp_path):
    """Safe switching: a chosen model that does not load keeps detection running on the automatic choice."""
    from vcstudio.ai.detection import DetectionConfig
    from vcstudio.ai.engine import AIEngine
    user = tmp_path / "models"; user.mkdir()
    shutil.copy(DATA / "garbage.mlmodel", user / "broken.mlmodel")
    eng = AIEngine(None, ModelManager(user))
    eng._ensure_models(DetectionConfig(detector_model="broken"), "balanced")
    assert eng._detector is not None and eng._detector.name.startswith("yolo11")
    assert "broken" in eng.model_failures
    assert "broken" in eng.stats().quality_note_args.get("model", "") or eng.mq_note
    eng._ensure_models(DetectionConfig(detector_model="broken"), "balanced")       # no retry storm
    eng.configure(DetectionConfig(detector_model="broken"), "balanced")
    assert eng.model_failures == {}                                                # new choice: fresh try


def test_ai_page_import_and_remove(qtbot, paths, custom_detector, monkeypatch):
    from vcstudio.app.application import AppContext
    from vcstudio.ui import model_import_dialog as mid
    from vcstudio.ui.pages.ai_page import AIEnginePage
    ctx = AppContext(paths, configure_logging=False)
    try:
        page = AIEnginePage(ctx)
        qtbot.addWidget(page.widget)

        def fake_exec(dlg):
            dlg.name.setText("table_cards")
            dlg.conf.setValue(0.5)
            dlg.classes.item(0, 1).setText("player")
            dlg.accept()
            return dlg.installed is not None
        monkeypatch.setattr(mid.ModelImportDialog, "exec", fake_exec)
        page.import_model(str(custom_detector))
        qtbot.waitUntil(lambda: page.import_btn.isEnabled(), timeout=60000)
        assert "table_cards" in page.import_status.text(), page.import_status.text()
        body = ctx.profiles.active()["body"]
        assert body["ai"]["detector_model"] == "table_cards"
        assert body["detection"]["confidence"] == pytest.approx(0.5)
        assert page.model.findData("table_cards") >= 0
        page._remove(ctx.models.available()["table_cards"])
        assert ctx.profiles.active()["body"]["ai"]["detector_model"] == "auto"
        assert "table_cards" not in ctx.models.available()
    finally:
        ctx.shutdown()
