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
            self.assertEqual(pages[0]["extraction_method"], "qwen_ocr")
            self.assertIn("识别后的", pages[0]["text"])
            self.assertFalse(provider.assert_path.exists())


def test_qwen_provider_retries_and_returns_text(tmp_path, monkeypatch):
    import httpx
    import ocr_client
    from openai import OpenAI
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error":{"message":"limited"}})
        return httpx.Response(200, json={"id":"synthetic", "model":"qwen3.5-ocr",
            "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"OCR 文本"}}],
            "usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}})
    monkeypatch.setattr(ocr_client.time, "sleep", lambda seconds: None)
    provider = ocr_client.QwenOCRClient("synthetic")
    provider.client = OpenAI(api_key="synthetic", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    image = tmp_path/"page.png"
    image.write_bytes(b"synthetic")
    assert provider.recognize_image(image) == "OCR 文本"
    assert len(calls) == 2
    assert provider.last_trace["usage"]["total_tokens"] == 12
    assert provider.last_trace["attempts"] == 2
