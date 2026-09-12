"""Offline OCR routing and retry tests with no network calls."""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

import fitz

from pdf_parser import PDFParser


class OCRRoutingTests(unittest.TestCase):
    def test_image_only_page_uses_ocr_and_removes_temp_file(self):
        class FakeOCR:
            calls = 0

            def recognize_image(self, path):
                self.calls += 1
                self.assert_path = Path(path)
                self.assertTrue = self.assert_path.exists()
                return "识别后的教材正文内容，足以进入切块流程。"

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "scan.pdf"
            document = fitz.open()
            page = document.new_page()
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 120), False)
            pixmap.clear_with(255)
            page.insert_image(fitz.Rect(0, 0, 120, 120), stream=pixmap.tobytes("png"))
            document.save(pdf_path)
            document.close()
            provider = FakeOCR()
            pages = PDFParser().extract_pages(pdf_path, provider)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(pages[0]["extraction_method"], "ocr")
            self.assertIn("识别后的", pages[0]["text"])
            self.assertFalse(provider.assert_path.exists())

    def test_qwen_provider_retries_and_returns_text(self):
        fake_dashscope = types.ModuleType("dashscope")

        class MultiModalConversation:
            calls = 0

            @classmethod
            def call(cls, **_kwargs):
                cls.calls += 1
                if cls.calls < 3:
                    raise RuntimeError("temporary")
                message = types.SimpleNamespace(content=[{"text": "OCR 文本"}])
                choice = types.SimpleNamespace(message=message)
                return types.SimpleNamespace(status_code=200, output=types.SimpleNamespace(choices=[choice]))

        fake_dashscope.MultiModalConversation = MultiModalConversation
        fake_dashscope.base_http_api_url = ""
        original = sys.modules.get("dashscope")
        sys.modules["dashscope"] = fake_dashscope
        try:
            import ocr_client
            importlib.reload(ocr_client)
            ocr_client.time.sleep = lambda _seconds: None
            with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
                result = ocr_client.QwenOCRClient("key").recognize_image(handle.name)
            self.assertEqual(result, "OCR 文本")
            self.assertEqual(MultiModalConversation.calls, 3)
        finally:
            if original is None:
                sys.modules.pop("dashscope", None)
            else:
                sys.modules["dashscope"] = original


if __name__ == "__main__":
    unittest.main()
