import json
import sqlite3

import pytest

from db_manager import DatabaseManager


def source(path, *, size=100, modified=200, digest="new-sha"):
    return {
        "absolute_path": str(path),
        "relative_path": path.name,
        "file_size": size,
        "modified_ns": modified,
        "sha256": digest,
    }


def staged_index(database, *, error=""):
    stage = database.create_index_stage()
    stage.append_pages([
        {"page_number": 1, "text": "first page", "extraction_method": "docling", "error_message": error},
        {"page_number": 2, "text": "second page", "extraction_method": "rapidocr_fallback", "error_message": ""},
    ])
    stage.append_chunks([
        {"page_number": 1, "chunk_index": 0, "chunk_text": "new alpha", "search_text": "newalpha", "embedding": [1.0, 0.0], "metadata": {"part": 1}},
        {"page_number": 2, "chunk_index": 0, "chunk_text": "new beta", "search_text": "newbeta", "embedding": [0.0, 1.0], "metadata": {"part": 2}},
    ])
    return stage


def existing_index(database, tmp_path):
    old_path = tmp_path / "old.pdf"
    old_path.write_bytes(b"synthetic pdf placeholder")
    library_id = database.add_library("Synthetic", "medicine", str(old_path))
    file_id = database.upsert_file_metadata(
        library_id, str(old_path), old_path.name, 10, 20, "old-sha", "ready"
    )
    database.replace_file_content(
        file_id,
        library_id,
        [{"page_number": 1, "text": "old page", "extraction_method": "text", "error_message": ""}],
        [{"page_number": 1, "chunk_index": 0, "chunk_text": "old content", "search_text": "oldtoken", "embedding": [0.5, 0.5]}],
    )
    database.set_file_index_profile(file_id, {"dimension": 2, "name": "old"}, "old-fingerprint")
    return library_id, file_id, old_path


def snapshot(database, file_id):
    row = dict(database.conn.execute("SELECT * FROM textbook_files WHERE id=?", (file_id,)).fetchone())
    pages = [tuple(row) for row in database.conn.execute(
        "SELECT page_number, extraction_method, char_count, error_message FROM textbook_pages WHERE file_id=? ORDER BY page_number",
        (file_id,),
    )]
    chunks = [tuple(row) for row in database.conn.execute(
        "SELECT page_number, chunk_index, chunk_text, search_text, embedding_dim, metadata_json FROM chunks_v2 WHERE file_id=? ORDER BY page_number, chunk_index",
        (file_id,),
    )]
    return row, pages, chunks


def test_staged_publish_replaces_content_metadata_and_fts(tmp_path):
    with DatabaseManager(tmp_path / "db.sqlite") as database:
        library_id, file_id, old_path = existing_index(database, tmp_path)
        replacement = source(old_path, size=300, modified=400)
        stage = staged_index(database, error="docling: one page warning")
        stage_path = stage.path

        result = database.publish_index_stage(
            stage,
            file_id,
            library_id,
            source=replacement,
            profile={"dimension": 2, "name": "new"},
            fingerprint="new-fingerprint",
            expected_dimension=2,
            revalidate_source=lambda: dict(replacement),
        )

        assert result == {
            "page_count": 2,
            "chunk_count": 2,
            "ocr_page_count": 1,
            "embedding_dimension": 2,
            "errors": ["docling: one page warning"],
        }
        row = database.get_file_by_path(str(old_path))
        assert row["status"] == "warning"
        assert row["page_count"] == 2
        assert row["ocr_page_count"] == 1
        assert row["file_size"] == 300
        assert row["modified_ns"] == 400
        assert row["sha256"] == "new-sha"
        assert json.loads(row["index_profile_json"])["name"] == "new"
        assert row["index_fingerprint"] == "new-fingerprint"
        assert "one page warning" in row["error_message"]
        assert database.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'newalpha'"
        ).fetchone()[0] == 1
        assert database.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'oldtoken'"
        ).fetchone()[0] == 0
        assert not stage_path.exists()


def test_source_change_rejects_publish_and_preserves_existing_index(tmp_path):
    with DatabaseManager(tmp_path / "db.sqlite") as database:
        library_id, file_id, old_path = existing_index(database, tmp_path)
        before = snapshot(database, file_id)
        replacement = source(old_path)
        stage = staged_index(database)
        stage_path = stage.path

        with pytest.raises(ValueError, match="索引期间发生变化"):
            database.publish_index_stage(
                stage,
                file_id,
                library_id,
                source=replacement,
                profile={"dimension": 2},
                fingerprint="new-fingerprint",
                revalidate_source=lambda: source(old_path, digest="changed-sha"),
            )

        assert snapshot(database, file_id) == before
        assert not stage_path.exists()


