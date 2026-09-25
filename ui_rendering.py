"""Process-local WebEngine compatibility defaults; never changes system GPU settings."""
import os
import sys


def configure_renderer(environ=None, platform=None):
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    default = "software" if platform == "win32" else "auto"
    mode = str(environ.get("MEDEXPLAIN_UI_RENDERER", default)).strip().lower()
    if mode not in {"auto", "software"}:
        mode = default
    if mode == "software":
        flags = environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
        if "--disable-gpu" not in flags.split():
            environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " --disable-gpu").strip()
        # WebEngineView also composites through Qt Quick. Avoid the driver path
        # in both layers, not only Chromium. This does not affect Torch/OCR compute.
        environ["QT_QUICK_BACKEND"] = "software"
    return mode
