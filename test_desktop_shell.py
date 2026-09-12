"""Offscreen smoke test for the WebEngine shell."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
# Isolate from the production database: MainWindow's runner would otherwise
# recover_incomplete_jobs() against the real user DB.
os.environ.setdefault(
    "MEDEXPLAIN_DATABASE_PATH",
    str(Path(tempfile.gettempdir()) / "medexplain_shell_test.db"),
)

from PySide6.QtWidgets import QApplication


class ShellSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_window_registers_bridge_objects(self) -> None:
        from desktop_app import MainWindow
        window = MainWindow()
        self.assertEqual(
            set(window.bridges),
            {"dashboard", "jobs", "settings", "imports", "questions", "review",
             "library", "tags", "exports"})
        self.assertIsNotNone(window.runner)
        window.runner.shutdown()
        window.close()

    def test_bundle_self_check_returns_zero(self) -> None:
        try:
            import docling, instructor, onnxruntime, rapidocr  # noqa: F401
        except ImportError:
            self.skipTest("打包重依赖仅存在于构建环境（.venv-build），跳过探针验证")
        from desktop_app import bundle_self_check
        self.assertEqual(bundle_self_check(), 0)


if __name__ == "__main__":
    unittest.main()
