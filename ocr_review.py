"""Explicit single-page cloud comparisons; never alter textbook evidence or indices."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import fitz

from db_manager import DatabaseManager
from ocr_client import QwenOCRClient, effective_ocr_model
from pdf_parser import PDFParser

REVIEW_MODELS = ("qwen3.8-max", "qwen3.7-plus")


def ensure_schema(database):
    database.conn.execute("""CREATE TABLE IF NOT EXISTS ocr_page_reviews (
        id TEXT PRIMARY KEY, library_id INTEGER NOT NULL, created_at TEXT NOT NULL,
        pdf_page INTEGER NOT NULL, report_json TEXT NOT NULL)""")
    database.conn.commit()


def history(database_path, library_id):
    with DatabaseManager(database_path) as db:
        ensure_schema(db)
        return [dict(row) for row in db.conn.execute(
            "SELECT id,created_at,pdf_page FROM ocr_page_reviews WHERE library_id=? ORDER BY created_at DESC LIMIT 20",
            (library_id,))]


def get_report(database_path, library_id, review_id):
    with DatabaseManager(database_path) as db:
        ensure_schema(db)
        row = db.conn.execute("SELECT report_json FROM ocr_page_reviews WHERE id=? AND library_id=?",
                              (review_id, library_id)).fetchone()
        if not row:
            raise ValueError("not_found: 复核记录不存在")
        return json.loads(row[0])


def compare_page(database_path, library_id, pdf_page, model, config, progress):
    if type(pdf_page) is not int or pdf_page < 1 or model not in REVIEW_MODELS:
        raise ValueError("bad_payload: 请填写有效物理页和复核模型")
    dash = config["dashscope"]
    baseline = effective_ocr_model(dash.get("ocr_model", ""))
    if baseline == model:
        raise ValueError("bad_payload: 复核模型必须与当前 OCR 模型不同")
    if not dash.get("api_key"):
        raise ValueError("请先配置 DashScope 密钥")
    with DatabaseManager(database_path) as db:
        library = db.get_library(library_id)
        if not library or not library["active"]:
            raise ValueError("not_found: 教材不存在或已移除")
        source = Path(library["root_path"])
    progress("读取指定物理页", 0, 2)
    with fitz.open(source) as document:
        if document.needs_pass or pdf_page > len(document):
            raise ValueError("PDF 已加密或指定物理页超出范围")
        image_path = Path(PDFParser._render_page(document[pdf_page - 1]))
    try:
        image_bytes = image_path.read_bytes()
        report = {"id": uuid.uuid4().hex, "library_id": library_id, "pdf_page": pdf_page,
                  "created_at": datetime.now(timezone.utc).isoformat(), "source_name": source.name,
                  "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                  "image": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
                  "results": [], "index_modified": False}
        # Keep exactly the same raster for both models and for human review.
        for i, selected in enumerate((baseline, model)):
            progress(f"调用 {selected}（每个模型最多一次，不自动重试）", i, 2)
            client = QwenOCRClient(dash["api_key"], selected, dash.get("base_url", ""), timeout=60)
            try:
                text = client.recognize_image(image_path, max_retries=1)
                report["results"].append({"model": selected, "ok": True, "text": text,
                    "raw_text": client.last_raw_text, "trace": client.last_trace})
            except RuntimeError as exc:
                report["results"].append({"model": selected, "ok": False, "error": str(exc),
                                          "trace": client.last_trace})
            finally:
                client.client.close()
        report["total_tokens"] = sum(row.get("trace", {}).get("usage", {}).get("total_tokens", 0) or 0
                                     for row in report["results"])
        successful = [row["text"] for row in report["results"] if row["ok"]]
        report["identical"] = len(successful) == 2 and successful[0] == successful[1]
        progress("提交入库", 2, 2)
        with DatabaseManager(database_path) as db:
            ensure_schema(db)
            with db.conn:
                db.conn.execute("INSERT INTO ocr_page_reviews VALUES(?,?,?,?,?)",
                    (report["id"], library_id, report["created_at"], pdf_page, json.dumps(report, ensure_ascii=False)))
        return report
    finally:
        image_path.unlink(missing_ok=True)
