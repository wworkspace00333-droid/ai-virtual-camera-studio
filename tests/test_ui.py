import pytest
from PySide6.QtGui import QKeySequence

from vcstudio.app.application import AppContext
from vcstudio.ui.main_window import MainWindow
from vcstudio.ui.widgets import StagePlaceholder


@pytest.fixture
def win(qtbot, paths):
    ctx = AppContext(paths, configure_logging=False)
    w = MainWindow(ctx)
    qtbot.addWidget(w)
    w.show()
    yield w
    w.close()
    ctx.shutdown()


def test_twelve_pages_in_spec_order(win):
    assert [p.title for p in win.pages] == ["Dashboard", "Input", "Preview", "AI Engine", "Detection", "ROI Editor",
                                            "Replacement", "Profiles", "Virtual Camera", "Performance", "Logs", "Settings"]
    for i in range(len(win.pages)):
        win.nav.setCurrentRow(i)
        assert win.stack.currentIndex() == i


def test_camera_page_reports_not_installed_honestly(win):
    page = win.pages[win.page_keys().index("camera")]
    if not page.status.device_visible:
        assert not page.pattern_btn.isEnabled() and not page.loop_btn.isEnabled()
        assert "Not" in page.cards["Camera device"].value.text()


def test_all_hotkey_actions_enabled(win):
    assert all(a.isEnabled() for a in win.actions_.actions.values())


def test_dashboard_shows_live_resources(win):
    win._tick()
    assert win.dashboard.cards["CPU"].value.text().endswith("%")
    assert "MB" in win.dashboard.cards["Memory"].value.text() or "GB" in win.dashboard.cards["Memory"].value.text()


def test_next_profile_action_and_status(win):
    win.ctx.profiles.create("Second")
    win.on_action("next_profile")
    assert win.ctx.profiles.active_name == "Second"
    assert "Second" in win.status_profile.text()
    assert win.ctx.settings.get("active_profile") == "Second"


def test_hotkey_conflicts_detected(win):
    conflicts = win.actions_.apply_bindings({"next_profile": "Ctrl+Alt+K", "toggle_preview": "Ctrl+Alt+K"})
    assert len(conflicts) == 1
    assert win.actions_.actions["next_profile"].shortcut() == QKeySequence("Ctrl+Alt+K")
    assert win.actions_.actions["toggle_preview"].shortcut().isEmpty()


def test_performance_mode_persists(win):
    perf = win.performance
    for b in perf.group.buttons():
        if b.property("mode") == "performance":
            b.setChecked(True)
    assert win.ctx.settings.get("performance.mode") == "performance"


def test_last_page_restored(qtbot, paths):
    ctx = AppContext(paths, configure_logging=False)
    w = MainWindow(ctx); qtbot.addWidget(w)
    w.goto("logs"); w.close(); ctx.shutdown()
    ctx2 = AppContext(paths, configure_logging=False)
    w2 = MainWindow(ctx2); qtbot.addWidget(w2)
    assert w2.pages[w2.stack.currentIndex()].key == "logs"
    w2.close(); ctx2.shutdown()
