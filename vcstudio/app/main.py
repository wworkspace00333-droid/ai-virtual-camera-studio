from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if "--camera-loopback-test" in argv:
        from vcstudio.app.selftest import camera_loopback_selftest
        i = argv.index("--camera-loopback-test")
        opt = lambda k, d: argv[argv.index(k) + 1] if k in argv else d
        return camera_loopback_selftest(argv[i + 1], float(opt("--seconds", 6)), opt("--device", "AI Virtual Camera"),
                                        opt("--sink", "AI Virtual Camera Input"), float(opt("--fps", 60)))
    if "--leak-bisect" in argv or "--run-script" in argv:
        # dev: run a scripts/*.py tool inside the app bundle (it needs the bundle's camera permission)
        import runpy
        from vcstudio.core.resources import scripts_dir
        scripts = scripts_dir()
        if "--leak-bisect" in argv:
            i, name = argv.index("--leak-bisect"), "leak_bisect.py"
            rest = argv[i + 1:]
        else:
            i = argv.index("--run-script")
            name, rest = argv[i + 1], argv[i + 2:]
        target = (scripts / name).resolve()
        if target.parent != scripts or not target.exists():      # only tools shipped in scripts/
            print(f"unknown script {name}", file=sys.stderr)
            return 2
        sys.argv = [name] + rest
        runpy.run_path(str(target), run_name="__main__")
        return 0
    if "--soak" in argv:
        from vcstudio.app.soak import main_entry
        return main_entry(argv)
    if "--pipeline-e2e-test" in argv:
        from vcstudio.app.selftest import pipeline_e2e_selftest
        i = argv.index("--pipeline-e2e-test")
        opt = lambda k, d: argv[argv.index(k) + 1] if k in argv else d
        return pipeline_e2e_selftest(argv[i + 1], float(opt("--seconds", 8)), opt("--device", "AI Virtual Camera"),
                                     opt("--sink", "AI Virtual Camera Input"), opt("--input", None),
                                     opt("--pixel-format", None))
    if "--self-check" in argv:
        from vcstudio.app.selftest import bundle_selfcheck
        i = argv.index("--self-check")
        nxt = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else None
        return bundle_selfcheck(nxt)
    if "--capture-selftest" in argv:
        from vcstudio.app.selftest import capture_selftest
        i = argv.index("--capture-selftest")
        secs = float(argv[argv.index("--seconds") + 1]) if "--seconds" in argv else 10.0
        hint = argv[argv.index("--device") + 1] if "--device" in argv else "OBS Virtual Camera"
        ai = argv[argv.index("--with-ai") + 1] if "--with-ai" in argv else None
        return capture_selftest(argv[i + 1], secs, hint, ai)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from vcstudio import APP_NAME, __version__
    from vcstudio.app.application import AppContext
    from vcstudio.ui.main_window import MainWindow
    from vcstudio.ui.theme import QSS

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("AIVCStudio")
    app.setStyleSheet(QSS)
    from vcstudio.core import single_instance
    from vcstudio.core.paths import AppPaths
    other = single_instance.acquire(AppPaths.default().root)
    if other is not None:
        from PySide6.QtWidgets import QMessageBox

        from vcstudio import i18n
        i18n.set_language(i18n.resolve("auto"))
        msg = i18n.tr("AI Virtual Camera Studio is already running (process {pid}). Two copies would fight over "
                      "the output camera, so this one will close. Switch to the open window in the Dock.",
                      pid=other or "?")
        print(msg, file=sys.stderr)
        if "--snapshot" not in argv and "--smoke-test" not in argv:
            QMessageBox.information(None, APP_NAME, msg)
        return 3
    ctx = AppContext()
    from vcstudio import i18n
    lang = argv[argv.index("--lang") + 1] if "--lang" in argv else ctx.settings.get("ui.language")
    i18n.set_language(i18n.resolve(lang))

    def excepthook(t, v, tb):
        logging.getLogger("vcstudio.crash").critical("Unhandled exception", exc_info=(t, v, tb))
        try:
            ctx.db.log_event("ERROR", "app", f"unhandled {t.__name__}: {v}")
        except Exception:
            pass
    sys.excepthook = excepthook

    win = MainWindow(ctx)
    win.show()
    # Let Ctrl+C in a terminal quit cleanly
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer(); keepalive.start(250); keepalive.timeout.connect(lambda: None)
    if "--smoke-test" in argv:  # launch, render, quit (used by tests/CI)
        QTimer.singleShot(1500, app.quit)
    if "--autostart-capture" in argv:
        QTimer.singleShot(300, ctx.start_capture)
    if "--snapshot" in argv:  # dev aid: save page screenshots after capture warms up, then quit
        out = Path(argv[argv.index("--snapshot") + 1]); out.mkdir(parents=True, exist_ok=True)

        def snap():
            keys = win.page_keys() if "--snapshot-all" in argv else ("preview", "ai", "detection", "dashboard")
            for key in keys:
                win.goto(key)
                for _ in range(10):
                    app.processEvents(); win.feeder.tick()
                win._tick(); app.processEvents()
                win.grab().save(str(out / f"{key}.png"))
            app.quit()
        QTimer.singleShot(5000, snap)
    rc = app.exec()
    ctx.shutdown()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
