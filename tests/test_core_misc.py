import logging
import time

from vcstudio.core.database import Database
from vcstudio.core.logging_setup import setup_logging
from vcstudio.core.performance import FpsCounter, SystemMonitor
from vcstudio.core.system_info import inspect_environment
from vcstudio.core.devices import is_own_output, list_video_devices


def test_database_sessions_and_events(paths):
    db = Database(paths.database_file)
    db.begin_session("t", "Default")
    db.log_event("INFO", "test", "hello", {"a": 1})
    assert db.recent_events()[0]["data"] == {"a": 1}
    db.close()
    db2 = Database(paths.database_file)   # previous session not ended => unclean
    assert db2.unclean_previous_sessions() == 1
    db2.begin_session("t", None); db2.end_session()
    assert db2.unclean_previous_sessions() == 0
    db2.add_benchmark("b", {"chip": "M5"}, {"fps": 1})
    assert db2.benchmarks()[0]["result"] == {"fps": 1}


def test_logging_ring_and_file(paths):
    ring = setup_logging(paths.logs_dir, "INFO")
    got = []
    ring.subscribe(lambda lv, ln: got.append(ln))
    logging.getLogger("t").info("ring-check")
    logging.getLogger("t").debug("hidden")
    assert any("ring-check" in l for l in got) and not any("hidden" in l for l in got)
    for h in logging.getLogger().handlers:
        h.flush()
    assert "ring-check" in (paths.logs_dir / "studio.log").read_text()


def test_system_monitor_sample():
    m = SystemMonitor(memory_warning_mb=100000)
    time.sleep(0.05)
    s = m.sample()
    assert 0 <= s.cpu_percent <= 100 and s.process_rss > 0 and s.mem_total > 0
    assert s.pressure in ("normal", "warning", "critical")
    assert s.gpu_percent is None or 0 <= s.gpu_percent <= 100


def test_fps_counter():
    c = FpsCounter(1.0)
    for i in range(60):
        c.tick(i / 60)
    assert 59 <= c.rate(59 / 60) <= 61


def test_environment_is_apple_silicon():
    env = inspect_environment()
    assert env["arch"] == "arm64" and env["apple_silicon"]
    assert env["metal"]["available"] and env["coreml"]["available"]


def test_own_output_guard():
    assert is_own_output("AI Virtual Camera", "x")
    assert not is_own_output("OBS Virtual Camera", "OBS Camera Extension")
    for d in list_video_devices():
        assert d.is_own_output == is_own_output(d.name, d.model_id)


def test_fps_counter_not_underreported_at_startup():
    c = FpsCounter(1.0)
    for i in range(30):          # 0.5 s of 60 fps
        c.tick(i / 60)
    assert 58 <= c.rate(29 / 60) <= 62
    assert FpsCounter().rate() == 0.0


def test_log_event_after_close_is_ignored(paths):
    db = Database(paths.database_file)
    db.close()
    db.log_event("INFO", "x", "late")   # must not raise
