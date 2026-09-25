"""SQLite persistence for textbook chunks, embeddings and generated explanations.

Dependencies: Python standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import struct
import tempfile
import uuid
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


def encode_embedding(values: Optional[Iterable[float]]) -> Optional[bytes]:
    if values is None:
        return None
    vector = [float(value) for value in values]
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_embedding(value: Optional[bytes], dimension: int) -> Optional[list[float]]:
    if value is None:
        return None
    if len(value) != dimension * 4:
        raise ValueError(f"Embedding byte length does not match dimension {dimension}")
    return list(struct.unpack(f"<{dimension}f", value))


class IndexBuildStage:
    """Disk-backed, disposable storage for one complete file index build."""

    OCR_METHODS = {"ocr", "rapidocr_fallback", "qwen_ocr_fallback"}

    def __init__(self, directory: str | Path, *, resume_key: str | None = None):
        self.persistent = resume_key is not None
        if resume_key is not None:
            import hashlib
            self.path = Path(directory) / ("medexplain-index-" + hashlib.sha256(resume_key.encode()).hexdigest() + ".sqlite3")
        else:
            handle, name = tempfile.mkstemp(prefix="medexplain-index-", suffix=".sqlite3", dir=directory)
            os.close(handle)
            self.path = Path(name)
        self.conn: Optional[sqlite3.Connection] = None
        try:
            self.conn = sqlite3.connect(self.path)
            self.conn.executescript(
                """
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pages (
                    page_number INTEGER PRIMARY KEY,
                    extraction_method TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    error_message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    page_number INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    embedding BLOB,
                    embedding_dim INTEGER,
                    extraction_method TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(page_number, chunk_index)
                );
                """
            )
        except Exception:
            self.cleanup()
            raise

    def _connection(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("索引暂存区已关闭")
        return self.conn

    def append_pages(self, pages: Iterable[dict]) -> None:
        connection = self._connection()
        with connection:
            connection.executemany(
                "INSERT INTO pages VALUES (?, ?, ?, ?)",
                ((int(item["page_number"]), item["extraction_method"],
                  len(item.get("text", "")), item.get("error_message", "")) for item in pages),
            )

    def append_chunks(self, chunks: Iterable[dict]) -> None:
        connection = self._connection()

        def rows():
            for item in chunks:
                embedding = item.get("embedding")
                yield (
                    int(item["page_number"]), int(item["chunk_index"]), item["chunk_text"],
                    item["search_text"], encode_embedding(embedding),
                    len(embedding) if embedding is not None else None,
                    item.get("extraction_method", "text"),
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                )

        with connection:
            connection.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows())

    def validate(self, expected_dimension: Optional[int] = None) -> dict:
        connection = self._connection()
        page = connection.execute(
            "SELECT COUNT(*), MIN(page_number), MAX(page_number), "
            "SUM(extraction_method IN ('ocr','rapidocr_fallback','qwen_ocr_fallback')) FROM pages"
        ).fetchone()
        page_count = int(page[0])
        if not page_count or int(page[1]) != 1 or int(page[2]) != page_count:
            raise ValueError("暂存页码不完整或不连续")
        chunk = connection.execute(
            "SELECT COUNT(*), SUM(embedding IS NULL), MIN(embedding_dim), MAX(embedding_dim) FROM chunks"
        ).fetchone()
        chunk_count = int(chunk[0])
        if not chunk_count:
            raise ValueError("暂存索引没有正文分块")
        if int(chunk[1] or 0) or chunk[2] != chunk[3] or (
            expected_dimension is not None and chunk[2] != int(expected_dimension)
        ):
            raise ValueError("暂存索引向量缺失或维度不一致")
        orphan = connection.execute(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN pages p USING(page_number) WHERE p.page_number IS NULL"
        ).fetchone()[0]
        if orphan:
            raise ValueError("暂存索引包含无对应页面的分块")
        errors = [row[0] for row in connection.execute(
            "SELECT DISTINCT error_message FROM pages WHERE error_message!='' LIMIT 5"
        )]
        return {"page_count": page_count, "chunk_count": chunk_count,
                "ocr_page_count": int(page[3] or 0),
                "embedding_dimension": int(chunk[2]), "errors": errors}

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def cleanup(self) -> None:
        self.close()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "IndexBuildStage":
        return self

    def __exit__(self, *_args) -> None:
        if self.persistent:
            self.close()
        else:
            self.cleanup()

    def save_parsed_batch(self, pages: list[dict], chunks: list[dict]) -> None:
        # Both tables advance together, including when a process is interrupted.
        connection = self._connection()
        connection.execute("SAVEPOINT parsed_batch")
        try:
            # Unlike append_*, these two inserts must share one transaction.
            connection.executemany("INSERT INTO pages VALUES (?, ?, ?, ?)", (
                (p["page_number"], p["extraction_method"], len(p.get("text", "")), p.get("error_message", "")) for p in pages
            ))
            connection.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)", (
                (c["page_number"], c["chunk_index"], c["chunk_text"], c["search_text"],
                 c.get("extraction_method", "text"), json.dumps(c.get("metadata") or {}, ensure_ascii=False)) for c in chunks
            ))
            connection.execute("RELEASE parsed_batch")
        except BaseException:
            connection.execute("ROLLBACK TO parsed_batch")
            connection.execute("RELEASE parsed_batch")
            raise

    def pending_chunks(self, limit: int) -> list[dict]:
        return [dict(zip(("page_number", "chunk_index", "chunk_text"), row)) for row in self._connection().execute(
            "SELECT page_number, chunk_index, chunk_text FROM chunks WHERE embedding IS NULL ORDER BY page_number, chunk_index LIMIT ?", (limit,)
        )]

    def save_vectors(self, chunks: list[dict], vectors, expected_dimension: int = 0) -> None:
        import math
        if len(vectors) != len(chunks):
            raise ValueError("向量服务返回数量不一致，保留断点等待重试")
        dimension = expected_dimension or (len(vectors[0]) if vectors else 0)
        if not dimension or any(len(v) != dimension or not all(math.isfinite(float(x)) for x in v) for v in vectors):
            raise ValueError("向量维度不一致或包含非法数值")
        existing = self._connection().execute("SELECT embedding_dim FROM chunks WHERE embedding IS NOT NULL LIMIT 1").fetchone()
        if existing and existing[0] != dimension:
            raise ValueError("向量维度与已保存断点不一致")
        with self._connection():
            self._connection().executemany(
                "UPDATE chunks SET embedding=?, embedding_dim=? WHERE page_number=? AND chunk_index=?",
                ((encode_embedding(v), dimension, c["page_number"], c["chunk_index"]) for c, v in zip(chunks, vectors)),
            )


class DatabaseManager:
    """Owns the local SQLite knowledge base and commits bounded transactions."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS textbook_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file TEXT NOT NULL,
                source_page INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding BLOB,
                embedding_dim INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_file, source_page, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id TEXT NOT NULL UNIQUE,
                question_text TEXT NOT NULL,
                matched_chunks TEXT,
                match_score REAL,
                match_status TEXT NOT NULL DEFAULT 'unmatched',
                explanation TEXT,
                error_message TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_questions_status ON questions(match_status);

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                version TEXT DEFAULT '',
                root_path TEXT NOT NULL UNIQUE,
                page_offset INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                last_scanned_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS textbook_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER NOT NULL REFERENCES libraries(id),
                absolute_path TEXT NOT NULL UNIQUE,
                relative_path TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready',
                page_count INTEGER NOT NULL DEFAULT 0,
                ocr_page_count INTEGER NOT NULL DEFAULT 0,
                index_profile_json TEXT NOT NULL DEFAULT '{}',
                index_fingerprint TEXT NOT NULL DEFAULT '',
                error_message TEXT DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_textbook_files_library ON textbook_files(library_id, status);
            CREATE TABLE IF NOT EXISTS textbook_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL REFERENCES textbook_files(id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL,
                extraction_method TEXT NOT NULL,
                char_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT DEFAULT '',
                UNIQUE(file_id, page_number)
            );
            CREATE TABLE IF NOT EXISTS chunks_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER NOT NULL REFERENCES libraries(id),
                file_id INTEGER NOT NULL REFERENCES textbook_files(id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                search_text TEXT NOT NULL,
                embedding BLOB,
                embedding_dim INTEGER,
                extraction_method TEXT NOT NULL DEFAULT 'text',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(file_id, page_number, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_library ON chunks_v2(library_id, file_id);

            CREATE TABLE IF NOT EXISTS question_sets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_type TEXT NOT NULL,
                question_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS imported_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                set_id TEXT NOT NULL REFERENCES question_sets(id) ON DELETE CASCADE,
                external_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                question_type TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                pipeline_status TEXT NOT NULL DEFAULT 'queued',
                retrieval_status TEXT NOT NULL DEFAULT 'pending',
                generation_status TEXT NOT NULL DEFAULT 'pending',
                match_confidence TEXT NOT NULL DEFAULT 'pending',
                review_status TEXT NOT NULL DEFAULT 'pending',
                generation_mode TEXT DEFAULT '',
                match_score REAL NOT NULL DEFAULT 0,
                explanation TEXT DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                error_message TEXT DEFAULT '',
                viewed_at TEXT,
                reviewed_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(set_id, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_imported_questions_filter
                ON imported_questions(set_id, review_status, pipeline_status, subject);
            CREATE TABLE IF NOT EXISTS review_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_pk INTEGER NOT NULL REFERENCES imported_questions(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS question_trash (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                set_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                review_actions_json TEXT NOT NULL DEFAULT '[]',
                deleted_at TEXT NOT NULL,
                purge_after TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_question_trash_deleted
                ON question_trash(deleted_at, purge_after, set_id, external_id);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                message TEXT DEFAULT '',
                control TEXT NOT NULL DEFAULT 'run',
                error_message TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                level TEXT NOT NULL DEFAULT 'info',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS export_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                set_id TEXT NOT NULL REFERENCES question_sets(id),
                export_type TEXT NOT NULL,
                output_path TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS question_tag_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                set_id TEXT NOT NULL REFERENCES question_sets(id) ON DELETE CASCADE,
                subject TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                label TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'candidate',
                usage_count INTEGER NOT NULL DEFAULT 0,
                merged_into TEXT DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(set_id, subject, normalized_name)
            );
            CREATE INDEX IF NOT EXISTS idx_question_tag_catalog_scope
                ON question_tag_catalog(set_id, subject, status, label);
            """
        )
        # v4: keep the PDF physical page separate from the printed textbook page.
        # Printed page = PDF page - page_offset. Existing libraries remain 1:1.
        library_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(libraries)").fetchall()
        }
        if "page_offset" not in library_columns:
            self.conn.execute(
                "ALTER TABLE libraries ADD COLUMN page_offset INTEGER NOT NULL DEFAULT 0"
            )
        if "calibration_json" not in library_columns:
            self.conn.execute("ALTER TABLE libraries ADD COLUMN calibration_json TEXT NOT NULL DEFAULT '{}'")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS library_page_map (
            library_id INTEGER NOT NULL REFERENCES libraries(id),
            pdf_page INTEGER NOT NULL, printed_page INTEGER NOT NULL,
            PRIMARY KEY (library_id, pdf_page))""")
        file_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(textbook_files)").fetchall()
        }
        if "index_profile_json" not in file_columns:
            self.conn.execute(
                "ALTER TABLE textbook_files ADD COLUMN index_profile_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "index_fingerprint" not in file_columns:
            self.conn.execute(
                "ALTER TABLE textbook_files ADD COLUMN index_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        chunk_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(chunks_v2)").fetchall()
        }
        if "metadata_json" not in chunk_columns:
            self.conn.execute(
                "ALTER TABLE chunks_v2 ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        question_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(imported_questions)").fetchall()
        }
        if "retrieval_status" not in question_columns:
            self.conn.execute(
                "ALTER TABLE imported_questions ADD COLUMN retrieval_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "generation_status" not in question_columns:
            self.conn.execute(
                "ALTER TABLE imported_questions ADD COLUMN generation_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "match_confidence" not in question_columns:
            self.conn.execute(
                "ALTER TABLE imported_questions ADD COLUMN match_confidence TEXT NOT NULL DEFAULT 'pending'"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_imported_questions_workflow "
            "ON imported_questions(set_id, retrieval_status, generation_status, review_status)"
        )
        try:
            self.conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    search_text, content='chunks_v2', content_rowid='id', tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS chunks_v2_ai AFTER INSERT ON chunks_v2 BEGIN
                    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_v2_ad AFTER DELETE ON chunks_v2 BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, search_text) VALUES ('delete', old.id, old.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_v2_au AFTER UPDATE ON chunks_v2 BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, search_text) VALUES ('delete', old.id, old.search_text);
                    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
                END;
                """
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError("当前 SQLite 未启用 FTS5，无法建立教材全文索引") from exc
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '8')"
        )
        self.conn.commit()
        self._migrate_legacy_data()
        self._migrate_question_status_dimensions()
        self._migrate_single_pdf_libraries()
        inferred = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='printed_page_inference_v4'"
        ).fetchone()
        if not inferred:
            self.infer_all_library_page_offsets()
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES"
                "('printed_page_inference_v4', CURRENT_TIMESTAMP)"
            )
            self.conn.commit()

    def _migrate_single_pdf_libraries(self) -> None:
        """Adopt the sole known PDF as the library source for pre-v3 directory records."""
        rows = self.conn.execute(
            """SELECT l.id, l.root_path, COUNT(f.id) AS file_count, MAX(f.absolute_path) AS pdf_path
               FROM libraries l LEFT JOIN textbook_files f ON f.library_id=l.id
               WHERE l.active=1 GROUP BY l.id"""
        ).fetchall()
        changed = False
        for row in rows:
            source = Path(row["root_path"])
            pdf_path = row["pdf_path"]
            if source.suffix.lower() != ".pdf" and row["file_count"] == 1 and pdf_path:
                candidate = Path(pdf_path)
                if candidate.suffix.lower() == ".pdf":
                    self.conn.execute("UPDATE libraries SET root_path=? WHERE id=?", (str(candidate), row["id"]))
                    changed = True
        if changed:
            self.conn.commit()
        self.conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '8')")
        self.conn.commit()

    @staticmethod
    def _validation_failure(error_message: str) -> bool:
        text = str(error_message or "")
        return any(token in text for token in (
            "模型输出", "格式修复", "解析结构", "结构化解析", "JSON", "标签", "一句话简析",
            "output_truncated", "invalid_json", "schema_validation_failed",
        ))

    @staticmethod
    def _confidence_from_evidence(
        score: float, evidence: list[dict], *, matched: bool,
    ) -> str:
        if not matched:
            return "low"
        scores = sorted(
            (float(item.get("score") or 0) for item in evidence if isinstance(item, dict)),
            reverse=True,
        )
        top = max([float(score or 0), *scores], default=0.0)
        if top >= 0.70 or sum(value >= 0.65 for value in scores) >= 2:
            return "strong"
        return "standard"

    def _migrate_question_status_dimensions(self) -> None:
        """Backfill the v8 two-axis workflow without invoking any external service."""
        marker = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='question_status_dimensions_v8'"
        ).fetchone()
        if marker:
            return
        rows = self.conn.execute(
            "SELECT id,pipeline_status,generation_mode,match_score,evidence_json,error_message "
            "FROM imported_questions ORDER BY id"
        ).fetchall()
        with self.conn:
            for row in rows:
                try:
                    evidence = json.loads(row["evidence_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    evidence = []
                pipeline = str(row["pipeline_status"] or "queued")
                mode = str(row["generation_mode"] or "")
                error = str(row["error_message"] or "")
                score = float(row["match_score"] or 0)
                if pipeline == "generated":
                    generation = "generated"
                    retrieval = "matched" if mode != "general_knowledge" else (
                        "unmatched" if evidence else "no_library"
                    )
                elif pipeline == "unmatched":
                    if evidence and (self._validation_failure(error) or score >= 0.60) and "相似度未达到阈值" not in error:
                        pipeline = "error"
                        retrieval, generation = "matched", "validation_error"
                    else:
                        retrieval, generation = "unmatched", "pending"
                elif pipeline == "no_library":
                    retrieval, generation = "no_library", "pending"
                elif pipeline == "error":
                    if evidence:
                        retrieval = "matched"
                        generation = "validation_error" if self._validation_failure(error) else "provider_error"
                    else:
                        retrieval, generation = "error", "pending"
                else:
                    retrieval, generation = "pending", "pending"
                confidence = self._confidence_from_evidence(
                    score, evidence, matched=retrieval == "matched",
                ) if retrieval not in {"pending", "no_library", "error"} else retrieval
                self.conn.execute(
                    """UPDATE imported_questions
                       SET pipeline_status=?,retrieval_status=?,generation_status=?,match_confidence=?
                       WHERE id=?""",
                    (pipeline, retrieval, generation, confidence, int(row["id"])),
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES"
                "('question_status_dimensions_v8',CURRENT_TIMESTAMP)"
            )

    def _migrate_legacy_data(self) -> None:
        """Copy v1 CLI rows into the desktop model once without deleting the source tables."""
        migrated = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='legacy_migrated'"
        ).fetchone()
        if migrated:
            return
        with self.conn:
            legacy_chunks = int(self.conn.execute("SELECT COUNT(*) FROM textbook_chunks").fetchone()[0])
            desktop_chunks = int(self.conn.execute("SELECT COUNT(*) FROM chunks_v2").fetchone()[0])
            if legacy_chunks and not desktop_chunks:
                cursor = self.conn.execute(
                    "INSERT INTO libraries(name, subject, version, root_path) VALUES(?, ?, ?, ?)",
                    ("旧版教材库", "未分类", "v1", "legacy://textbooks"),
                )
                library_id = int(cursor.lastrowid)
                source_files = self.conn.execute(
                    "SELECT DISTINCT source_file FROM textbook_chunks ORDER BY source_file"
                ).fetchall()
                for source in source_files:
                    source_file = source["source_file"]
                    file_cursor = self.conn.execute(
                        """INSERT INTO textbook_files
                           (library_id, absolute_path, relative_path, file_size, modified_ns, sha256,
                            status, page_count, updated_at)
                           VALUES (?, ?, ?, 0, 0, ?, 'ready',
                             (SELECT COUNT(DISTINCT source_page) FROM textbook_chunks WHERE source_file=?),
                             CURRENT_TIMESTAMP)""",
                        (library_id, f"legacy://{source_file}", source_file, f"legacy-{source_file}", source_file),
                    )
                    file_id = int(file_cursor.lastrowid)
                    pages = self.conn.execute(
                        "SELECT DISTINCT source_page FROM textbook_chunks WHERE source_file=? ORDER BY source_page",
                        (source_file,),
                    ).fetchall()
                    self.conn.executemany(
                        "INSERT INTO textbook_pages(file_id, page_number, extraction_method) VALUES(?, ?, 'text')",
                        [(file_id, page["source_page"]) for page in pages],
                    )
                    chunks = self.conn.execute(
                        """SELECT source_page, chunk_index, chunk_text, embedding, embedding_dim
                           FROM textbook_chunks WHERE source_file=? ORDER BY source_page, chunk_index""",
                        (source_file,),
                    ).fetchall()
                    self.conn.executemany(
                        """INSERT INTO chunks_v2
                           (library_id, file_id, page_number, chunk_index, chunk_text, search_text,
                            embedding, embedding_dim, extraction_method)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'text')""",
                        [
                            (
                                library_id, file_id, row["source_page"], row["chunk_index"], row["chunk_text"],
                                " ".join(re.findall(r"[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?", row["chunk_text"].lower())),
                                row["embedding"], row["embedding_dim"],
                            )
                            for row in chunks
                        ],
                    )

            legacy_questions = int(self.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
            desktop_questions = int(self.conn.execute("SELECT COUNT(*) FROM imported_questions").fetchone()[0])
            if legacy_questions and not desktop_questions:
                set_id = "legacy-v1-import"
                self.conn.execute(
                    """INSERT OR IGNORE INTO question_sets
                       (id, name, source_path, source_type, question_count)
                       VALUES (?, '旧版导入', 'legacy://questions', 'legacy', ?)""",
                    (set_id, legacy_questions),
                )
                rows = self.conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
                self.conn.executemany(
                    """INSERT OR IGNORE INTO imported_questions
                       (set_id, external_id, subject, question_type, prompt_text, raw_json,
                        pipeline_status, generation_mode, match_score, explanation, evidence_json,
                        error_message, updated_at)
                       VALUES (?, ?, '未分类', 'A1', ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))""",
                    [
                        (
                            set_id, row["question_id"], row["question_text"],
                            json.dumps({"id": row["question_id"], "subject": "未分类", "type": "A1",
                                        "question": row["question_text"], "options": [], "answer": ""}, ensure_ascii=False),
                            "generated" if row["explanation"] else row["match_status"],
                            "textbook" if row["explanation"] else "", row["match_score"] or 0,
                            row["explanation"] or "", row["matched_chunks"] or "[]",
                            row["error_message"] or "", row["updated_at"],
                        )
                        for row in rows
                    ],
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('legacy_migrated', CURRENT_TIMESTAMP)"
            )

    def clear_chunks(self) -> None:
        self.conn.execute("DELETE FROM textbook_chunks")
        self.conn.commit()

    def upsert_chunks(self, chunks: list[dict]) -> None:
        rows = [
            (
                item["source_file"], item["source_page"], item["chunk_index"],
                item["chunk_text"], encode_embedding(item.get("embedding")),
                len(item["embedding"]) if item.get("embedding") is not None else None,
            )
            for item in chunks
        ]
        self.conn.executemany(
            """
            INSERT INTO textbook_chunks
                (source_file, source_page, chunk_index, chunk_text, embedding, embedding_dim)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_file, source_page, chunk_index) DO UPDATE SET
                chunk_text=excluded.chunk_text,
                embedding=excluded.embedding,
                embedding_dim=excluded.embedding_dim
            """,
            rows,
        )
        self.conn.commit()

    def fetch_chunks(self, require_embedding: bool = True) -> list[dict]:
        where = "WHERE embedding IS NOT NULL" if require_embedding else ""
        rows = self.conn.execute(
            f"SELECT id, source_file, source_page, chunk_index, chunk_text, embedding, embedding_dim FROM textbook_chunks {where} ORDER BY id"
        ).fetchall()
        return [
            {
                **dict(row),
                "embedding": decode_embedding(row["embedding"], row["embedding_dim"])
                if row["embedding"] is not None else None,
            }
            for row in rows
        ]

    def save_question(
        self,
        question_id: str,
        question_text: str,
        matched_chunks: list[dict],
        match_score: float,
        match_status: str,
        explanation: str = "",
        error_message: str = "",
    ) -> None:
        evidence = json.dumps(matched_chunks, ensure_ascii=False)
        self.conn.execute(
            """
            INSERT INTO questions
                (question_id, question_text, matched_chunks, match_score, match_status, explanation, error_message, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(question_id) DO UPDATE SET
                question_text=excluded.question_text,
                matched_chunks=excluded.matched_chunks,
                match_score=excluded.match_score,
                match_status=excluded.match_status,
                explanation=excluded.explanation,
                error_message=excluded.error_message,
                updated_at=CURRENT_TIMESTAMP
            """,
            (question_id, question_text, evidence, match_score, match_status, explanation, error_message),
        )
        self.conn.commit()

    def get_question(self, question_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM questions WHERE question_id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def query_by_status(self, status: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT question_id, question_text, match_score, error_message FROM questions WHERE match_status=? ORDER BY question_id",
            (status,),
        ).fetchall()
        return [dict(row) for row in rows]

    def explanation_map(self) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT question_id, explanation, match_score, match_status, matched_chunks FROM questions"
        ).fetchall()
        return {row["question_id"]: dict(row) for row in rows}

    # ---- Standalone desktop repositories -------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: object) -> None:
        self.conn.execute(
            "INSERT INTO app_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def dashboard_counts(self) -> dict[str, int]:
        queries = {
            "libraries": "SELECT COUNT(*) FROM libraries WHERE active=1",
            "files": "SELECT COUNT(*) FROM textbook_files WHERE status='ready'",
            "questions": "SELECT COUNT(*) FROM imported_questions",
            "waiting": "SELECT COUNT(*) FROM imported_questions WHERE pipeline_status='queued'",
            "pending": "SELECT COUNT(*) FROM imported_questions WHERE review_status='pending' AND pipeline_status='generated'",
            "unmatched": "SELECT COUNT(*) FROM imported_questions WHERE pipeline_status='unmatched'",
            "failed": "SELECT COUNT(*) FROM jobs WHERE status='failed'",
        }
        return {key: int(self.conn.execute(sql).fetchone()[0]) for key, sql in queries.items()}

    def add_library(
        self, name: str, subject: str, root_path: str, version: str = "", page_offset: int = 0
    ) -> int:
        source = Path(root_path)
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise ValueError("教材来源必须是可以访问的 PDF 文件")
        cursor = self.conn.execute(
            "INSERT INTO libraries(name, subject, version, root_path, page_offset) VALUES(?, ?, ?, ?, ?)",
            (name.strip(), subject.strip(), version.strip(), str(source.resolve()), int(page_offset)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def update_library(
        self, library_id: int, name: str, subject: str, version: str, root_path: str,
        page_offset: int = 0,
    ) -> None:
        source = Path(root_path)
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise ValueError("教材来源必须是可以访问的 PDF 文件")
        previous = self.get_library(library_id)
        self.conn.execute(
            "UPDATE libraries SET name=?, subject=?, version=?, root_path=? WHERE id=?",
            (name.strip(), subject.strip(), version.strip(), str(source.resolve()), library_id),
        )
        if not previous or previous["root_path"] != str(source.resolve()) or previous["page_offset"] != int(page_offset):
            self.set_library_page_offset(library_id, int(page_offset))
        else:
            self.conn.commit()  # Metadata-only edits preserve verified multi-section pagination.

    def set_library_page_offset(self, library_id: int, page_offset: int) -> None:
        """Save calibration and migrate stored evidence without changing PDF locators."""
        page_offset = int(page_offset)
        paths = {
            row["absolute_path"] for row in self.conn.execute(
                "SELECT absolute_path FROM textbook_files WHERE library_id=?", (library_id,)
            ).fetchall()
        }
        with self.conn:
            self.conn.execute("DELETE FROM library_page_map WHERE library_id=?", (library_id,))
            self.conn.execute(
                "UPDATE libraries SET page_offset=?, calibration_json='{}' WHERE id=?", (page_offset, library_id)
            )
            if not paths:
                return
            questions = self.conn.execute(
                "SELECT id, explanation, evidence_json FROM imported_questions "
                "WHERE evidence_json!='[]'"
            ).fetchall()
            for question in questions:
                evidence = json.loads(question["evidence_json"] or "[]")
                explanation = question["explanation"] or ""
                changed = False
                for item in evidence:
                    if item.get("source_path") not in paths:
                        continue
                    pdf_page = item.get("pdf_page", item.get("source_page"))
                    if not pdf_page:
                        continue
                    old_page = item.get("source_page")
                    printed_page = int(pdf_page) - page_offset
                    item["pdf_page"] = int(pdf_page)
                    item["source_page"] = printed_page if printed_page > 0 else None
                    if old_page and printed_page > 0 and int(old_page) != printed_page:
                        explanation = explanation.replace(
                            f"第{int(old_page)}页", f"第{printed_page}页"
                        )
                    changed = True
                if changed:
                    self.conn.execute(
                        "UPDATE imported_questions SET explanation=?, evidence_json=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (explanation, json.dumps(evidence, ensure_ascii=False), question["id"]),
                    )

    def infer_library_page_offset(self, library_id: int, force: bool = False) -> int | None:
        """Infer a stable PDF-to-printed-page difference from extracted page headers."""
        library = self.get_library(library_id)
        if not library:
            return None
        if library.get("calibration_json", "{}") != "{}":
            return json.loads(library["calibration_json"]).get("offset")
        if not force and int(library.get("page_offset", 0)) != 0:
            return int(library["page_offset"])
        rows = self.conn.execute(
            """SELECT c.page_number, c.chunk_text FROM chunks_v2 c
               WHERE c.library_id=? AND c.chunk_index=0 ORDER BY c.page_number""",
            (library_id,),
        ).fetchall()
        offsets: list[int] = []
        for row in rows:
            match = re.match(r"^\s*(\d{1,4})\s*(?:\r?\n|$)", row["chunk_text"] or "")
            if not match:
                continue
            printed_page = int(match.group(1))
            offset = int(row["page_number"]) - printed_page
            if printed_page > 0 and -500 <= offset <= 500:
                offsets.append(offset)
        if not offsets:
            return None
        offset, support = Counter(offsets).most_common(1)[0]
        # Three agreeing pages are enough; scanned textbooks normally provide hundreds.
        if support < 3:
            return None
        self.set_library_page_offset(library_id, offset)
        return offset

    def apply_library_calibration(self, library_id: int, calibration: dict, *, commit=True) -> int:
        """Apply inspected page mapping; uncertain pages have no invented printed number.

        Existing prose is preserved and marked for re-review, not globally string-replaced.
        Caller owns the transaction when commit=False (batch import).
        """
        library = self.get_library(library_id)
        if not library:
            raise ValueError("教材不存在")
        mapping = {int(k): int(v) for k, v in calibration.get("mapping", {}).items()}
        count = int(calibration["page_count"])
        if any(not 1 <= k <= count or not 1 <= v <= 20000 for k, v in mapping.items()):
            raise ValueError("页码映射超出范围")
        summary = {k: v for k, v in calibration.items() if k not in {"mapping", "anchors"}}
        summary["anchors"] = calibration.get("anchors", [])[:20]
        self.conn.execute("DELETE FROM library_page_map WHERE library_id=?", (library_id,))
        self.conn.executemany("INSERT INTO library_page_map VALUES(?,?,?)",
                              [(library_id, k, v) for k, v in mapping.items()])
        self.conn.execute("UPDATE libraries SET page_offset=?, calibration_json=? WHERE id=?",
                          (int(calibration.get("offset") or 0), json.dumps(summary, ensure_ascii=False), library_id))
        # A library may retain old indexed files after its source PDF is changed.
        # Calibration belongs only to the inspected current file, never those older sources.
        paths = {r[0] for r in self.conn.execute(
            "SELECT absolute_path FROM textbook_files WHERE library_id=? AND absolute_path=?",
            (library_id, library["root_path"]))}
        changed_count = 0
        if paths:
            for row in self.conn.execute("SELECT id, evidence_json, raw_json FROM imported_questions WHERE evidence_json!='[]'").fetchall():
                evidence, changed = json.loads(row["evidence_json"]), False
                for item in evidence:
                    # Without a physical locator, a historical printed page must not be guessed.
                    if item.get("source_path") not in paths or not item.get("pdf_page"):
                        continue
                    printed = mapping.get(int(item["pdf_page"]))
                    if item.get("source_page") != printed:
                        item["source_page"] = printed
                        changed = True
                if changed:
                    raw = json.loads(row["raw_json"])
                    from question_format_v2 import normalize_evidence
                    meta = raw.setdefault("explanationMeta", {})
                    meta["evidence"] = normalize_evidence(evidence)
                    warnings = meta.setdefault("reviewWarnings", [])
                    warning = "教材页码校准已更新，请复核解析文字中的页码引用"
                    if warning not in warnings:
                        warnings.append(warning)
                    self.conn.execute("UPDATE imported_questions SET evidence_json=?, raw_json=?, review_status='pending', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (json.dumps(evidence, ensure_ascii=False), json.dumps(raw, ensure_ascii=False), row["id"]))
                    changed_count += 1
        if commit:
            self.conn.commit()
        return changed_count

    def infer_all_library_page_offsets(self) -> None:
        rows = self.conn.execute(
            "SELECT id FROM libraries WHERE active=1 AND page_offset=0"
        ).fetchall()
        for row in rows:
            self.infer_library_page_offset(int(row["id"]))

    def list_libraries(self, active_only: bool = True) -> list[dict]:
        where = "WHERE l.active=1" if active_only else ""
        rows = self.conn.execute(
            f"""SELECT l.*, COUNT(f.id) AS file_count,
                COALESCE(SUM(f.page_count), 0) AS page_count,
                COALESCE(SUM(f.ocr_page_count), 0) AS ocr_page_count,
                MAX(f.status) AS file_status, MAX(f.error_message) AS file_error,
                MAX(f.relative_path) AS file_name,
                MAX(f.index_profile_json) AS index_profile_json,
                MAX(f.index_fingerprint) AS index_fingerprint
                FROM libraries l LEFT JOIN textbook_files f ON f.library_id=l.id AND f.status!='missing'
                {where} GROUP BY l.id ORDER BY l.subject, l.name"""
        ).fetchall()
        return [dict(row) for row in rows]

    def get_library(self, library_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()
        return dict(row) if row else None

    def deactivate_library(self, library_id: int) -> None:
        self.conn.execute("UPDATE libraries SET active=0 WHERE id=?", (library_id,))
        self.conn.commit()

    def get_file_by_path(self, absolute_path: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM textbook_files WHERE absolute_path=?", (absolute_path,)).fetchone()
        return dict(row) if row else None

    def upsert_file_metadata(
        self, library_id: int, absolute_path: str, relative_path: str, file_size: int,
        modified_ns: int, sha256: str, status: str = "indexing",
    ) -> int:
        self.conn.execute(
            """INSERT INTO textbook_files
                (library_id, absolute_path, relative_path, file_size, modified_ns, sha256, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(absolute_path) DO UPDATE SET library_id=excluded.library_id,
                    relative_path=excluded.relative_path, file_size=excluded.file_size,
                    modified_ns=excluded.modified_ns, sha256=excluded.sha256,
                    status=excluded.status, error_message='', updated_at=CURRENT_TIMESTAMP""",
            (library_id, absolute_path, relative_path, file_size, modified_ns, sha256, status),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT id FROM textbook_files WHERE absolute_path=?", (absolute_path,)).fetchone()[0])

    def create_index_stage(self, resume_key: str | None = None) -> IndexBuildStage:
        return IndexBuildStage(self.path.parent, resume_key=resume_key)

    def publish_index_stage(
        self, stage: IndexBuildStage, file_id: int, library_id: int, *,
        source: dict, profile: dict, fingerprint: str,
        revalidate_source=None, expected_dimension: Optional[int] = None,
    ) -> dict:
        """Validate and atomically replace one file's complete searchable index."""
        attached = False
        primary_error = None
        try:
            summary = stage.validate(expected_dimension)
            stage.close()
            self.conn.execute("ATTACH DATABASE ? AS index_stage", (str(stage.path),))
            attached = True
            with self.conn:
                row = self.conn.execute(
                    "SELECT id FROM textbook_files WHERE id=?", (int(file_id),)
                ).fetchone()
                if not row:
                    raise ValueError("待发布的教材文件记录不存在")
                if revalidate_source is not None:
                    current = revalidate_source()
                    identity_keys = ("absolute_path", "file_size", "modified_ns", "sha256")
                    if any(current.get(key) != source.get(key) for key in identity_keys):
                        raise ValueError("教材文件在索引期间发生变化，请重新建立索引")
                self.conn.execute("DELETE FROM textbook_pages WHERE file_id=?", (int(file_id),))
                self.conn.execute("DELETE FROM chunks_v2 WHERE file_id=?", (int(file_id),))
                self.conn.execute(
                    """INSERT INTO textbook_pages
                       (file_id, page_number, extraction_method, char_count, error_message)
                       SELECT ?, page_number, extraction_method, char_count, error_message
                       FROM index_stage.pages ORDER BY page_number""",
                    (int(file_id),),
                )
                self.conn.execute(
                    """INSERT INTO chunks_v2
                       (library_id, file_id, page_number, chunk_index, chunk_text, search_text,
                        embedding, embedding_dim, extraction_method, metadata_json)
                       SELECT ?, ?, page_number, chunk_index, chunk_text, search_text,
                              embedding, embedding_dim, extraction_method, metadata_json
                       FROM index_stage.chunks ORDER BY page_number, chunk_index""",
                    (int(library_id), int(file_id)),
                )
                status = "warning" if summary["errors"] else "ready"
                cursor = self.conn.execute(
                    """UPDATE textbook_files SET library_id=?, absolute_path=?, relative_path=?,
                       file_size=?, modified_ns=?, sha256=?, status=?, page_count=?,
                       ocr_page_count=?, index_profile_json=?, index_fingerprint=?,
                       error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (int(library_id), source["absolute_path"], source["relative_path"],
                     int(source["file_size"]), int(source["modified_ns"]), source["sha256"],
                     status, summary["page_count"], summary["ocr_page_count"],
                     json.dumps(profile, ensure_ascii=False, sort_keys=True), fingerprint,
                     "；".join(summary["errors"]), int(file_id)),
                )
                if cursor.rowcount != 1:
                    raise ValueError("待发布的教材文件记录不存在")
            return summary
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                if attached:
                    self.conn.execute("DETACH DATABASE index_stage")
            except sqlite3.Error:
                if primary_error is None:
                    raise
            finally:
                if primary_error is not None and stage.persistent:
                    stage.close()
                else:
                    stage.cleanup()

    def replace_file_content(
        self, file_id: int, library_id: int,
        pages: Iterable[dict], chunks: Iterable[dict],
    ) -> None:
        page_count = 0
        ocr_count = 0
        errors = []
        seen_errors = set()

        def page_rows():
            nonlocal page_count, ocr_count
            for item in pages:
                page_count += 1
                if item["extraction_method"] in IndexBuildStage.OCR_METHODS:
                    ocr_count += 1
                error = item.get("error_message", "")
                if error and error not in seen_errors and len(errors) < 5:
                    seen_errors.add(error)
                    errors.append(error)
                yield (
                    file_id, item["page_number"], item["extraction_method"],
                    len(item.get("text", "")), error,
                )

        def chunk_rows():
            for item in chunks:
                embedding = item.get("embedding")
                yield (
                    library_id, file_id, item["page_number"], item["chunk_index"],
                    item["chunk_text"], item["search_text"], encode_embedding(embedding),
                    len(embedding) if embedding is not None else None,
                    item.get("extraction_method", "text"),
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                )

        with self.conn:
            self.conn.execute("DELETE FROM textbook_pages WHERE file_id=?", (file_id,))
            self.conn.execute("DELETE FROM chunks_v2 WHERE file_id=?", (file_id,))
            self.conn.executemany(
                """INSERT INTO textbook_pages(file_id, page_number, extraction_method, char_count, error_message)
                   VALUES(?, ?, ?, ?, ?)""",
                page_rows(),
            )
            self.conn.executemany(
                """INSERT INTO chunks_v2
                    (library_id, file_id, page_number, chunk_index, chunk_text, search_text,
                     embedding, embedding_dim, extraction_method, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                chunk_rows(),
            )
            self.conn.execute(
                """UPDATE textbook_files SET status=?, page_count=?, ocr_page_count=?,
                   error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                ("warning" if errors else "ready", page_count, ocr_count,
                 "；".join(errors), file_id),
            )

    def mark_missing_files(self, library_id: int, seen_paths: set[str]) -> None:
        rows = self.conn.execute(
            "SELECT id, absolute_path FROM textbook_files WHERE library_id=?", (library_id,)
        ).fetchall()
        missing = [row["id"] for row in rows if row["absolute_path"] not in seen_paths]
        if missing:
            self.conn.executemany("UPDATE textbook_files SET status='missing' WHERE id=?", [(item,) for item in missing])
            self.conn.commit()

    def finish_library_scan(self, library_id: int) -> None:
        self.conn.execute("UPDATE libraries SET last_scanned_at=CURRENT_TIMESTAMP WHERE id=?", (library_id,))
        self.conn.commit()

    def file_chunk_count(self, file_id: int) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM chunks_v2 WHERE file_id=?", (file_id,)).fetchone()[0])

    def set_file_index_profile(self, file_id: int, profile: dict, fingerprint: str) -> None:
        self.conn.execute(
            "UPDATE textbook_files SET index_profile_json=?, index_fingerprint=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(profile, ensure_ascii=False, sort_keys=True), fingerprint, int(file_id)),
        )
        self.conn.commit()

    def adopt_legacy_index_profiles(self, profile: dict, fingerprint: str) -> int:
        """Claim legacy indexes only when every stored vector has the configured dimension."""
        expected = int(profile["dimension"])
        rows = self.conn.execute(
            """SELECT f.id, COUNT(c.id) AS chunks,
                      MIN(c.embedding_dim) AS min_dim, MAX(c.embedding_dim) AS max_dim
               FROM textbook_files f LEFT JOIN chunks_v2 c ON c.file_id=f.id
               WHERE f.index_fingerprint='' AND f.status IN ('ready','warning')
               GROUP BY f.id"""
        ).fetchall()
        adopted = 0
        adopted_profile = dict(profile)
        adopted_profile["adoptedLegacy"] = True
        for row in rows:
            if row["chunks"] and row["min_dim"] == expected and row["max_dim"] == expected:
                self.set_file_index_profile(int(row["id"]), adopted_profile, fingerprint)
                adopted += 1
        return adopted

    def compatible_library_files(self, subject: str, fingerprint: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT f.*, l.name AS library_name, l.subject FROM textbook_files f
               JOIN libraries l ON l.id=f.library_id
               WHERE l.active=1 AND l.subject=? AND f.status IN ('ready','warning')
                 AND f.index_fingerprint=? ORDER BY l.name""",
            (subject, fingerprint),
        ).fetchall()
        return [dict(row) for row in rows]

    def index_compatibility(self, fingerprint: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT l.id AS library_id, l.name, l.subject, f.id AS file_id, f.status,
                      f.index_fingerprint,
                      CASE WHEN f.index_fingerprint=? THEN 'compatible'
                           WHEN f.index_fingerprint='' THEN 'unknown' ELSE 'stale' END AS compatibility
               FROM libraries l LEFT JOIN textbook_files f ON f.library_id=l.id AND f.status!='missing'
               WHERE l.active=1 ORDER BY l.subject,l.name""",
            (fingerprint,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_file_error(self, file_id: int, message: str) -> None:
        self.conn.execute(
            "UPDATE textbook_files SET status='error', error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (message, file_id),
        )
        self.conn.commit()

    def search_chunks(
        self, subject: str, query: str, limit: int = 20, index_fingerprint: str = "",
        library_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return subject candidates, or search every explicitly selected textbook."""
        selected = list(dict.fromkeys(int(item) for item in (library_ids or []) if int(item) > 0))
        if library_ids is None:
            rows = self.conn.execute(
                "SELECT id FROM libraries WHERE active=1 AND subject=?", (subject,)
            ).fetchall()
        elif selected:
            placeholders = ",".join("?" for _ in selected)
            rows = self.conn.execute(
                f"SELECT id FROM libraries WHERE active=1 AND id IN ({placeholders})",
                tuple(selected),
            ).fetchall()
        else:
            rows = []
        library_ids = [int(row[0]) for row in rows]
        if not library_ids or not query.strip():
            return []
        placeholders = ",".join("?" for _ in library_ids)
        sql = f"""SELECT c.id, c.chunk_text, c.embedding, c.embedding_dim, c.metadata_json,
                   c.page_number AS pdf_page,
                   CASE WHEN l.calibration_json!='{{}}' THEN COALESCE(pm.printed_page, 0)
                        ELSE c.page_number - l.page_offset END AS textbook_page,
                   c.extraction_method, f.absolute_path AS source_path, f.relative_path AS source_file,
                   l.id AS library_id, l.name AS textbook, l.version AS textbook_version,
                   bm25(chunks_fts) AS bm25_score
                   FROM chunks_fts JOIN chunks_v2 c ON c.id=chunks_fts.rowid
                   JOIN textbook_files f ON f.id=c.file_id
                   JOIN libraries l ON l.id=c.library_id
                   LEFT JOIN library_page_map pm ON pm.library_id=c.library_id AND pm.pdf_page=c.page_number
                   WHERE chunks_fts MATCH ? AND c.library_id IN ({placeholders})
                     AND f.status IN ('ready', 'warning')
                     {"AND f.index_fingerprint=?" if index_fingerprint else ""}
                   ORDER BY bm25_score LIMIT ?"""
        params = [query, *library_ids]
        if index_fingerprint:
            params.append(index_fingerprint)
        params.append(int(limit))
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                **{key: row[key] for key in row.keys() if key not in {"embedding", "embedding_dim"}},
                "embedding": decode_embedding(row["embedding"], row["embedding_dim"])
                if row["embedding"] is not None else None,
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in rows
        ]

    def create_question_set(self, name: str, source_path: str, source_type: str, questions: list[dict]) -> str:
        set_id = str(uuid.uuid4())
        from importers import question_prompt

        with self.conn:
            self.conn.execute(
                "INSERT INTO question_sets(id, name, source_path, source_type, question_count) VALUES(?, ?, ?, ?, ?)",
                (set_id, name.strip(), str(source_path), source_type, len(questions)),
            )
            self.conn.executemany(
                """INSERT INTO imported_questions
                    (set_id, external_id, subject, question_type, prompt_text, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        set_id, str(item["id"]), str(item["subject"]), str(item.get("type") or "A1"),
                        question_prompt(item), json.dumps(item, ensure_ascii=False),
                    )
                    for item in questions
                ],
            )
            from incremental_export import capture
            capture(self, set_id)
        self.sync_question_tags(set_id)
        return set_id

    def append_questions_to_set(self, set_id: str, questions: list[dict]) -> None:
        from importers import question_prompt

        if not self.conn.execute("SELECT 1 FROM question_sets WHERE id=?", (set_id,)).fetchone():
            raise ValueError("题目集不存在")
        with self.conn:
            last_pk = self.conn.execute("SELECT COALESCE(MAX(id),0) FROM imported_questions WHERE set_id=?", (set_id,)).fetchone()[0]
            self.conn.executemany(
                """INSERT INTO imported_questions
                    (set_id, external_id, subject, question_type, prompt_text, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        set_id, str(item["id"]), str(item["subject"]), str(item.get("type") or "A1"),
                        question_prompt(item), json.dumps(item, ensure_ascii=False),
                    )
                    for item in questions
                ],
            )
            self.conn.execute(
                "UPDATE question_sets SET question_count=(SELECT COUNT(*) FROM imported_questions WHERE set_id=?) WHERE id=?",
                (set_id, set_id),
            )
            from incremental_export import capture
            capture(self, set_id, last_pk)
        self.sync_question_tags(set_id)

    def list_question_sets(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.*, SUM(CASE WHEN q.review_status='approved' THEN 1 ELSE 0 END) AS approved_count,
               SUM(CASE WHEN q.pipeline_status='unmatched' THEN 1 ELSE 0 END) AS unmatched_count
               FROM question_sets s LEFT JOIN imported_questions q ON q.set_id=s.id
               GROUP BY s.id ORDER BY s.created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def list_imported_questions(
        self, set_id: str, review_status: str = "", pipeline_status: str = "",
        search: str = "", limit: int = 500, offset: int = 0,
    ) -> list[dict]:
        clauses = ["set_id=?"]
        params: list[object] = [set_id]
        if review_status:
            clauses.append("review_status=?")
            params.append(review_status)
        if pipeline_status:
            if pipeline_status == "matched_generation_error":
                clauses.append("retrieval_status='matched' AND generation_status IN ('validation_error','provider_error')")
            else:
                clauses.append("pipeline_status=?")
                params.append(pipeline_status)
        if search:
            clauses.append("(external_id LIKE ? OR prompt_text LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        params.extend([int(limit), int(offset)])
        rows = self.conn.execute(
            f"""SELECT id, external_id, subject, question_type, pipeline_status,
                retrieval_status, generation_status, match_confidence, review_status,
                generation_mode, match_score, explanation, error_message, viewed_at, updated_at
                ,COALESCE(json_extract(raw_json, '$.explanationMeta.evidenceGrade'),
                    CASE generation_mode WHEN 'textbook' THEN 'A' WHEN 'textbook_reasoning' THEN 'B'
                    WHEN 'general_knowledge' THEN 'C' ELSE '' END) AS evidence_grade
                FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_question_ids_by_filter(
        self, set_id: str, review_status: str = "", pipeline_status: str = "", search: str = "",
    ) -> list[int]:
        """Return lightweight IDs for a cross-page bulk selection without loading question payloads."""
        clauses = ["set_id=?"]
        params: list[object] = [set_id]
        if review_status:
            clauses.append("review_status=?"); params.append(review_status)
        if pipeline_status:
            if pipeline_status == "matched_generation_error":
                clauses.append("retrieval_status='matched' AND generation_status IN ('validation_error','provider_error')")
            else:
                clauses.append("pipeline_status=?"); params.append(pipeline_status)
        if search:
            clauses.append("(external_id LIKE ? OR prompt_text LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        rows = self.conn.execute(
            f"SELECT id FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY id", params,
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _question_global_filter(
        self, set_id: str = "", review_status: str = "", pipeline_status: str = "",
        subject: str = "", tag: str = "", question_source: str = "", search: str = "",
    ) -> tuple[str, list[object]]:
        """Shared WHERE clause for cross-set question browsing (questions domain)."""
        clauses: list[str] = []
        params: list[object] = []
        if set_id:
            clauses.append("set_id=?")
            params.append(set_id)
        if review_status:
            clauses.append("review_status=?")
            params.append(review_status)
        if pipeline_status:
            if pipeline_status == "matched_generation_error":
                clauses.append("retrieval_status='matched' AND generation_status IN ('validation_error','provider_error')")
            else:
                clauses.append("pipeline_status=?")
                params.append(pipeline_status)
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if tag:
            # 低频标签被 sync_question_tags 降级到 suggestedTags，全局浏览需两处都查
            clauses.append(
                "(EXISTS (SELECT 1 FROM json_each(json_extract(raw_json,'$.tags')) je WHERE je.value=?)"
                " OR EXISTS (SELECT 1 FROM json_each(json_extract(raw_json,'$.suggestedTags')) je WHERE je.value=?))")
            params.extend([tag, tag])
        if question_source:
            clauses.append("json_extract(raw_json,'$.questionSource')=?")
            params.append(question_source)
        if search:
            clauses.append("(external_id LIKE ? OR prompt_text LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_questions_global(
        self, set_id: str = "", review_status: str = "", pipeline_status: str = "",
        subject: str = "", tag: str = "", question_source: str = "", search: str = "",
        limit: int = 200, offset: int = 0,
    ) -> list[dict]:
        where, params = self._question_global_filter(
            set_id, review_status, pipeline_status, subject, tag, question_source, search)
        params.extend([int(limit), int(offset)])
        rows = self.conn.execute(
            f"""SELECT id, set_id, external_id, subject, question_type, prompt_text, pipeline_status,
                retrieval_status, generation_status, match_confidence, review_status,
                generation_mode, match_score, error_message, updated_at,
                COALESCE(json_extract(raw_json,'$.questionSource'),'') AS question_source,
                COALESCE(json_extract(raw_json,'$.tags'),'[]') AS tags_json,
                COALESCE(json_extract(raw_json,'$.suggestedTags'),'[]') AS suggested_tags_json,
                COALESCE(json_extract(raw_json,'$.explanationMeta.evidenceGrade'),
                    CASE generation_mode WHEN 'textbook' THEN 'A' WHEN 'textbook_reasoning' THEN 'B'
                    WHEN 'general_knowledge' THEN 'C' ELSE '' END) AS evidence_grade
                FROM imported_questions {where} ORDER BY id LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            tags = json.loads(item.pop("tags_json") or "[]")
            suggested = json.loads(item.pop("suggested_tags_json") or "[]")
            item["tags"] = [*tags, *[label for label in suggested if label not in tags]]
            result.append(item)
        return result

    def count_questions_global(
        self, set_id: str = "", review_status: str = "", pipeline_status: str = "",
        subject: str = "", tag: str = "", question_source: str = "", search: str = "",
    ) -> int:
        where, params = self._question_global_filter(
            set_id, review_status, pipeline_status, subject, tag, question_source, search)
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM imported_questions {where}", params).fetchone()[0])

    def list_question_ids_global(
        self, set_id: str = "", review_status: str = "", pipeline_status: str = "",
        subject: str = "", tag: str = "", question_source: str = "", search: str = "",
    ) -> list[int]:
        """Lightweight IDs for cross-page bulk selection without loading payloads."""
        where, params = self._question_global_filter(
            set_id, review_status, pipeline_status, subject, tag, question_source, search)
        rows = self.conn.execute(
            f"SELECT id FROM imported_questions {where} ORDER BY id", params).fetchall()
        return [int(row[0]) for row in rows]

    def all_question_subjects(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT subject FROM imported_questions WHERE COALESCE(subject,'')<>'' ORDER BY subject"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def all_question_sources(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT json_extract(raw_json,'$.questionSource') FROM imported_questions"
            " WHERE COALESCE(json_extract(raw_json,'$.questionSource'),'')<>'' ORDER BY 1"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def all_tag_labels(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT label, SUM(usage_count) AS total FROM question_tag_catalog"
            " WHERE status='active' GROUP BY normalized_name ORDER BY total DESC, label"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def count_trash(self, search: str = "") -> int:
        params: list[object] = []
        where = ""
        if search:
            where = "WHERE t.external_id LIKE ? OR t.subject LIKE ? OR q.name LIKE ?"
            params.extend([f"%{search}%"] * 3)
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM question_trash t LEFT JOIN question_sets q ON q.id=t.set_id {where}",
            params,
        ).fetchone()[0])

    def get_imported_question(self, question_pk: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM imported_questions WHERE id=?", (question_pk,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["raw"] = json.loads(value["raw_json"])
        value["evidence"] = json.loads(value["evidence_json"] or "[]")
        return value

    def queued_questions(self, set_id: str, question_ids: Optional[list[int]] = None) -> list[dict]:
        params: list[object] = [set_id]
        id_filter = ""
        if question_ids:
            id_filter = f" AND id IN ({','.join('?' for _ in question_ids)})"
            params.extend(int(item) for item in question_ids)
        rows = self.conn.execute(
            f"""SELECT id, external_id, subject, prompt_text, raw_json,match_score,evidence_json,
                       retrieval_status,generation_status,match_confidence
                FROM imported_questions
               WHERE set_id=? AND pipeline_status IN ('queued', 'error'){id_filter} ORDER BY id""", params
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["raw"] = json.loads(item.pop("raw_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["raw"] = {}
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["evidence"] = []
            result.append(item)
        return result

    def matched_generation_failures(self, set_id: str) -> list[dict]:
        """Return failures that can retry generation from already persisted evidence."""
        rows = self.conn.execute(
            """SELECT id,external_id,subject,match_score,match_confidence,error_message
               FROM imported_questions
               WHERE set_id=? AND review_status='pending' AND retrieval_status='matched'
                 AND generation_status IN ('validation_error','provider_error')
                 AND evidence_json NOT IN ('','[]')
               ORDER BY id""",
            (set_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def general_generation_questions(
        self, set_id: str, question_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Return pending unmatched questions eligible for user-authorized general generation."""
        clauses = ["set_id=?", "pipeline_status IN ('unmatched','no_library')", "review_status='pending'"]
        params: list[object] = [set_id]
        if question_ids is not None:
            selected = list(dict.fromkeys(int(item) for item in question_ids if int(item) > 0))
            if not selected:
                return []
            clauses.append(f"id IN ({','.join('?' for _ in selected)})")
            params.extend(selected)
        rows = self.conn.execute(
            f"""SELECT id,external_id,set_id,subject,prompt_text,pipeline_status,
                       match_score,evidence_json,raw_json
                FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY id""",
            params,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
            item["raw"] = json.loads(item.pop("raw_json") or "{}")
            result.append(item)
        return result

    def list_missing_tag_questions(
        self, set_id: str, include_approved: bool = True,
    ) -> list[dict]:
        """List generated questions without tags and classify safe tag-only repairs."""
        clauses = ["set_id=?", "pipeline_status='generated'"]
        params: list[object] = [set_id]
        if not include_approved:
            clauses.append("review_status='pending'")
        rows = self.conn.execute(
            f"""SELECT id,external_id,set_id,subject,prompt_text,pipeline_status,review_status,
                       generation_mode,match_score,explanation,evidence_json,raw_json
                FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY id""",
            params,
        ).fetchall()
        result = []
        for source in rows:
            item = dict(source)
            try:
                raw = json.loads(item.pop("raw_json") or "{}")
                evidence = json.loads(item.pop("evidence_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if [value for value in [*(raw.get("tags") or []), *(raw.get("suggestedTags") or [])] if str(value).strip()]:
                continue
            blocks = raw.get("explanationBlocks") if isinstance(raw.get("explanationBlocks"), list) else []
            text = str(item.get("explanation") or "").strip()
            sections = {
                str(block.get("section") or "") for block in blocks if isinstance(block, dict)
            }
            has_pitfalls_table = any(
                isinstance(block, dict) and block.get("section") == "pitfalls" and block.get("type") == "table"
                for block in blocks
            )
            mode = str(item.get("generation_mode") or "")
            if not text or "待人工整理" in text or "模型输出无法解析" in text:
                reason = "解析正文无效"
            elif (text.startswith("{") or text.startswith("```json")) and '"explanationBlocks"' in text:
                reason = "解析仍是原始 JSON"
            elif not blocks:
                reason = "缺少结构化解析"
            elif mode == "general_knowledge" and not {"answerBasis", "pitfalls"}.issubset(sections):
                reason = "通识解析结构不完整"
            elif mode != "general_knowledge" and not {"analysis", "answerBasis", "pitfalls"}.issubset(sections):
                reason = "教材解析结构不完整"
            elif not has_pitfalls_table:
                reason = "易错点尚未整理为表格"
            elif mode != "general_knowledge" and not evidence:
                reason = "缺少教材证据"
            else:
                reason = ""
            item.update({"raw": raw, "evidence": evidence, "repairable": not reason, "repair_reason": reason})
            result.append(item)
        return result

    def list_missing_study_point_questions(
        self, set_id: str, include_approved: bool = True,
    ) -> list[dict]:
        """List generated questions without usable study points."""
        clauses = ["set_id=?", "pipeline_status='generated'"]
        params: list[object] = [set_id]
        if not include_approved:
            clauses.append("review_status='pending'")
        rows = self.conn.execute(
            f"""SELECT id,external_id,set_id,subject,prompt_text,review_status,explanation,raw_json
                FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY id""",
            params,
        ).fetchall()
        result = []
        for source in rows:
            item = dict(source)
            try:
                raw = json.loads(item.pop("raw_json") or "{}")
            except (TypeError, ValueError):
                continue
            points = raw.get("studyPoints")
            if isinstance(points, list) and any(
                isinstance(point, dict)
                and str(point.get("title") or "").strip()
                and str(point.get("body") or "").strip()
                for point in points
            ):
                continue
            item["raw"] = raw
            result.append(item)
        return result

    def list_missing_memory_card_questions(
        self, set_id: str, include_approved: bool = True,
    ) -> list[dict]:
        """List generated questions that still lack the separately generated memory-card artifact."""
        clauses = ["set_id=?", "pipeline_status='generated'"]
        params: list[object] = [set_id]
        if not include_approved:
            clauses.append("review_status='pending'")
        rows = self.conn.execute(
            f"""SELECT id,external_id,set_id,subject,prompt_text,review_status,explanation,raw_json
                FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY id""",
            params,
        ).fetchall()
        result = []
        for source in rows:
            item = dict(source)
            try:
                raw = json.loads(item.pop("raw_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            cards = raw.get("memoryCards")
            if isinstance(cards, list) and any(
                isinstance(card, dict)
                and str(card.get("title") or "").strip()
                and str(card.get("content") or "").strip()
                for card in cards
            ):
                continue
            item["raw"] = raw
            result.append(item)
        return result

    def abnormal_generation_questions(self, set_id: str) -> list[dict]:
        """Return pending structural failures that are safe to queue for regeneration."""
        rows = self.conn.execute(
            """SELECT id,external_id,subject,explanation,error_message,raw_json
               FROM imported_questions
               WHERE set_id=? AND review_status='pending' AND pipeline_status IN ('unmatched','error')
               ORDER BY id""",
            (set_id,),
        ).fetchall()
        result = []
        for source in rows:
            row = dict(source)
            try:
                raw = json.loads(row.pop("raw_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
            text = str(row.get("explanation") or "").strip()
            error = str(row.get("error_message") or "")
            meta = raw.get("explanationMeta") if isinstance(raw.get("explanationMeta"), dict) else {}
            structural = bool(
                meta.get("reviewWarnings")
                or "结构" in error or "模型输出" in error or "格式修复" in error
                or "待人工整理" in text or "模型输出无法解析" in text
                or ((text.startswith("{") or text.startswith("```json")) and '"explanationBlocks"' in text)
            )
            if structural:
                row["raw"] = raw
                result.append(row)
        return result

    def reconcile_high_score_unmatched(self, threshold: float) -> int:
        """Move matched-but-invalid legacy rows out of the retrieval-unmatched queue."""
        threshold = min(0.99, max(0.0, float(threshold)))
        rows = self.conn.execute(
            """SELECT id,match_score,evidence_json,error_message FROM imported_questions
               WHERE pipeline_status='unmatched' AND match_score>=? AND evidence_json NOT IN ('','[]')""",
            (threshold,),
        ).fetchall()
        changed: list[tuple[str, int]] = []
        for row in rows:
            error = str(row["error_message"] or "")
            if "相似度未达到阈值" in error:
                continue
            try:
                evidence = json.loads(row["evidence_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = []
            if not evidence:
                continue
            confidence = self._confidence_from_evidence(
                float(row["match_score"] or 0), evidence, matched=True,
            )
            changed.append((confidence, int(row["id"])))
        if not changed:
            return 0
        with self.conn:
            self.conn.executemany(
                """UPDATE imported_questions SET pipeline_status='error',retrieval_status='matched',
                   generation_status='validation_error',match_confidence=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                changed,
            )
        return len(changed)

    def reclassify_below_threshold_generated_questions(
        self, threshold: float, set_id: str = "",
    ) -> int:
        """Remove pending textbook output below the current threshold from the review queue."""
        threshold = min(1.0, max(0.0, float(threshold)))
        clauses = [
            "pipeline_status='generated'", "review_status='pending'",
            "generation_mode!='general_knowledge'", "match_score<?",
        ]
        params: list[object] = [threshold]
        if set_id:
            clauses.append("set_id=?")
            params.append(set_id)
        with self.conn:
            cursor = self.conn.execute(
                f"""UPDATE imported_questions
                       SET pipeline_status='unmatched',retrieval_status='unmatched',
                           generation_status='pending',match_confidence='low',generation_mode='',
                           error_message='相似度未达到当前匹配阈值，已移出待审核',
                           updated_at=CURRENT_TIMESTAMP
                       WHERE {' AND '.join(clauses)}""",
                params,
            )
        return max(0, int(cursor.rowcount or 0))

    def apply_tag_backfill(
        self, question_id: int, tags: object, brief_explanation: str | None = None,
        *, sync_catalog: bool = True,
    ) -> dict:
        """Patch only tags and a missing brief; workflow and approved content stay untouched."""
        row = self.conn.execute(
            "SELECT set_id,subject,raw_json FROM imported_questions WHERE id=?", (int(question_id),)
        ).fetchone()
        if not row:
            raise ValueError("题目不存在")
        from question_format_v2 import normalize_tags
        normalized_tags = normalize_tags(tags)
        if not normalized_tags:
            raise ValueError("题目标签不能为空")
        raw = json.loads(row["raw_json"] or "{}")
        raw["tags"] = normalized_tags[:3]
        raw["suggestedTags"] = []
        if not str(raw.get("briefExplanation") or "").strip() and brief_explanation:
            raw["briefExplanation"] = re.sub(r"\s+", " ", str(brief_explanation)).strip()[:160]
        with self.conn:
            self.conn.execute(
                "UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(raw, ensure_ascii=False), int(question_id)),
            )
        if sync_catalog:
            self.sync_question_tags(row["set_id"], row["subject"])
        return raw

    def apply_study_point_backfill(self, question_id: int, study_points: object) -> dict:
        """Patch only studyPoints; workflow and approved content stay untouched."""
        row = self.conn.execute(
            "SELECT raw_json FROM imported_questions WHERE id=?", (int(question_id),)
        ).fetchone()
        if not row:
            raise ValueError("题目不存在")
        from question_format_v2 import normalize_study_points
        normalized = normalize_study_points(study_points)
        if not normalized:
            raise ValueError("考点不能为空")
        raw = json.loads(row["raw_json"] or "{}")
        raw["studyPoints"] = normalized
        with self.conn:
            self.conn.execute(
                "UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(raw, ensure_ascii=False), int(question_id)),
            )
        return raw

    def apply_memory_card_backfill(
        self, question_id: int, memory_cards: object, *, generator: str = "analysis-tool",
    ) -> dict:
        """Patch only independent memory cards; legacy tags and explanations stay untouched."""
        row = self.conn.execute(
            "SELECT raw_json FROM imported_questions WHERE id=?", (int(question_id),)
        ).fetchone()
        if not row:
            raise ValueError("题目不存在")
        from question_format_v2 import normalize_memory_cards
        raw = json.loads(row["raw_json"] or "{}")
        normalized = normalize_memory_cards(memory_cards, raw)
        if not normalized:
            raise ValueError("独立背诵知识卡为空，或与旧标签/解析完全重复")
        raw["memoryCards"] = normalized
        raw["memoryCardMeta"] = {
            "schemaVersion": 1,
            "generator": str(generator or "analysis-tool").strip()[:80],
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        with self.conn:
            self.conn.execute(
                "UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(raw, ensure_ascii=False), int(question_id)),
            )
        return raw

    def reclassify_invalid_generated_questions(self, set_id: str = "") -> int:
        """Move legacy placeholder/JSON-dump results out of the normal generated review queue."""
        params: list[object] = []
        where = "pipeline_status='generated' AND review_status='pending'"
        if set_id:
            where += " AND set_id=?"
            params.append(set_id)
        rows = self.conn.execute(
            f"""SELECT id,explanation,raw_json,generation_mode,evidence_json
                FROM imported_questions WHERE {where}""", params,
        ).fetchall()
        invalid_rows: list[tuple[int, str, str]] = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
            meta = raw.get("explanationMeta") if isinstance(raw.get("explanationMeta"), dict) else {}
            text = str(row["explanation"] or "").strip()
            try:
                evidence = json.loads(row["evidence_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = []
            # Textbook-mode output is reviewable only when it has an actual source excerpt.
            # Authorized general-knowledge output intentionally has no textbook evidence.
            is_textbook_mode = str(row["generation_mode"] or "") != "general_knowledge"
            missing_textbook_source = is_textbook_mode and not evidence
            tags = [*(raw.get("tags") or []), *(raw.get("suggestedTags") or [])]
            blocks = raw.get("explanationBlocks") if isinstance(raw.get("explanationBlocks"), list) else []
            incomplete_sections = is_textbook_mode and bool(blocks) and not {"analysis", "answerBasis", "pitfalls"}.issubset({
                str(item.get("section") or "") for item in blocks if isinstance(item, dict)
            })
            invalid_pitfalls = bool(blocks) and not any(
                isinstance(item, dict) and item.get("section") == "pitfalls" and item.get("type") == "table"
                for item in blocks
            )
            invalid = bool(
                missing_textbook_source or meta.get("reviewWarnings")
                or not tags or incomplete_sections or invalid_pitfalls
                or not text or "待人工整理" in text or "模型输出无法解析" in text
                or (text.startswith("{") and '"explanationBlocks"' in text)
                or (text.startswith("```json") and '"explanationBlocks"' in text)
            )
            if invalid:
                is_placeholder = bool(
                    "待人工整理" in text or "模型输出无法解析" in text
                    or ((text.startswith("{") or text.startswith("```json")) and '"explanationBlocks"' in text)
                )
                if is_placeholder:
                    raw.pop("explanationBlocks", None)
                    raw.pop("briefExplanation", None)
                    raw.pop("explanationMeta", None)
                    text = ""
                invalid_rows.append((int(row["id"]), json.dumps(raw, ensure_ascii=False), text))
        if not invalid_rows:
            return 0
        with self.conn:
            for question_id, raw_json, explanation in invalid_rows:
                evidence_row = self.conn.execute(
                    "SELECT evidence_json,match_score FROM imported_questions WHERE id=?", (question_id,)
                ).fetchone()
                evidence = json.loads(evidence_row["evidence_json"] or "[]") if evidence_row else []
                retrieval = "matched" if evidence else "error"
                confidence = self._confidence_from_evidence(
                    float(evidence_row["match_score"] or 0) if evidence_row else 0,
                    evidence, matched=bool(evidence),
                ) if evidence else "error"
                self.conn.execute(
                    """UPDATE imported_questions SET pipeline_status='error',generation_mode='',
                       retrieval_status=?,generation_status='validation_error',match_confidence=?,
                       explanation=?,raw_json=?,error_message='解析结构异常或缺少可用原文，请重新生成',
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (retrieval, confidence, explanation, raw_json, question_id),
                )
        return len(invalid_rows)

    def save_generated_result(
        self, question_pk: int, status: str, mode: str, score: float,
        explanation: str, evidence: list[dict], error_message: str = "",
        package: Optional[dict] = None, *, write_tags: bool = True,
    ) -> None:
        row = self.conn.execute(
            "SELECT set_id,subject,raw_json FROM imported_questions WHERE id=?", (question_pk,)
        ).fetchone()
        if not row:
            raise ValueError("题目不存在")
        raw = json.loads(row["raw_json"])
        if package:
            from question_format_v2 import normalize_question_v2
            patch = {
                "explanationBlocks": package.get("explanationBlocks", []),
                "briefExplanation": package.get("briefExplanation", ""),
                "knowledgePoints": package.get("knowledgePoints", []),
                "tags": package.get("tags", []),
                "suggestedTags": package.get("suggestedTags", []),
                "explanationMeta": package.get("explanationMeta", {}),
                "extensions": package.get("extensions", raw.get("extensions", {})),
            }
            if not write_tags:
                patch.pop("tags"); patch.pop("suggestedTags")
            raw.update(patch)
            raw = normalize_question_v2(raw)
            explanation = raw["explanation"]
        if status == "generated":
            generation_status = "generated"
            retrieval_status = "matched" if mode != "general_knowledge" else (
                "unmatched" if evidence else "no_library"
            )
        elif status == "unmatched":
            retrieval_status, generation_status = "unmatched", "pending"
        elif status == "no_library":
            retrieval_status, generation_status = "no_library", "pending"
        elif status == "error" and evidence:
            retrieval_status = "matched"
            generation_status = "validation_error" if self._validation_failure(error_message) else "provider_error"
        else:
            retrieval_status, generation_status = "error", "pending"
        confidence = self._confidence_from_evidence(
            score, evidence, matched=retrieval_status == "matched",
        ) if retrieval_status not in {"pending", "no_library", "error"} else retrieval_status
        with self.conn:
            self.conn.execute(
                """UPDATE imported_questions SET pipeline_status=?,retrieval_status=?,generation_status=?,
                   match_confidence=?,generation_mode=?,match_score=?,
                   explanation=?, evidence_json=?, raw_json=?, error_message=?, review_status='pending',
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, retrieval_status, generation_status, confidence, mode, float(score),
                 explanation, json.dumps(evidence, ensure_ascii=False),
                 json.dumps(raw, ensure_ascii=False), error_message, question_pk),
            )
        if package and write_tags:
            self.sync_question_tags(row["set_id"], row["subject"])

    def set_question_extension(self, question_pk: int, key: str, value: object) -> None:
        """Idempotently persist a non-core v2 trace without changing evidence schema."""
        row = self.conn.execute(
            "SELECT raw_json FROM imported_questions WHERE id=?", (int(question_pk),)
        ).fetchone()
        if not row:
            return
        raw = json.loads(row["raw_json"] or "{}")
        extensions = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
        extensions[str(key)] = value
        raw["extensions"] = extensions
        self.conn.execute(
            "UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(raw, ensure_ascii=False), int(question_pk)),
        )
        self.conn.commit()

    def save_generation_failure(
        self, question_pk: int, status: str, error_message: str, *,
        score: float | None = None, evidence: list[dict] | None = None,
        attempt_meta: dict | None = None,
    ) -> None:
        """Record a failed attempt without overwriting any previously valid explanation."""
        if status not in {"error", "unmatched", "no_library"}:
            raise ValueError("无效的失败状态")
        fields = ["pipeline_status=?", "error_message=?", "updated_at=CURRENT_TIMESTAMP"]
        params: list[object] = [status, str(error_message or "解析生成失败")[:2000]]
        available_evidence = evidence
        if available_evidence is None:
            row = self.conn.execute(
                "SELECT evidence_json FROM imported_questions WHERE id=?", (int(question_pk),)
            ).fetchone()
            try:
                available_evidence = json.loads(row["evidence_json"] or "[]") if row else []
            except (TypeError, ValueError, json.JSONDecodeError):
                available_evidence = []
        if status == "unmatched":
            retrieval_status, generation_status = "unmatched", "pending"
        elif status == "no_library":
            retrieval_status, generation_status = "no_library", "pending"
        elif available_evidence:
            retrieval_status = "matched"
            generation_status = "validation_error" if self._validation_failure(error_message) else "provider_error"
        else:
            retrieval_status, generation_status = "error", "pending"
        confidence = self._confidence_from_evidence(
            float(score or 0), available_evidence or [], matched=retrieval_status == "matched",
        ) if retrieval_status not in {"pending", "no_library", "error"} else retrieval_status
        fields.extend(["retrieval_status=?", "generation_status=?", "match_confidence=?"])
        params.extend([retrieval_status, generation_status, confidence])
        if score is not None:
            fields.append("match_score=?")
            params.append(float(score))
        if evidence is not None:
            fields.append("evidence_json=?")
            params.append(json.dumps(evidence, ensure_ascii=False))
        if attempt_meta:
            row = self.conn.execute(
                "SELECT raw_json FROM imported_questions WHERE id=?", (int(question_pk),)
            ).fetchone()
            try:
                raw = json.loads(row["raw_json"] or "{}") if row else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
            meta = raw.get("explanationMeta") if isinstance(raw.get("explanationMeta"), dict) else {}
            raw["explanationMeta"] = {**meta, "lastGenerationFailure": attempt_meta}
            fields.append("raw_json=?")
            params.append(json.dumps(raw, ensure_ascii=False))
        params.append(int(question_pk))
        with self.conn:
            self.conn.execute(
                f"UPDATE imported_questions SET {','.join(fields)} WHERE id=?", params,
            )

    def queue_question(self, question_pk: int) -> None:
        self.conn.execute(
            """UPDATE imported_questions SET pipeline_status='queued',retrieval_status='pending',
               generation_status='pending',match_confidence='pending',review_status='pending',
               error_message='', updated_at=CURRENT_TIMESTAMP WHERE id=?""", (question_pk,)
        )
        self.conn.commit()

    def update_question_subject(self, question_pk: int, subject: str) -> None:
        subject = subject.strip()
        if not subject:
            raise ValueError("学科不能为空")
        raw_row = self.conn.execute("SELECT raw_json FROM imported_questions WHERE id=?", (question_pk,)).fetchone()
        if not raw_row:
            raise ValueError("题目不存在")
        raw = json.loads(raw_row["raw_json"])
        raw["subject"] = subject
        self.conn.execute(
            """UPDATE imported_questions SET subject=?,raw_json=?,pipeline_status='queued',
               retrieval_status='pending',generation_status='pending',match_confidence='pending',
               error_message='',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (subject, json.dumps(raw, ensure_ascii=False), question_pk),
        )
        self.conn.commit()

    def update_question_knowledge_point(self, question_pk: int, knowledge_point: str) -> None:
        """Update the website-compatible knowledgePoint field without changing pipeline state."""
        row = self.conn.execute(
            "SELECT raw_json FROM imported_questions WHERE id=?", (question_pk,)
        ).fetchone()
        if not row:
            raise ValueError("题目不存在")
        raw = json.loads(row["raw_json"])
        points = []
        for item in re.split(r"[|,，;；、/]+", str(knowledge_point or "")):
            tag = re.sub(r"\s+", " ", item).strip()[:60]
            if len(tag) >= 2 and tag not in points:
                points.append(tag)
        if len(points) > 3:
            raise ValueError("每题最多设置 3 条知识点")
        raw["knowledgePoints"] = points
        raw["knowledgePoint"] = "、".join(points)
        self.conn.execute(
            "UPDATE imported_questions SET raw_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(raw, ensure_ascii=False), question_pk),
        )
        self.conn.commit()

    def update_question_exchange_fields(
        self, question_pk: int, *, explanation_blocks: list[dict], knowledge_points: object,
        tags: object, suggested_tags: object, mnemonic: str, brief_explanation: str = "",
    ) -> dict:
        """Persist the v2 review fields while leaving workflow state unchanged."""
        row = self.conn.execute("SELECT set_id,subject,raw_json FROM imported_questions WHERE id=?", (int(question_pk),)).fetchone()
        if not row: raise ValueError("题目不存在")
        from question_format_v2 import normalize_question_v2
        raw = json.loads(row["raw_json"])
        raw.update({"explanationBlocks":explanation_blocks,"briefExplanation":brief_explanation,
                    "knowledgePoints":knowledge_points,"tags":tags,
                    "suggestedTags":suggested_tags,"mnemonic":mnemonic})
        normalized = normalize_question_v2(raw)
        with self.conn:
            self.conn.execute("UPDATE imported_questions SET raw_json=?,explanation=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(normalized, ensure_ascii=False), normalized["explanation"], int(question_pk)))
        self.sync_question_tags(row["set_id"], row["subject"])
        return normalized

    @staticmethod
    def normalize_tag_name(value: str) -> str:
        return re.sub(r"[\s·._-]+", "", unicodedata.normalize("NFKC", str(value or "")).strip().casefold())

    def sync_question_tags(self, set_id: str, subject: str = "") -> int:
        """Promote repeated candidates and keep a set/subject scoped catalog."""
        clauses = ["set_id=?"]
        params: list[object] = [set_id]
        if subject:
            clauses.append("subject=?"); params.append(subject)
        rows = self.conn.execute(
            f"SELECT id,subject,raw_json FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY id", params
        ).fetchall()
        by_subject: dict[str, list[tuple[sqlite3.Row, dict]]] = {}
        for row in rows:
            by_subject.setdefault(row["subject"], []).append((row, json.loads(row["raw_json"])))
        changed = 0
        with self.conn:
            for subject_name, items in by_subject.items():
                counts: Counter[str] = Counter()
                labels: dict[str, str] = {}
                for _, raw in items:
                    labels_on_question: dict[str, str] = {}
                    for label in [*(raw.get("tags") or []), *(raw.get("suggestedTags") or [])]:
                        key = self.normalize_tag_name(label)
                        if key:
                            labels_on_question.setdefault(key, str(label).strip()[:24])
                    for key, label in labels_on_question.items():
                        counts[key] += 1; labels.setdefault(key, label)
                existing = {
                    row["normalized_name"]: dict(row) for row in self.conn.execute(
                        "SELECT * FROM question_tag_catalog WHERE set_id=? AND subject=?", (set_id, subject_name)
                    ).fetchall()
                }
                protected_targets = {
                    self.normalize_tag_name(item.get("merged_into", ""))
                    for item in existing.values() if item.get("status") == "merged" and item.get("merged_into")
                }
                active_keys = {key for key, count in counts.items() if count >= 3} | protected_targets
                for row, raw in items:
                    original = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                    tags = []
                    candidates = []
                    combined = [*(raw.get("tags") or []), *(raw.get("suggestedTags") or [])]
                    seen: set[str] = set()
                    for label in combined:
                        key = self.normalize_tag_name(label)
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        if key in active_keys:
                            if len(tags) < 3: tags.append(labels.get(key, str(label).strip()[:24]))
                        elif len(candidates) < 3:
                            candidates.append(labels.get(key, str(label).strip()[:24]))
                    raw["tags"] = tags; raw["suggestedTags"] = [item for item in candidates if item not in tags]
                    if json.dumps(raw, ensure_ascii=False, sort_keys=True) != original:
                        self.conn.execute("UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(raw, ensure_ascii=False), row["id"])); changed += 1
                live_keys = set(counts)
                for key, count in counts.items():
                    current = existing.get(key, {})
                    status = "merged" if current.get("status") == "merged" else "active" if key in active_keys else "candidate"
                    self.conn.execute(
                        """INSERT INTO question_tag_catalog
                           (set_id,subject,normalized_name,label,aliases_json,status,usage_count,merged_into,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                           ON CONFLICT(set_id,subject,normalized_name) DO UPDATE SET
                           label=CASE WHEN question_tag_catalog.status='merged' THEN question_tag_catalog.label ELSE excluded.label END,
                           status=CASE WHEN question_tag_catalog.status='merged' THEN question_tag_catalog.status ELSE excluded.status END,
                           usage_count=excluded.usage_count,updated_at=CURRENT_TIMESTAMP""",
                        (set_id, subject_name, key, labels[key], current.get("aliases_json", "[]"), status, count, current.get("merged_into", "")),
                    )
                for key, item in existing.items():
                    if key not in live_keys:
                        status = "merged" if item["status"] == "merged" else "candidate"
                        self.conn.execute("UPDATE question_tag_catalog SET usage_count=0,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, item["id"]))
        return changed

    def get_question_tag(self, tag_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM question_tag_catalog WHERE id=?", (int(tag_id),)).fetchone()
        return dict(row) if row else None

    def list_question_tags(self, set_id: str, subject: str = "", status: str = "") -> list[dict]:
        self.sync_question_tags(set_id, subject)
        clauses = ["set_id=?"]; params: list[object] = [set_id]
        if subject: clauses.append("subject=?"); params.append(subject)
        if status: clauses.append("status=?"); params.append(status)
        rows = self.conn.execute(f"SELECT * FROM question_tag_catalog WHERE {' AND '.join(clauses)} ORDER BY subject,status,label", params).fetchall()
        result = []
        for row in rows:
            item = dict(row); item["aliases"] = json.loads(item.pop("aliases_json") or "[]"); result.append(item)
        return result

    def update_question_tag(self, tag_id: int, label: str = "", status: str = "") -> dict:
        row = self.conn.execute("SELECT * FROM question_tag_catalog WHERE id=?", (int(tag_id),)).fetchone()
        if not row: raise ValueError("标签不存在")
        next_label = re.sub(r"\s+", " ", label or row["label"]).strip()[:24]
        next_status = status or row["status"]
        if len(next_label) < 2 or next_status not in {"active", "candidate"}: raise ValueError("标签名称或状态无效")
        if next_status == "active" and int(row["usage_count"] or 0) < 3:
            raise ValueError("正式标签至少需要关联同题目集、同学科的 3 道题")
        source_key = row["normalized_name"]
        items = self.conn.execute("SELECT id,raw_json FROM imported_questions WHERE set_id=? AND subject=?", (row["set_id"], row["subject"])).fetchall()
        with self.conn:
            for item in items:
                raw = json.loads(item["raw_json"])
                tags = [next_label if self.normalize_tag_name(value) == source_key else value for value in raw.get("tags") or []]
                candidates = [next_label if self.normalize_tag_name(value) == source_key else value for value in raw.get("suggestedTags") or []]
                if next_status == "active" and any(self.normalize_tag_name(value) == self.normalize_tag_name(next_label) for value in candidates):
                    tags = list(dict.fromkeys([*tags, next_label]))[:3]
                    candidates = [value for value in candidates if self.normalize_tag_name(value) != self.normalize_tag_name(next_label)]
                raw["tags"] = tags; raw["suggestedTags"] = candidates
                self.conn.execute("UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(raw, ensure_ascii=False), item["id"]))
            self.conn.execute("DELETE FROM question_tag_catalog WHERE id=?", (row["id"],))
        self.sync_question_tags(row["set_id"], row["subject"])
        updated = self.conn.execute("SELECT * FROM question_tag_catalog WHERE set_id=? AND subject=? AND normalized_name=?", (row["set_id"], row["subject"], self.normalize_tag_name(next_label))).fetchone()
        if updated and next_status == "active":
            self.conn.execute("UPDATE question_tag_catalog SET status='active' WHERE id=?", (updated["id"],)); self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM question_tag_catalog WHERE id=?", (updated["id"],)).fetchone())

    def delete_question_tag(self, tag_id: int) -> dict:
        """Permanently remove one tag from questions, candidates and the local catalog."""
        row = self.conn.execute("SELECT * FROM question_tag_catalog WHERE id=?", (int(tag_id),)).fetchone()
        if not row:
            raise ValueError("标签不存在")
        changed = 0
        questions = self.conn.execute(
            "SELECT id,raw_json FROM imported_questions WHERE set_id=? AND subject=?",
            (row["set_id"], row["subject"]),
        ).fetchall()
        with self.conn:
            for question in questions:
                raw = json.loads(question["raw_json"] or "{}")
                before = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                raw["tags"] = [value for value in raw.get("tags") or [] if self.normalize_tag_name(value) != row["normalized_name"]]
                raw["suggestedTags"] = [value for value in raw.get("suggestedTags") or [] if self.normalize_tag_name(value) != row["normalized_name"]]
                if json.dumps(raw, ensure_ascii=False, sort_keys=True) != before:
                    self.conn.execute(
                        "UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (json.dumps(raw, ensure_ascii=False), question["id"]),
                    )
                    changed += 1
            self.conn.execute("DELETE FROM question_tag_catalog WHERE id=?", (row["id"],))
        self.sync_question_tags(row["set_id"], row["subject"])
        return {"label": row["label"], "changed": changed}

    def merge_question_tags(self, source_id: int, target_id: int) -> int:
        source = self.conn.execute("SELECT * FROM question_tag_catalog WHERE id=?", (int(source_id),)).fetchone()
        target = self.conn.execute("SELECT * FROM question_tag_catalog WHERE id=?", (int(target_id),)).fetchone()
        if not source or not target or source["set_id"] != target["set_id"] or source["subject"] != target["subject"] or source["id"] == target["id"]:
            raise ValueError("只能合并同一题目集、同一学科内的不同标签")
        changed = 0
        rows = self.conn.execute("SELECT id,raw_json FROM imported_questions WHERE set_id=? AND subject=?", (source["set_id"], source["subject"])).fetchall()
        with self.conn:
            for row in rows:
                raw = json.loads(row["raw_json"]); before = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                def replace(values): return list(dict.fromkeys(target["label"] if self.normalize_tag_name(value) == source["normalized_name"] else value for value in (values or [])))[:3]
                raw["tags"] = replace(raw.get("tags")); raw["suggestedTags"] = [value for value in replace(raw.get("suggestedTags")) if value not in raw["tags"]]
                if json.dumps(raw, ensure_ascii=False, sort_keys=True) != before:
                    self.conn.execute("UPDATE imported_questions SET raw_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(raw, ensure_ascii=False), row["id"])); changed += 1
            self.conn.execute("UPDATE question_tag_catalog SET status='merged',merged_into=?,usage_count=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (target["label"], source["id"]))
            self.conn.execute("UPDATE question_tag_catalog SET status='active',updated_at=CURRENT_TIMESTAMP WHERE id=?", (target["id"],))
        return changed

    def bulk_update_question_subject(self, question_pks: list[int], subject: str) -> int:
        subject = subject.strip()
        ids = sorted({int(item) for item in question_pks})
        if not subject:
            raise ValueError("学科不能为空")
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id, raw_json FROM imported_questions WHERE id IN ({placeholders})", ids,
        ).fetchall()
        with self.conn:
            for row in rows:
                raw = json.loads(row["raw_json"])
                raw["subject"] = subject
                self.conn.execute(
                    """UPDATE imported_questions SET subject=?,raw_json=?,pipeline_status='queued',
                       retrieval_status='pending',generation_status='pending',match_confidence='pending',
                       review_status='pending',error_message='',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (subject, json.dumps(raw, ensure_ascii=False), row["id"]),
                )
        return len(rows)

    def bulk_queue_questions(self, question_pks: list[int], *, preserve_retrieval: bool = False) -> int:
        ids = sorted({int(item) for item in question_pks})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        if preserve_retrieval:
            cursor = self.conn.execute(
                f"""UPDATE imported_questions SET pipeline_status='queued',generation_status='pending',
                   review_status='pending',error_message='',updated_at=CURRENT_TIMESTAMP
                   WHERE id IN ({placeholders}) AND retrieval_status='matched'
                     AND evidence_json NOT IN ('','[]')""",
                ids,
            )
        else:
            cursor = self.conn.execute(
                f"""UPDATE imported_questions SET pipeline_status='queued',retrieval_status='pending',
                   generation_status='pending',match_confidence='pending',review_status='pending',
                   error_message='',updated_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})""",
                ids,
            )
        self.conn.commit()
        return int(cursor.rowcount)

    def delete_imported_questions(self, question_pks: list[int]) -> int:
        """Permanently delete questions and their review records.

        The desktop UI intentionally has no recoverable question deletion path.  The
        legacy trash table remains readable for old databases, but new deletions never
        write to it.
        """
        ids = sorted({int(item) for item in question_pks})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id,set_id,external_id,subject FROM imported_questions "
            f"WHERE id IN ({placeholders})", ids,
        ).fetchall()
        if not rows:
            return 0
        scopes = {(row["set_id"], row["subject"]) for row in rows}
        with self.conn:
            # Remove any older recoverable snapshot of the same logical question too.
            self.conn.executemany(
                "DELETE FROM question_trash WHERE set_id=? AND external_id=?",
                [(row["set_id"], row["external_id"]) for row in rows],
            )
            self.conn.execute(f"DELETE FROM imported_questions WHERE id IN ({placeholders})", ids)
            for set_id in {row["set_id"] for row in rows}:
                self.conn.execute(
                    "UPDATE question_sets SET question_count="
                    "(SELECT COUNT(*) FROM imported_questions WHERE set_id=?) WHERE id=?",
                    (set_id, set_id),
                )
        for set_id, subject in scopes:
            # Keep tag counts correct even when the deleted question was the last one
            # in a subject.
            remaining = self.conn.execute(
                "SELECT 1 FROM imported_questions WHERE set_id=? AND subject=? LIMIT 1",
                (set_id, subject),
            ).fetchone()
            if remaining:
                self.sync_question_tags(set_id, subject)
            else:
                self.conn.execute(
                    "DELETE FROM question_tag_catalog WHERE set_id=? AND subject=?",
                    (set_id, subject),
                )
                self.conn.commit()
        return len(rows)

    def delete_question_set(self, set_id: str) -> dict:
        """Permanently remove one question set and all records owned by it."""
        set_id = str(set_id or "").strip()
        row = self.conn.execute(
            "SELECT id,name,question_count FROM question_sets WHERE id=?", (set_id,)
        ).fetchone()
        if not row:
            return {"deleted": False, "questions": 0, "jobs": 0}
        question_ids = {
            int(item[0]) for item in self.conn.execute(
                "SELECT id FROM imported_questions WHERE set_id=?", (set_id,)
            ).fetchall()
        }
        def references_set(job: sqlite3.Row) -> bool:
            try:
                payload = json.loads(job["payload_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            if str(payload.get("set_id") or "") == set_id:
                return True
            try:
                question_pk = int(payload.get("question_pk") or 0)
            except (TypeError, ValueError):
                question_pk = 0
            if question_pk in question_ids:
                return True
            queued_ids = {
                int(item) for item in (payload.get("question_ids") or [])
                if str(item).isdigit()
            }
            return bool(question_ids.intersection(queued_ids))
        related_jobs = [
            job for job in self.conn.execute("SELECT id,status,payload_json FROM jobs").fetchall()
            if references_set(job)
        ]
        active = [job for job in related_jobs if job["status"] in {"queued", "running", "paused"}]
        if active:
            raise ValueError("当前题库仍有等待、运行中或已暂停任务，请先取消并等待任务结束。")
        job_ids = [job["id"] for job in related_jobs]
        with self.conn:
            self.conn.execute("DELETE FROM export_records WHERE set_id=?", (set_id,))
            self.conn.execute("DELETE FROM question_trash WHERE set_id=?", (set_id,))
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                self.conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", job_ids)
            # imported_questions, review_actions and question_tag_catalog cascade.
            self.conn.execute("DELETE FROM question_sets WHERE id=?", (set_id,))
        return {
            "deleted": True,
            "name": row["name"],
            "questions": int(row["question_count"] or 0),
            "jobs": len(job_ids),
        }

    def clear_all_question_data(self) -> dict:
        """Permanently clear question-owned data while retaining textbooks/indexes."""
        counts = {
            "sets": int(self.conn.execute("SELECT COUNT(*) FROM question_sets").fetchone()[0]),
            "questions": int(self.conn.execute("SELECT COUNT(*) FROM imported_questions").fetchone()[0]),
            "legacy_questions": int(self.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]),
            "trash": int(self.conn.execute("SELECT COUNT(*) FROM question_trash").fetchone()[0]),
        }
        if self.active_job_count():
            raise ValueError("仍有等待、运行中或已暂停任务，不能清空题目记录。")
        with self.conn:
            self.conn.execute("DELETE FROM export_records")
            self.conn.execute("DELETE FROM question_trash")
            self.conn.execute("DELETE FROM review_actions")
            self.conn.execute("DELETE FROM imported_questions")
            self.conn.execute("DELETE FROM question_tag_catalog")
            self.conn.execute("DELETE FROM question_sets")
            self.conn.execute("DELETE FROM questions")
        return counts

    def move_questions_to_trash(self, question_pks: list[int], retention_days: int = 30) -> int:
        ids = sorted({int(item) for item in question_pks})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        questions = self.conn.execute(
            f"SELECT * FROM imported_questions WHERE id IN ({placeholders}) ORDER BY id", ids
        ).fetchall()
        deleted_at = datetime.now(timezone.utc)
        purge_after = deleted_at + timedelta(days=max(1, int(retention_days)))
        with self.conn:
            for row in questions:
                actions = self.conn.execute(
                    "SELECT action,note,created_at FROM review_actions WHERE question_pk=? ORDER BY id",
                    (row["id"],),
                ).fetchall()
                self.conn.execute(
                    """INSERT INTO question_trash
                       (set_id,external_id,subject,snapshot_json,review_actions_json,deleted_at,purge_after)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        row["set_id"], row["external_id"], row["subject"],
                        json.dumps(dict(row), ensure_ascii=False),
                        json.dumps([dict(item) for item in actions], ensure_ascii=False),
                        deleted_at.isoformat(), purge_after.isoformat(),
                    ),
                )
            if questions:
                self.conn.execute(f"DELETE FROM imported_questions WHERE id IN ({placeholders})", ids)
            for set_id in {row["set_id"] for row in questions}:
                self.conn.execute(
                    "UPDATE question_sets SET question_count="
                    "(SELECT COUNT(*) FROM imported_questions WHERE set_id=?) WHERE id=?",
                    (set_id, set_id),
                )
        return len(questions)

    def list_trash(self, search: str = "", limit: int = 1000, offset: int = 0) -> list[dict]:
        params: list[object] = []
        where = ""
        if search:
            where = "WHERE t.external_id LIKE ? OR t.subject LIKE ? OR q.name LIKE ?"
            params.extend([f"%{search}%"] * 3)
        params.extend([int(limit), int(offset)])
        rows = self.conn.execute(
            f"""SELECT t.*, COALESCE(q.name,'题目集不存在') AS set_name
                FROM question_trash t LEFT JOIN question_sets q ON q.id=t.set_id
                {where} ORDER BY t.deleted_at DESC LIMIT ? OFFSET ?""", params
        ).fetchall()
        return [dict(row) for row in rows]

    def trash_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM question_trash").fetchone()[0])

    def restore_trash(self, trash_ids: list[int]) -> dict:
        ids = sorted({int(item) for item in trash_ids})
        if not ids:
            return {"restored": 0, "conflicts": []}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT * FROM question_trash WHERE id IN ({placeholders}) ORDER BY id", ids
        ).fetchall()
        restored = 0
        conflicts: list[str] = []
        question_columns = [
            "set_id", "external_id", "subject", "question_type", "prompt_text", "raw_json",
            "pipeline_status", "retrieval_status", "generation_status", "match_confidence",
            "review_status", "generation_mode", "match_score", "explanation",
            "evidence_json", "error_message", "viewed_at", "reviewed_at", "updated_at",
        ]
        with self.conn:
            for row in rows:
                exists = self.conn.execute(
                    "SELECT 1 FROM imported_questions WHERE set_id=? AND external_id=?",
                    (row["set_id"], row["external_id"]),
                ).fetchone()
                set_exists = self.conn.execute(
                    "SELECT 1 FROM question_sets WHERE id=?", (row["set_id"],)
                ).fetchone()
                if exists or not set_exists:
                    conflicts.append(row["external_id"])
                    continue
                snapshot = json.loads(row["snapshot_json"])
                values = [snapshot.get(column) for column in question_columns]
                for index, column in enumerate(question_columns):
                    # v8 之前写入的快照没有这三列；显式 NULL 会违反 NOT NULL DEFAULT 约束
                    if values[index] is None and column in (
                        "retrieval_status", "generation_status", "match_confidence"):
                        values[index] = "pending"
                cursor = self.conn.execute(
                    f"INSERT INTO imported_questions({','.join(question_columns)}) "
                    f"VALUES({','.join('?' for _ in question_columns)})", values,
                )
                question_pk = int(cursor.lastrowid)
                actions = json.loads(row["review_actions_json"] or "[]")
                self.conn.executemany(
                    "INSERT INTO review_actions(question_pk,action,note,created_at) VALUES(?,?,?,?)",
                    [(question_pk, item["action"], item.get("note", ""), item["created_at"]) for item in actions],
                )
                self.conn.execute("DELETE FROM question_trash WHERE id=?", (row["id"],))
                self.conn.execute(
                    "UPDATE question_sets SET question_count="
                    "(SELECT COUNT(*) FROM imported_questions WHERE set_id=?) WHERE id=?",
                    (row["set_id"], row["set_id"]),
                )
                restored += 1
        return {"restored": restored, "conflicts": conflicts}

    def purge_trash(self, trash_ids: list[int]) -> int:
        ids = sorted({int(item) for item in trash_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = self.conn.execute(f"DELETE FROM question_trash WHERE id IN ({placeholders})", ids)
        self.conn.commit()
        return int(cursor.rowcount)

    def purge_expired_trash(self, now: Optional[datetime] = None) -> int:
        current = (now or datetime.now(timezone.utc)).isoformat()
        cursor = self.conn.execute("DELETE FROM question_trash WHERE purge_after<=?", (current,))
        self.conn.commit()
        return int(cursor.rowcount)

    def active_job_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','paused')"
        ).fetchone()[0])

    def subject_library_count(self, subject: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM libraries WHERE active=1 AND subject=?", (subject,)
        ).fetchone()[0])

    def question_subjects(self, set_id: str, question_ids: Optional[list[int]] = None) -> list[str]:
        clauses = ["set_id=?"]
        params: list[object] = [set_id]
        selected = list(dict.fromkeys(int(item) for item in (question_ids or []) if int(item) > 0))
        if question_ids is not None:
            if not selected:
                return []
            clauses.append(f"id IN ({','.join('?' for _ in selected)})")
            params.extend(selected)
        rows = self.conn.execute(
            f"SELECT DISTINCT subject FROM imported_questions WHERE {' AND '.join(clauses)} ORDER BY subject",
            params,
        ).fetchall()
        return [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]

    def mark_question_viewed(self, question_pk: int) -> None:
        self.conn.execute(
            "UPDATE imported_questions SET viewed_at=COALESCE(viewed_at, CURRENT_TIMESTAMP) WHERE id=?", (question_pk,)
        )
        self.conn.commit()

    @staticmethod
    def _quick_review_reason(row: dict, minimum_score: float) -> str:
        """Return an exclusion reason, or an empty string when the result is safely reviewable."""
        if row.get("pipeline_status") != "generated" or row.get("review_status") != "pending":
            return "状态不属于待审核"
        if row.get("generation_mode") != "textbook":
            return "不是教材解析"
        if str(row.get("error_message") or "").strip():
            return "存在生成错误"
        if float(row.get("match_score") or 0) < float(minimum_score):
            return "相似度低于门槛"
        explanation = str(row.get("explanation") or "").strip()
        if len(explanation) < 80:
            return "解析过短"
        try:
            raw = json.loads(row.get("raw_json") or "{}")
            evidence = json.loads(row.get("evidence_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return "结构数据损坏"
        meta = raw.get("explanationMeta") if isinstance(raw.get("explanationMeta"), dict) else {}
        if str(meta.get("evidenceGrade") or "A").upper() != "A":
            return "仅 A 级证据可快速审核"
        if meta.get("reviewWarnings") or raw.get("reviewWarnings"):
            return "存在格式警告"
        blocks = raw.get("explanationBlocks")
        if not isinstance(blocks, list) or not blocks:
            return "缺少结构化解析"
        allowed_sections = {"analysis", "answerBasis", "pitfalls", "clinicalNotes"}
        allowed_types = {"paragraph", "list", "table", "callout"}
        if any(
            not isinstance(block, dict)
            or block.get("section") not in allowed_sections
            or block.get("type") not in allowed_types
            for block in blocks
        ):
            return "解析区块格式异常"
        sections = {str(block.get("section")) for block in blocks}
        if not {"analysis", "answerBasis", "pitfalls"}.issubset(sections):
            return "核心解析区块不完整"
        if not any(block.get("section") == "pitfalls" and block.get("type") == "table" for block in blocks):
            return "易错点未使用表格"
        if not (raw.get("tags") or raw.get("suggestedTags")):
            return "缺少题目标签"
        if not str(raw.get("briefExplanation") or "").strip():
            return "缺少一句话简析"
        if not isinstance(evidence, list) or not evidence:
            return "缺少教材证据"
        valid_evidence = False
        for item in evidence:
            if not isinstance(item, dict):
                continue
            textbook = str(item.get("textbook") or item.get("source_file") or item.get("fileName") or "").strip()
            source_page = item.get("source_page", item.get("sourcePage"))
            quote = str(item.get("text") or item.get("quote") or "").strip()
            score = float(item.get("score") or 0)
            if textbook and str(source_page or "").isdigit() and int(source_page) > 0 and len(quote) >= 20 and score >= minimum_score:
                valid_evidence = True
                break
        if not valid_evidence:
            return "教材出处或证据不完整"
        return ""

    def quick_review_candidates(self, set_id: str, minimum_score: float = 0.75) -> dict:
        """Preview strict quick-review candidates without changing any review state."""
        minimum_score = min(0.99, max(0.5, float(minimum_score)))
        rows = self.conn.execute(
            """SELECT id,external_id,subject,pipeline_status,review_status,generation_mode,
                      match_score,explanation,evidence_json,raw_json,error_message
               FROM imported_questions
               WHERE set_id=? AND pipeline_status='generated' AND review_status='pending'
               ORDER BY id""",
            (set_id,),
        ).fetchall()
        eligible: list[dict] = []
        excluded: Counter[str] = Counter()
        for source in rows:
            row = dict(source)
            reason = self._quick_review_reason(row, minimum_score)
            if reason:
                excluded[reason] += 1
                continue
            evidence = json.loads(row["evidence_json"] or "[]")
            eligible.append({
                "id": int(row["id"]), "external_id": row["external_id"],
                "subject": row["subject"], "match_score": float(row["match_score"] or 0),
                "evidence_count": len(evidence),
            })
        return {"total": len(rows), "eligible": eligible, "excluded": dict(excluded), "minimum_score": minimum_score}

    def bulk_approve_quick_review(
        self, set_id: str, minimum_score: float = 0.75,
        expected_ids: Optional[list[int]] = None,
    ) -> dict:
        """Atomically approve only candidates that still satisfy every strict gate."""
        preview = self.quick_review_candidates(set_id, minimum_score)
        eligible_ids = {int(item["id"]) for item in preview["eligible"]}
        expected = eligible_ids if expected_ids is None else {int(item) for item in expected_ids}
        approved_ids = sorted(eligible_ids & expected)
        skipped = len(expected - eligible_ids)
        if not approved_ids:
            return {"approved": 0, "skipped": skipped}
        placeholders = ",".join("?" for _ in approved_ids)
        rows = self.conn.execute(
            f"SELECT id,raw_json FROM imported_questions WHERE id IN ({placeholders}) ORDER BY id",
            approved_ids,
        ).fetchall()
        reviewed_at = datetime.now(timezone.utc).isoformat()
        note = f"快速审核：教材相似度≥{float(minimum_score):.2f}，且通过结构、证据、知识点与标签门槛"
        from question_format_v2 import normalize_question_v2
        with self.conn:
            for row in rows:
                raw = json.loads(row["raw_json"] or "{}")
                meta = raw.get("explanationMeta") if isinstance(raw.get("explanationMeta"), dict) else {}
                raw["explanationMeta"] = {**meta, "reviewedAt": reviewed_at}
                normalized = normalize_question_v2(raw)
                self.conn.execute(
                    """UPDATE imported_questions SET review_status='approved', explanation=?, raw_json=?,
                       reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (normalized["explanation"], json.dumps(normalized, ensure_ascii=False), int(row["id"])),
                )
                self.conn.execute(
                    "INSERT INTO review_actions(question_pk, action, note) VALUES(?, 'approved', ?)",
                    (int(row["id"]), note),
                )
        return {"approved": len(rows), "skipped": skipped}

    def review_question(self, question_pk: int, action: str, explanation: str, note: str = "") -> None:
        if action not in {"approved", "rejected", "pending"}:
            raise ValueError("Invalid review action")
        if action == "approved":
            row = self.conn.execute("SELECT viewed_at FROM imported_questions WHERE id=?", (question_pk,)).fetchone()
            if not row or not row["viewed_at"]:
                raise ValueError("题目必须先打开查看才能批准")
            if not explanation.strip():
                raise ValueError("解析为空，不能批准")
        raw_row = self.conn.execute("SELECT raw_json,evidence_json,generation_mode,match_score FROM imported_questions WHERE id=?", (question_pk,)).fetchone()
        raw = json.loads(raw_row["raw_json"]) if raw_row else {}
        from question_format_v2 import normalize_blocks, normalize_question_v2
        if explanation.strip() != str(raw.get("explanation") or "").strip() or not raw.get("explanationBlocks"):
            raw["explanationBlocks"] = normalize_blocks([], explanation)
        raw["explanation"] = explanation
        raw["explanationMeta"] = {
            **(raw.get("explanationMeta") or {}), "mode": raw_row["generation_mode"] if raw_row else "",
            "score": float(raw_row["match_score"] or 0) if raw_row else 0,
            "evidence": json.loads(raw_row["evidence_json"] or "[]") if raw_row else [],
            "reviewedAt": datetime.now(timezone.utc).isoformat() if action != "pending" else "",
        }
        raw = normalize_question_v2(raw)
        explanation = raw["explanation"]
        with self.conn:
            self.conn.execute(
                """UPDATE imported_questions SET review_status=?, explanation=?,
                   raw_json=?,
                   reviewed_at=CASE WHEN ?='pending' THEN NULL ELSE CURRENT_TIMESTAMP END,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (action, explanation, json.dumps(raw, ensure_ascii=False), action, question_pk),
            )
            self.conn.execute(
                "INSERT INTO review_actions(question_pk, action, note) VALUES(?, ?, ?)",
                (question_pk, action, note),
            )

    def create_job(self, job_type: str, title: str, payload: dict) -> str:
        job_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO jobs(id, job_type, title, payload_json) VALUES(?, ?, ?, ?)",
            (job_id, job_type, title, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()
        return job_id

    def get_job(self, job_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload_json"])
        return value

    def list_jobs(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def delete_jobs(self, job_ids: list[str], terminal_only: bool = True) -> dict:
        """Permanently delete task rows; active tasks must finish or be cancelled first."""
        ids = list(dict.fromkeys(str(item) for item in job_ids if str(item).strip()))
        if not ids:
            return {"deleted": 0, "blocked": []}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id,status FROM jobs WHERE id IN ({placeholders})", ids,
        ).fetchall()
        blocked = [
            row["id"] for row in rows
            if terminal_only and row["status"] in {"queued", "running", "paused"}
        ]
        deletable = [row["id"] for row in rows if row["id"] not in blocked]
        if deletable:
            targets = ",".join("?" for _ in deletable)
            with self.conn:
                self.conn.execute(f"DELETE FROM jobs WHERE id IN ({targets})", deletable)
        return {"deleted": len(deletable), "blocked": blocked}

    def delete_finished_jobs(self) -> int:
        cursor = self.conn.execute(
            "DELETE FROM jobs WHERE status IN ('completed','failed','cancelled')"
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def next_queued_job(self) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return dict(row) if row else None

    def update_job(
        self, job_id: str, status: Optional[str] = None, current: Optional[int] = None,
        total: Optional[int] = None, message: Optional[str] = None, error: Optional[str] = None,
    ) -> None:
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        params: list[object] = []
        for column, value in (("status", status), ("progress_current", current), ("progress_total", total),
                              ("message", message), ("error_message", error)):
            if value is not None:
                fields.append(f"{column}=?")
                params.append(value)
        if status == "running":
            fields.append("started_at=COALESCE(started_at, CURRENT_TIMESTAMP)")
        if status in {"completed", "failed", "cancelled"}:
            fields.append("finished_at=CURRENT_TIMESTAMP")
        params.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", params)
        self.conn.commit()

    def set_job_control(self, job_id: str, control: str) -> None:
        if control not in {"run", "pause", "cancel"}:
            raise ValueError("Invalid job control")
        self.conn.execute("UPDATE jobs SET control=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (control, job_id))
        self.conn.commit()

    def add_job_event(self, job_id: str, message: str, level: str = "info") -> None:
        self.conn.execute("INSERT INTO job_events(job_id, level, message) VALUES(?, ?, ?)", (job_id, level, message))
        self.conn.commit()

    def recover_incomplete_jobs(self) -> None:
        self.conn.execute(
            "UPDATE jobs SET status='queued', control='run', message='等待恢复' WHERE status IN ('running', 'paused')"
        )
        self.conn.commit()

    def approved_questions(self, set_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM imported_questions WHERE set_id=? AND review_status='approved' ORDER BY id", (set_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def v2_upgrade_questions(self, set_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM imported_questions WHERE set_id=? ORDER BY id", (set_id,)).fetchall()
        result=[]
        for row in rows:
            item=dict(row);item["raw"]=json.loads(item["raw_json"]);item["evidence"]=json.loads(item["evidence_json"] or "[]");result.append(item)
        return result

    def backup_database(self, destination: str | Path) -> str:
        target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True)
        connection=sqlite3.connect(target)
        try:self.conn.backup(connection)
        finally:connection.close()
        return str(target)

    def replace_v2_upgrade_result(self, question_pk: int, package: dict) -> None:
        row=self.conn.execute("SELECT set_id,subject,raw_json FROM imported_questions WHERE id=?",(int(question_pk),)).fetchone()
        if not row:raise ValueError("题目不存在")
        from question_format_v2 import normalize_question_v2
        raw=json.loads(row["raw_json"]);raw.update({key:package.get(key,raw.get(key)) for key in ("explanationBlocks","briefExplanation","knowledgePoints","tags","suggestedTags","explanationMeta")});raw=normalize_question_v2(raw)
        with self.conn:self.conn.execute("UPDATE imported_questions SET raw_json=?,explanation=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(json.dumps(raw,ensure_ascii=False),raw["explanation"],int(question_pk)))
        self.sync_question_tags(row["set_id"],row["subject"])

    def list_export_records(self, set_id: str = "", limit: int = 30) -> list[dict]:
        if set_id:
            rows = self.conn.execute(
                "SELECT * FROM export_records WHERE set_id=?"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (set_id, int(limit))).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM export_records ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def add_export_record(self, set_id: str, export_type: str, path: str, count: int) -> None:
        self.conn.execute(
            "INSERT INTO export_records(set_id, export_type, output_path, item_count) VALUES(?, ?, ?, ?)",
            (set_id, export_type, path, count),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