def test_publish_sql_failure_rolls_back_rows_and_file_metadata(tmp_path):
    with DatabaseManager(tmp_path / "db.sqlite") as database:
        library_id, file_id, old_path = existing_index(database, tmp_path)
        before = snapshot(database, file_id)
        database.conn.execute(
            """CREATE TRIGGER reject_replacement BEFORE INSERT ON textbook_pages
               WHEN new.extraction_method='docling'
               BEGIN SELECT RAISE(ABORT, 'injected publish failure'); END"""
        )
        database.conn.commit()
        stage = staged_index(database)
        stage_path = stage.path

        with pytest.raises(sqlite3.IntegrityError, match="injected publish failure"):
            database.publish_index_stage(
                stage,
                file_id,
                library_id,
                source=source(old_path),
                profile={"dimension": 2, "name": "new"},
                fingerprint="new-fingerprint",
            )

        assert snapshot(database, file_id) == before
        assert database.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'oldtoken'"
        ).fetchone()[0] == 1
        assert not stage_path.exists()


@pytest.mark.parametrize(
    "pages,chunks,message",
    [
        (
            [{"page_number": 2, "text": "gap", "extraction_method": "docling"}],
            [{"page_number": 2, "chunk_index": 0, "chunk_text": "x", "search_text": "x", "embedding": [1.0]}],
            "页码不完整",
        ),
        (
            [{"page_number": 1, "text": "page", "extraction_method": "docling"}],
            [{"page_number": 1, "chunk_index": 0, "chunk_text": "x", "search_text": "x", "embedding": None}],
            "向量缺失",
        ),
        (
            [{"page_number": 1, "text": "page", "extraction_method": "docling"}],
            [
                {"page_number": 1, "chunk_index": 0, "chunk_text": "x", "search_text": "x", "embedding": [1.0]},
                {"page_number": 1, "chunk_index": 1, "chunk_text": "y", "search_text": "y", "embedding": [1.0, 2.0]},
            ],
            "维度不一致",
        ),
        (
            [{"page_number": 1, "text": "page", "extraction_method": "docling"}],
            [{"page_number": 2, "chunk_index": 0, "chunk_text": "x", "search_text": "x", "embedding": [1.0]}],
            "无对应页面",
        ),
    ],
)
def test_stage_validation_rejects_incomplete_data_and_cleans_up(tmp_path, pages, chunks, message):
    with DatabaseManager(tmp_path / "db.sqlite") as database:
        with pytest.raises(ValueError, match=message):
            with database.create_index_stage() as stage:
                stage_path = stage.path
                stage.append_pages(pages)
                stage.append_chunks(chunks)
                stage.validate()
        assert not stage_path.exists()


def test_stage_context_cleans_up_after_exception(tmp_path):
    with DatabaseManager(tmp_path / "db.sqlite") as database:
        with pytest.raises(RuntimeError, match="synthetic"):
            with database.create_index_stage() as stage:
                stage_path = stage.path
                raise RuntimeError("synthetic")
        assert not stage_path.exists()


@pytest.mark.parametrize("method,table", [("append_pages", "pages"), ("append_chunks", "chunks")])
def test_stage_append_rolls_back_partial_generator_batch(tmp_path, method, table):
    with DatabaseManager(tmp_path / "db.sqlite") as database:
        with database.create_index_stage() as stage:
            if method == "append_pages":
                first = {"page_number": 1, "text": "page", "extraction_method": "docling"}
            else:
                first = {
                    "page_number": 1,
                    "chunk_index": 0,
                    "chunk_text": "chunk",
                    "search_text": "chunk",
                    "embedding": [1.0],
                }

            def failing_rows():
                yield first
                raise RuntimeError("batch interrupted")

            with pytest.raises(RuntimeError, match="batch interrupted"):
                getattr(stage, method)(failing_rows())

            count = stage.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0


def test_replace_file_content_accepts_one_shot_generators(tmp_path):
    with DatabaseManager(tmp_path / "db.sqlite") as database:
        library_id, file_id, old_path = existing_index(database, tmp_path)
        pages = (
            item for item in [
                {"page_number": 1, "text": "replacement", "extraction_method": "rapidocr_fallback", "error_message": "fallback"},
            ]
        )
        chunks = (
            item for item in [
                {"page_number": 1, "chunk_index": 0, "chunk_text": "replacement", "search_text": "replacementtoken", "embedding": [1.0, 0.0]},
            ]
        )

        database.replace_file_content(file_id, library_id, pages, chunks)

        row = database.get_file_by_path(str(old_path))
        assert row["status"] == "warning"
        assert row["page_count"] == 1
        assert row["ocr_page_count"] == 1
        assert row["error_message"] == "fallback"
        assert database.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'replacementtoken'"
        ).fetchone()[0] == 1
