from types import SimpleNamespace

from db_manager import DatabaseManager
from memory_guard import MemoryLimitExceeded, MemorySnapshot, MemoryState
import worker


def test_worker_restarts_once_at_same_physical_page_then_fails(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    with DatabaseManager(path) as db:
        job = db.create_job("scan", "Synthetic", {"library_id": 1})
    monkeypatch.setattr(worker, "runtime_config", lambda: {
        "database": {"path": path}, "dashscope": {"api_key": ""},
    })
    monkeypatch.setattr(worker, "_embedding", lambda *a: None)
    monkeypatch.setattr(worker, "QwenOCRClient", lambda *a: None)
    progress = iter([199, 0])  # Retry may fail before its first progress callback.

    def fail(library_id, force, control):
        control.progress(next(progress), 398, "Synthetic parse")
        raise MemoryLimitExceeded(
            MemorySnapshot(2050, 1740, 2048, MemoryState.HARD),
            stage="docling_parse", page_range=(200, 200), batch_size=1,
        )

    monkeypatch.setattr(worker, "TextbookService", lambda *a: SimpleNamespace(scan_library=fail))
    assert worker.run_job(job) == 75
    with DatabaseManager(path) as db:
        assert db.get_job(job)["status"] == "queued"
    assert worker.run_job(job) == 1
    with DatabaseManager(path) as db:
        assert db.get_job(job)["status"] == "failed"


def test_worker_allows_restart_after_actual_forward_progress(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    with DatabaseManager(path) as db:
        job = db.create_job("scan", "Synthetic", {"library_id": 1})
    monkeypatch.setattr(worker, "runtime_config", lambda: {
        "database": {"path": path}, "dashscope": {"api_key": ""},
    })
    monkeypatch.setattr(worker, "_embedding", lambda *a: None)
    monkeypatch.setattr(worker, "QwenOCRClient", lambda *a: None)
    pages = iter([200, 300])

    def fail(library_id, force, control):
        page = next(pages)
        control.progress(page - 1, 398, "Synthetic parse")
        raise MemoryLimitExceeded(
            MemorySnapshot(2050, 1740, 2048, MemoryState.HARD),
            stage="docling_parse", page_range=(page, page), batch_size=1,
        )

    monkeypatch.setattr(worker, "TextbookService", lambda *a: SimpleNamespace(scan_library=fail))
    assert worker.run_job(job) == 75
    assert worker.run_job(job) == 75
