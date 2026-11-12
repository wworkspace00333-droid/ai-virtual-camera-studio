"""Virtual Camera > Check the whole chain: child process, report parsing, capture paused and resumed."""
import sys

from vcstudio.ui import chain_check

FAKE = r'''
import json, sys
a = sys.argv
out = a[a.index("--pipeline-e2e-test") + 1]
obs = "--input" in a
if "--no-report" in a:
    sys.exit(1)
json.dump({"ok": True, "client_distinct_fps": 30.1 if obs else 60.0, "client_resolution": "1920x1080",
           "frames_with_replacement": 1.0, "pipeline_latency_ms": 2.8, "pipeline_latency_p95_ms": 4.0,
           "camera_sent": 600, "camera_dropped": 0, "render_errors": 0, "ai_model": "yolo11n-seg", "ai_fps": 20.0,
           "device": a[a.index("--device") + 1]}, open(out, "w"))
'''


def run_page(qtbot, paths, monkeypatch, tmp_path, extra=(), obs_live=True, running=False):
    from vcstudio.app.application import AppContext
    from vcstudio.ui.pages.camera_page import VirtualCameraPage
    fake = tmp_path / "fake_e2e.py"; fake.write_text(FAKE)
    monkeypatch.setattr(chain_check, "self_command", lambda: [sys.executable, str(fake), *extra])
    monkeypatch.setattr("vcstudio.core.obs_shm_source.obs_sharing_status", lambda *a: {"available": obs_live})
    ctx = AppContext(paths, configure_logging=False)
    calls = []
    states = iter(["running"] if running else ["stopped"])
    monkeypatch.setattr(ctx.capture, "stats", lambda: type("S", (), {"state": next(states, "stopped")})())
    monkeypatch.setattr(ctx, "stop_capture", lambda: calls.append("stop"))
    monkeypatch.setattr(ctx, "start_capture", lambda: calls.append("start"))
    page = VirtualCameraPage(ctx)
    qtbot.addWidget(page.widget)
    page.check_chain()
    qtbot.waitUntil(lambda: page.chain_btn.isEnabled(), timeout=30000)
    return ctx, page, calls


def test_both_checks_reported(qtbot, paths, monkeypatch, tmp_path):
    ctx, page, calls = run_page(qtbot, paths, monkeypatch, tmp_path, running=True)
    try:
        assert [t for t, _, _ in page.last_chain] == ["Test scene 1080p60", "Your OBS video"]
        assert all(r["ok"] for _, r, _ in page.last_chain)
        assert page.last_chain[1][1]["client_distinct_fps"] == 30.1
        assert all(p.startswith(str(paths.root / "reports")) for _, _, p in page.last_chain)
        assert "60.0" in page.chain_info.text() and "30.1" in page.chain_info.text()
        assert calls == ["stop", "start"]            # own processing paused for the check, then resumed
    finally:
        ctx.shutdown()


def test_without_obs_and_missing_report(qtbot, paths, monkeypatch, tmp_path):
    ctx, page, calls = run_page(qtbot, paths, monkeypatch, tmp_path, extra=("--no-report",), obs_live=False)
    try:
        assert len(page.last_chain) == 1 and page.last_chain[0][1]["ok"] is False
        assert "did not write a report" in page.last_chain[0][1]["error"]
        assert "OBS is not sharing" in page.chain_info.text()
        assert calls == []                           # it was not running: nothing to pause or resume
    finally:
        ctx.shutdown()
