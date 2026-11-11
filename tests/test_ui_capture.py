import time

from vcstudio.app.application import AppContext
from vcstudio.ui.main_window import MainWindow


def test_capture_from_ui_with_test_pattern(qtbot, paths):
    ctx = AppContext(paths, configure_logging=False)
    ctx.settings.set("input.method", "test_pattern")
    ctx.settings.set("input.preferred_width", 1280)
    ctx.settings.set("input.preferred_height", 720)
    w = MainWindow(ctx); qtbot.addWidget(w); w.show()
    w.goto("preview")
    preview = w.pages[w.page_keys().index("preview")]
    w.input_page.method.setCurrentIndex(w.input_page.method.findData("test_pattern"))
    w.input_page.start()
    qtbot.waitUntil(lambda: ctx.capture.stats().state == "running", timeout=5000)
    qtbot.waitUntil(lambda: preview.original.image is not None and preview.processed.image is not None, timeout=5000)
    assert preview.original.image.width() <= 1280
    # no replacement configured -> the processed view is the untouched passthrough of the same frame
    a, b = preview.original.image, preview.processed.image
    assert a.size() == b.size()
    import numpy as np
    def arr(img):
        img = img.convertToFormat(img.Format.Format_RGB32)
        return np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())[:, : img.width() * 4]
    assert np.abs(arr(a).astype(int) - arr(b).astype(int)).mean() < 4      # GPU bilinear vs CPU decimation
    w._tick()
    assert "Running" in w.dashboard.cards["Input"].value.text()
    w.on_action("stop_processing")
    qtbot.waitUntil(lambda: ctx.capture.stats().state == "stopped", timeout=3000)
    qtbot.waitUntil(lambda: preview.original.image is None and preview.processed.image is None, timeout=3000)
    w.close(); ctx.shutdown()


def test_obs_ipc_method_selectable_with_status(qtbot, paths):
    ctx = AppContext(paths, configure_logging=False)
    w = MainWindow(ctx); qtbot.addWidget(w); w.show(); w.goto("input")
    m = w.input_page.method
    m.setCurrentIndex(m.findData("obs_ipc"))
    assert m.model().item(m.findData("obs_ipc")).isEnabled()
    assert w.input_page.obs_row.isVisible() and not w.input_page.device.isEnabled()
    assert w.input_page.obs_status.text()
    w.close(); ctx.shutdown()
