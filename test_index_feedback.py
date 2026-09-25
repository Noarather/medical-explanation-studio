from types import SimpleNamespace

import fitz
import pytest

from db_manager import DatabaseManager
from parser_health import index_error_summary
from pdf_parser import PDFParser
from services import TextbookService


def synthetic_pdf(path, count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as doc:
        for number in range(count):
            doc.new_page().insert_text((72, 72), f"Synthetic offline textbook page {number+1}, with enough body text for indexing.")
        doc.save(path)
    return path


def test_unicode_text_pages_preserve_order_and_source(tmp_path):
    path = synthetic_pdf(tmp_path / "中文目录" / "教材（第10版）.pdf", 9)
    original = path.read_bytes()
    progress = []
    pages = PDFParser().extract_pages(path, progress=lambda a,b: progress.append((a,b)))
    assert path.read_bytes() == original
    assert [p["page_number"] for p in pages] == list(range(1,10))
    assert all(p["extraction_method"] == "text" and not p["error_message"] for p in pages)
    assert progress == [(i,9) for i in range(1,10)]


def test_cloud_error_preserves_source_and_is_not_blank(tmp_path):
    path = synthetic_pdf(tmp_path / "中文目录" / "教材.pdf")
    original = path.read_bytes()
    ocr = SimpleNamespace(recognize_image=lambda path: "")
    with pytest.raises(RuntimeError, match="第 1 页.*空内容"):
        PDFParser().extract_pages(path, ocr, force_ocr=True)
    assert path.read_bytes() == original


@pytest.mark.parametrize("method,error,degraded", [
    ("docling", "", False),
    ("docling_fallback", "docling: failed", True),
    ("rapidocr_fallback", "docling: failed", True),
])
def test_index_completion_exposes_degradation_without_discarding_chunks(tmp_path, monkeypatch, method, error, degraded):
    path = synthetic_pdf(tmp_path / "book.pdf")
    config = {"retrieval": {"chunk_size": 500, "chunk_overlap": 50},
              "memory_guard": {"max_memory_mb": 2048, "batch_process_size": 50}}
    with DatabaseManager(str(tmp_path / "isolated.db")) as database:
        library = database.add_library("Synthetic", "test", str(path))
        service = TextbookService(database, config, SimpleNamespace(get_embeddings=lambda texts: [[1., 0.] for _ in texts]))
        monkeypatch.setattr(service.memory, "check", lambda *args: None)
        monkeypatch.setattr("parser_health.parser_health", lambda *args: {"missing": []})
        monkeypatch.setattr(service.parser, "iter_page_batches", lambda *args, **kwargs: (batch for batch in [[{
            "page_number": 1, "text": "Synthetic offline content", "extraction_method": method, "error_message": error,
        }]]))
        result = service.scan_library(library)
        row = database.get_file_by_path(str(path.resolve()))
        assert database.file_chunk_count(row["id"]) > 0
        assert row["status"] == ("warning" if degraded else "ready")
        assert ("有解析降级／异常" in result["message"]) == degraded
        assert result["fallback_pages"] == int(degraded)
        assert result["error_pages"] == int(bool(error))


def test_native_loader_failure_has_readable_summary():
    error = "docling: Conversion failed; docling-parse could not load document abc: Failed to load document with key key=C:/fixtures/synthetic.pdf"
    summary = index_error_summary(error)
    assert "不等于没有索引" in summary
    assert "技术详情" in summary
    assert "abc" not in summary
