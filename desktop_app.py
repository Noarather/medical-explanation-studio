"""MedExplain Studio desktop shell: QWebEngineView host + QWebChannel bridge.

Modes:
  (no args)        launch the GUI
  --worker <id>    execute one queued job (see worker.py)
  --bundle-check   packaging import probe
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 - must precede QApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

ROOT = Path(__file__).resolve().parent
DEV_SERVER_URL = os.getenv("MEDEXPLAIN_DEV_SERVER", "http://localhost:5173")


def frontend_url() -> QUrl:
    index = ROOT / "frontend" / "dist" / "index.html"
    if os.getenv("MEDEXPLAIN_DEV", "").strip():
        return QUrl(DEV_SERVER_URL)
    if index.exists():
        return QUrl.fromLocalFile(str(index))
    from app_paths import temp_dir
    fallback = temp_dir() / "frontend-missing.html"
    fallback.write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<body style='font-family:sans-serif;max-width:560px;margin:80px auto;line-height:1.8'>"
        "<h2>前端资源未构建</h2>"
        "<p>未找到 frontend/dist/index.html。请先在项目目录执行：</p>"
        "<pre style='background:#f1f3f5;padding:12px;border-radius:6px'>cd frontend\nnpm install\nnpm run build</pre>"
        "<p>开发模式：设置环境变量 MEDEXPLAIN_DEV=1 并运行 npm run dev。</p>"
        "</body></html>",
        encoding="utf-8",
    )
    return QUrl.fromLocalFile(str(fallback))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        from app_paths import database_path
        from ui_bridge.dashboard import DashboardBridge
        from ui_bridge.imports import ImportsBridge
        from ui_bridge.questions import QuestionsBridge
        from ui_bridge.review import ReviewBridge
        from ui_bridge.jobs import JobRunner, JobsBridge
        from ui_bridge.library import LibraryBridge
        from ui_bridge.settings import SettingsBridge
        from ui_bridge.tags import TagsBridge
        from ui_bridge.exports import ExportsBridge

        self.database_path = str(database_path())
        self.setWindowTitle("MedExplain Studio")
        self.resize(1440, 900)
        self.setMinimumSize(1080, 700)

        self.runner = JobRunner(self.database_path, self)
        self.bridges = {
            "dashboard": DashboardBridge(self.database_path, self),
            "jobs": JobsBridge(self.database_path, self.runner, self),
            "settings": SettingsBridge(self.database_path, self),
            "imports": ImportsBridge(self.database_path, self),
            "questions": QuestionsBridge(self.database_path, self),
            "review": ReviewBridge(self.database_path, self),
            "library": LibraryBridge(self.database_path, self),
            "tags": TagsBridge(self.database_path, self),
            "exports": ExportsBridge(self.database_path, self),
        }
        self.channel = QWebChannel(self)
        for name, obj in self.bridges.items():
            self.channel.registerObject(name, obj)
        self.channel.registerObject("jobEvents", self.runner)
        self.channel.registerObject("settingsEvents", self.bridges["settings"])
        self.channel.registerObject("importsEvents", self.bridges["imports"])

        self.view = QWebEngineView(self)
        self.view.page().setWebChannel(self.channel)
        self.setCentralWidget(self.view)
        self.view.load(frontend_url())
        self.runner.start()

    def closeEvent(self, event) -> None:
        if self.bridges["imports"]._batch_tasks.active():
            QMessageBox.information(self, "批量导入正在运行", "请等待批量操作完成，或在题目导入页取消操作后再退出。")
            event.ignore()
            return
        if self.runner.process.state() != QProcess.NotRunning:
            answer = QMessageBox.question(
                self, "任务正在运行", "关闭后任务将在下次启动时继续。现在关闭？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.runner.shutdown()
        event.accept()


def launch() -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("MedExplain Studio")
    window = MainWindow()
    window.show()
    return app.exec()


def bundle_self_check() -> int:
    """Packaging-only import probe; it never reads or mutates business data."""
    import docx  # noqa: F401
    import fitz  # noqa: F401
    import jieba  # noqa: F401
    import openpyxl  # noqa: F401
    import docling  # noqa: F401
    import instructor  # noqa: F401
    import onnxruntime  # noqa: F401
    import rapidocr  # noqa: F401
    from keyring.backends import Windows  # noqa: F401
    from PySide6.QtSvg import QSvgRenderer  # noqa: F401
    from PySide6.QtWebChannel import QWebChannel as _QWebChannel  # noqa: F401
    from PySide6.QtWebEngineCore import QWebEngineSettings  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView as _QWebEngineView  # noqa: F401
    if "--parser-smoke-report" in sys.argv:
        from parser_health import parser_smoke_check
        parser_smoke_check(sys.argv[sys.argv.index("--parser-smoke-report") + 1])
    return 0


if __name__ == "__main__":
    if "--ui-smoke-report" in sys.argv:
        from ui_smoke import run
        raise SystemExit(run(sys.argv[sys.argv.index("--ui-smoke-report") + 1]))
    if "--bundle-check" in sys.argv:
        raise SystemExit(bundle_self_check())
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        from worker import run_job
        raise SystemExit(run_job(sys.argv[2]))
    raise SystemExit(launch())
