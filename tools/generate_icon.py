"""Render the source SVG into the Windows ICO used by PyInstaller and Inno Setup."""

from pathlib import Path

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "assets" / "app_icon.svg"
target = ROOT / "assets" / "app_icon.ico"

app = QGuiApplication([])
renderer = QSvgRenderer(str(source))
if not renderer.isValid():
    raise SystemExit(f"Invalid SVG: {source}")
image = QImage(QSize(256, 256), QImage.Format_ARGB32)
image.fill(0)
painter = QPainter(image)
renderer.render(painter, QRectF(0, 0, 256, 256))
painter.end()
if not image.save(str(target), "ICO"):
    raise SystemExit("Qt ICO writer is unavailable")
print(target)
