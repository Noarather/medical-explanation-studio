from types import SimpleNamespace

import fitz
import pytest

from memory_guard import MemoryLimitExceeded, MemorySnapshot, MemoryState
from parser_health import index_error_summary
from pdf_parser import PDFParser


def synthetic_pdf(path, count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as doc:
        for number in range(count):
            page = doc.new_page()
            page.insert_text((72, 72), f"Synthetic page {number + 1}")
        doc.save(path)
    return path


def parsed_pages(start, end):
    return [
        {
            "page_number": number,
            "text": f"page {number}",
            "extraction_method": "docling",
            "error_message": "",
            "blocks": [],
        }
        for number in range(start, end + 1)
    ]


def snapshot(state, current=100.0, soft=1500.0, hard=2048.0):
    return MemorySnapshot(current, soft, hard, state, "pdf_parse")


def test_memory_failures_shrink_batches_without_fallback(tmp_path, monkeypatch):
    path = synthetic_pdf(tmp_path / "book.pdf", 10)
    parser = PDFParser(page_batch_size=8)
    attempts = []
    fallback_calls = []

    def convert(_path, start, end):
        attempts.append((start, end))
        if end - start + 1 > 1:
            raise MemoryError("failed to allocate")
        return parsed_pages(start, end)

    monkeypatch.setattr(parser, "_docling_batch", convert)
    monkeypatch.setattr(parser, "_fallback_page", lambda *args: fallback_calls.append(args))

    pages = parser.extract_pages(path)

    assert attempts[:4] == [(1, 8), (1, 4), (1, 2), (1, 1)]
    assert attempts[4:] == [(2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 8), (9, 9), (10, 10)]
    assert [page["page_number"] for page in pages] == list(range(1, 11))
    assert fallback_calls == []


def test_ordinary_failure_only_falls_back_at_single_page(tmp_path, monkeypatch):
    path = synthetic_pdf(tmp_path / "book.pdf", 2)
    parser = PDFParser(page_batch_size=2)
    attempts = []

    def convert(_path, start, end):
        attempts.append((start, end))
        raise RuntimeError("damaged page structure")

    monkeypatch.setattr(parser, "_docling_batch", convert)
    monkeypatch.setattr(parser, "_fallback_page", lambda _page, number, *_args: {
        "page_number": number,
        "text": f"fallback {number}",
        "extraction_method": "docling_fallback",
        "error_message": "",
        "blocks": [],
    })

    pages = parser.extract_pages(path)

    assert attempts == [(1, 2), (1, 1), (2, 2)]
    assert [page["extraction_method"] for page in pages] == ["docling_fallback"] * 2
    assert all("damaged page structure" in page["error_message"] for page in pages)


def test_non_memory_onnx_failure_uses_single_page_fallback(tmp_path, monkeypatch):
    path = synthetic_pdf(tmp_path / "book.pdf", 2)
    parser = PDFParser(page_batch_size=2)
    attempts = []
    fallback_calls = []

    def convert(_path, start, end):
        attempts.append((start, end))
        raise RuntimeError("ONNXRuntimeError: invalid graph input shape")

    def fallback(_page, number, *_args):
        fallback_calls.append(number)
        return {
            "page_number": number,
            "text": f"fallback {number}",
            "extraction_method": "docling_fallback",
            "error_message": "",
            "blocks": [],
        }

    monkeypatch.setattr(parser, "_docling_batch", convert)
    monkeypatch.setattr(parser, "_fallback_page", fallback)

    pages = parser.extract_pages(path)

    assert attempts == [(1, 2), (1, 1), (2, 2)]
    assert fallback_calls == [1, 2]
    assert [page["extraction_method"] for page in pages] == ["docling_fallback"] * 2
    assert all("invalid graph input shape" in page["error_message"] for page in pages)


def test_soft_pressure_releases_resources_and_keeps_smaller_batch(tmp_path, monkeypatch):
    path = synthetic_pdf(tmp_path / "book.pdf", 9)
    states = iter([MemoryState.SOFT, MemoryState.NORMAL, MemoryState.NORMAL, MemoryState.NORMAL])

    class Guard:
        def __init__(self):
            self.releases = 0

        def sample(self, _label):
            return snapshot(next(states))

        def recover(self, _label, release, **_kwargs):
            release()
            self.releases += 1
            return snapshot(MemoryState.NORMAL)

    guard = Guard()
    parser = PDFParser(page_batch_size=8, memory_guard=guard)
    attempts = []
    monkeypatch.setattr(parser, "_docling_batch", lambda _path, start, end: (
        attempts.append((start, end)) or parsed_pages(start, end)
    ))

    pages = parser.extract_pages(path)

    assert attempts == [(1, 4), (5, 8), (9, 9)]
    assert len(pages) == 9
    assert guard.releases == 1


def test_persistent_single_page_hard_pressure_is_typed(tmp_path):
    path = synthetic_pdf(tmp_path / "book.pdf")

    class Guard:
        def sample(self, _label):
            return snapshot(MemoryState.HARD, current=2050.5)

        def recover(self, _label, release, **_kwargs):
            release()
            return snapshot(MemoryState.HARD, current=2050.5)

    parser = PDFParser(memory_guard=Guard())
    with pytest.raises(MemoryLimitExceeded) as raised:
        parser.extract_pages(path)

    error = raised.value
    assert error.stage == "docling_parse"
    assert error.file_path == str(path)
    assert error.page_range == (1, 1)
    assert error.batch_size == 1
    assert error.snapshot.current_mb == 2050.5
    assert "rss=2050.5MB" in str(error)


def test_extract_pages_flattens_batches_and_releases_models(tmp_path, monkeypatch):
    path = synthetic_pdf(tmp_path / "book.pdf", 3)
    parser = PDFParser(page_batch_size=2)
    parser._converter = object()
    parser._rapidocr = object()
    monkeypatch.setattr(parser, "_docling_batch", lambda _path, start, end: parsed_pages(start, end))

    pages = parser.extract_pages(path)

    assert [page["page_number"] for page in pages] == [1, 2, 3]
    assert parser._converter is None
    assert parser._rapidocr is None


def test_closing_batch_iterator_releases_models(tmp_path, monkeypatch):
    path = synthetic_pdf(tmp_path / "book.pdf", 3)
    parser = PDFParser(page_batch_size=2)
    monkeypatch.setattr(parser, "_docling_batch", lambda _path, start, end: parsed_pages(start, end))
    iterator = parser.iter_page_batches(path)

    assert [page["page_number"] for page in next(iterator)] == [1, 2]
    parser._converter = SimpleNamespace()
    parser._rapidocr = SimpleNamespace()
    iterator.close()

    assert parser._converter is None
    assert parser._rapidocr is None


def test_memory_error_summary_is_actionable():
    summary = index_error_summary(
        "MEMORY_LIMIT_EXCEEDED; stage=docling_parse; rss=2048.1MB; "
        "soft=1740.8MB; hard=2048.0MB; pages=25-25; batch=1"
    )

    assert "最小单页范围" in summary
    assert "2048.1 MB" in summary
    assert "2048.0 MB" in summary
    assert "不会覆盖原有可用索引" in summary
    assert "拆分" in summary
    assert "技术详情" in summary
