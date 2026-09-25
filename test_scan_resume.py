"""Offline end-to-end checks of durable, bounded textbook indexing."""
from types import SimpleNamespace

import fitz
import pytest

from db_manager import DatabaseManager
from services import TextbookService


def setup_service(tmp_path, monkeypatch, embed):
    pdf = tmp_path / "book.pdf"
    with fitz.open() as document:
        for _ in range(3):
            document.new_page()
        document.save(pdf)
    database = DatabaseManager(str(tmp_path / "app.db"))
    library = database.add_library("Synthetic", "test", str(pdf))
    config = {"retrieval": {"chunk_size": 500, "chunk_overlap": 50},
              "memory_guard": {"max_memory_mb": 2048, "batch_process_size": 1}}
    service = TextbookService(database, config, SimpleNamespace(get_embeddings=embed))
    monkeypatch.setattr("parser_health.parser_health", lambda *args: {"missing": []})
    monkeypatch.setattr(service.memory, "check", lambda *args: None)
    monkeypatch.setattr("services.backup_dir", lambda: tmp_path)
    return database, library, service, pdf


def pages(start=1):
    for number in range(start, 4):
        yield [{"page_number": number, "text": f"Synthetic textbook page {number}.",
                "extraction_method": "docling", "error_message": ""}]


def test_failed_embedding_resumes_without_reparsing_or_rebilling_saved_batch(tmp_path, monkeypatch):
    calls = []
    fail = True

    def embed(texts):
        nonlocal fail
        calls.append(texts)
        assert service.parser._converter is None
        if fail and len(calls) == 2:
            raise RuntimeError("synthetic API failure")
        return [[1., 0.] for _ in texts]

    database, library, service, pdf = setup_service(tmp_path, monkeypatch, embed)
    try:
        service.parser._converter = object()
        monkeypatch.setattr(service.parser, "iter_page_batches", lambda *a, **kw: pages(kw["start_page"]))
        with pytest.raises(RuntimeError, match="API failure"):
            service.scan_library(library)
        assert database.conn.execute("SELECT COUNT(*) FROM chunks_v2").fetchone()[0] == 0
        assert len(list(tmp_path.glob("medexplain-index-*.sqlite3"))) == 1
        fail = False
        monkeypatch.setattr(service.parser, "iter_page_batches", lambda *a, **kw: pytest.fail("must reuse parsed stage"))
        result = service.scan_library(library)
        assert result["chunks"] == 3
        assert len(calls) == 4  # one saved + failed request + two remaining
        assert calls.count(calls[0]) == 1
        assert not list(tmp_path.glob("medexplain-index-*.sqlite3"))
        assert database.get_file_by_path(str(pdf))["status"] == "ready"
    finally:
        database.close()


def test_interrupted_parsing_resumes_at_next_saved_physical_page(tmp_path, monkeypatch):
    database, library, service, _ = setup_service(tmp_path, monkeypatch, lambda texts: [[1.] for _ in texts])
    starts = []

    def interrupted(*args, start_page, **kwargs):
        starts.append(start_page)
        yield next(pages())
        raise RuntimeError("interrupted parsing")

    try:
        monkeypatch.setattr(service.parser, "iter_page_batches", interrupted)
        with pytest.raises(RuntimeError, match="interrupted parsing"):
            service.scan_library(library)

        def resumed(*args, start_page, **kwargs):
            starts.append(start_page)
            yield from pages(start_page)

        monkeypatch.setattr(service.parser, "iter_page_batches", resumed)
        assert service.scan_library(library)["pages"] == 3
        assert starts == [1, 2]
    finally:
        database.close()


def test_failed_rebuild_preserves_old_searchable_content(tmp_path, monkeypatch):
    database, library, service, pdf = setup_service(tmp_path, monkeypatch, lambda texts: [[1.] for _ in texts])
    try:
        monkeypatch.setattr(service.parser, "iter_page_batches", lambda *a, **kw: pages(kw["start_page"]))
        service.scan_library(library)
        before = [tuple(r) for r in database.conn.execute("SELECT * FROM chunks_v2")]
        service.embedding_client.get_embeddings = lambda texts: []
        with pytest.raises(ValueError, match="数量不一致"):
            service.scan_library(library, force_ocr=True)
        assert [tuple(r) for r in database.conn.execute("SELECT * FROM chunks_v2")] == before
    finally:
        database.close()
