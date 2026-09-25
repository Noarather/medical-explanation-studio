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


def test_unicode_stream_keeps_original_pages_batches_and_closes_buffers(tmp_path, monkeypatch):
    pytest.importorskip("docling")
    path = synthetic_pdf(tmp_path / "中文目录" / "教材（第10版）.pdf", 9)
    original = path.read_bytes()
    seen = []

    class Converter:
        def convert(self, source, *, page_range):
            assert source.stream.read() == original
            source.stream.seek(0)
            assert source.name == "document.pdf"
            seen.append((page_range, source.stream))
            return SimpleNamespace(document=SimpleNamespace(
                export_to_markdown=lambda **kwargs: f"Synthetic physical page {kwargs['page_no']}"))

    parser = PDFParser()
    monkeypatch.setattr(parser, "_docling_converter", lambda: Converter())
    monkeypatch.setattr(type(path), "read_bytes", lambda _path: pytest.fail("read_bytes must not be used"))
    progress = []
    pages = parser.extract_pages(path, progress=lambda a, b: progress.append((a, b)))
    assert [r[0] for r in seen] == [(1, 8), (9, 9)]
    assert all(r[1].closed for r in seen)
    assert [p["page_number"] for p in pages] == list(range(1, 10))
    assert pages[-1]["text"] == "Synthetic physical page 9"
    assert all(p["extraction_method"] == "docling" and not p["error_message"] for p in pages)
    assert progress == [(8, 9), (9, 9)]


def test_unicode_stream_failure_closes_buffer_and_preserves_local_fallback(tmp_path, monkeypatch):
    pytest.importorskip("docling")
    path = synthetic_pdf(tmp_path / "损坏解析路径" / "教材.pdf")
    seen = []

    class BrokenConverter:
        def convert(self, source, **kwargs):
            seen.append(source.stream)
            raise RuntimeError("synthetic conversion failure")

    parser = PDFParser()
    monkeypatch.setattr(parser, "_docling_converter", lambda: BrokenConverter())
    pages = parser.extract_pages(path)
    assert seen[0].closed
    assert pages[0]["extraction_method"] == "docling_fallback"
    assert "synthetic conversion failure" in pages[0]["error_message"]
    assert "Synthetic offline textbook" in pages[0]["text"]


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
    error = "docling: Conversion failed; docling-parse could not load document abc: Failed to load document with key key=C:/fixtures/教材/测试.pdf"
    summary = index_error_summary(error)
    assert "不等于没有索引" in summary
    assert "技术详情" in summary
    assert "abc" not in summary
