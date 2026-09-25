"""Cloud parsing memory boundaries and per-page checkpoint contracts."""
from types import SimpleNamespace
import fitz
import pytest
from memory_guard import MemoryLimitExceeded, MemorySnapshot, MemoryState
from parser_health import index_error_summary
from pdf_parser import PDFParser

def synthetic_pdf(path, count=1):
    with fitz.open() as doc:
        for n in range(count):
            doc.new_page().insert_text((72,72), f"Synthetic textbook page {n+1}")
        doc.save(path)
    return path

def test_page_iterator_yields_before_next_paid_call(tmp_path):
    calls = []
    ocr = SimpleNamespace(recognize_image=lambda path: calls.append(path) or "Cloud text")
    iterator = PDFParser().iter_page_batches(synthetic_pdf(tmp_path/"book.pdf", 3), ocr, True)
    assert next(iterator)[0]["page_number"] == 1
    assert len(calls) == 1
    assert next(iterator)[0]["page_number"] == 2
    assert len(calls) == 2
    iterator.close()
    assert len(calls) == 2

def test_resume_skips_already_committed_pages(tmp_path):
    calls = []
    ocr = SimpleNamespace(recognize_image=lambda path: calls.append(path) or "Cloud text")
    pages = list(PDFParser().iter_page_batches(synthetic_pdf(tmp_path/"book.pdf", 3), ocr, True, start_page=3))
    assert pages[0][0]["page_number"] == 3
    assert len(calls) == 1

def test_hard_memory_error_never_triggers_paid_ocr(tmp_path):
    snap = MemorySnapshot(2050, 1500, 2048, MemoryState.HARD, "pdf_parse")
    class Guard:
        def sample(self, label): return snap
        def recover(self, label, release, **kw):
            raise MemoryLimitExceeded(snap, **kw)
    ocr = SimpleNamespace(recognize_image=lambda path: pytest.fail("must not call cloud"))
    with pytest.raises(MemoryLimitExceeded) as caught:
        PDFParser(memory_guard=Guard()).extract_pages(synthetic_pdf(tmp_path/"book.pdf"), ocr, True)
    assert caught.value.stage == "cloud_parse"
    assert caught.value.page_range == (1,1)

def test_normal_memory_does_not_sleep_or_collect_every_page(tmp_path):
    guard = SimpleNamespace(sample=lambda label: MemorySnapshot(100,1500,2048,MemoryState.NORMAL,label),
                            recover=lambda *a, **kw: pytest.fail("normal state must not recover"))
    assert len(PDFParser(memory_guard=guard).extract_pages(synthetic_pdf(tmp_path/"book.pdf",3))) == 3

def test_closing_iterator_releases_file_handle(tmp_path):
    path = synthetic_pdf(tmp_path/"book.pdf",3)
    iterator = PDFParser().iter_page_batches(path)
    next(iterator)
    iterator.close()
    path.rename(tmp_path/"renamed.pdf")

def test_old_memory_error_remains_readable():
    summary = index_error_summary("MEMORY_LIMIT_EXCEEDED; stage=docling_parse; rss=2048.1MB; soft=1740.8MB; hard=2048.0MB; pages=25-25; batch=1")
    assert "2048.1 MB" in summary and "不会覆盖原有可用索引" in summary
