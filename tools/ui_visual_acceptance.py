"""Capture offscreen screenshots of the WebEngine shell for visual acceptance.

Run with the dev venv (frontend/dist must be built first):

    ./.venv/Scripts/python.exe tools/ui_visual_acceptance.py

Outputs land in work/ui-screenshots/ and each capture is sanity-checked so a
fully black / fully white render fails loudly instead of passing silently.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox --lang=zh-CN")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "work" / "ui-screenshots"
os.environ.setdefault("MEDEXPLAIN_DATABASE_PATH", str((OUT / "seed.db").resolve()))

ROUTES = {
    "dashboard": "#/",
    "import": "#/import",
    "questions": "#/questions",
    "review": "#/review",
    "library": "#/library",
    "tags": "#/tags",
    "export": "#/export",
}


def wait_for(predicate, timeout: float, app) -> bool:
    """Pump the event loop until predicate() is true or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def capture(window, app, route: str, name: str) -> Path:
    from PySide6.QtCore import QEventLoop, QTimer

    window.view.page().runJavaScript(f"location.hash = '{route}';")
    # Wait for web fonts to settle, then force a reflow so any glyph that was
    # rasterized with a .notdef fallback before the real font arrived is
    # re-shaped (headless Chromium otherwise leaves intermittent tofu boxes).
    fonts_ready = {"ok": False}

    def on_fonts_ready(_result) -> None:
        fonts_ready["ok"] = True

    window.view.page().runJavaScript(
        "document.fonts.ready.then(() => {"
        "  document.body.style.display = 'none';"
        "  void document.body.offsetHeight;"
        "  document.body.style.display = '';"
        "  return true;"
        "});",
        on_fonts_ready,
    )
    wait_for(lambda: fonts_ready["ok"], timeout=10, app=app)

    # Allow the Vue render + bridge round trip to settle.
    loop = QEventLoop()
    QTimer.singleShot(1500, loop.quit)
    loop.exec()
    app.processEvents()

    image = window.view.grab()
    target = OUT / f"{name}.png"
    image.save(str(target))
    # Sanity: reject pure black/white frames.
    small = image.scaled(8, 8).toImage()
    colors = {small.pixelColor(x, y).name() for x in range(8) for y in range(8)}
    if len(colors) <= 1:
        raise RuntimeError(f"screenshot looks blank: {target}")
    return target


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    from desktop_app import MainWindow

    window = MainWindow()
    window.resize(1440, 900)
    window.show()

    # Wait for the initial page load to finish.
    loaded = {"ok": False}

    def on_load_finished(ok: bool) -> None:
        loaded["ok"] = ok

    window.view.loadFinished.connect(on_load_finished)
    if not wait_for(lambda: loaded["ok"], timeout=30, app=app):
        raise RuntimeError("initial page load did not finish within 30s")

    # Give the SPA a moment to mount and run its first bridge calls.
    loop = QEventLoop()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    for name, route in ROUTES.items():
        print(capture(window, app, route, name))

    window.runner.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
