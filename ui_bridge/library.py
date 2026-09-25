"""Library domain: textbook libraries, page-offset calibration and cloud notice."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QFileDialog

from db_manager import DatabaseManager
from index_profile import build_index_profile, index_fingerprint, retrieval_compatible
from runtime_config import runtime_config
from ui_bridge.protocol import BridgeBase
from parser_health import index_error_summary
from batch_tasks import BatchTasks, BatchCancelled
from textbook_probe import TextbookInspector, file_identity

PDF_FILE_FILTER = "PDF 文件 (*.pdf)"


def _require(fields: dict[str, str]) -> None:
    missing = [label for label, value in fields.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(f"bad_payload: 请填写{'、'.join(missing)}")


def _index_state(row: dict, current_fingerprint: str) -> str:
    if not row.get("file_count"):
        return "none"
    if row.get("file_status") == "error":
        return "error"
    if row.get("file_status") == "warning" or row.get("file_error"):
        return "partial"
    if row.get("file_status") in {"indexing", "missing"}:
        return "unknown"
    fingerprint = str(row.get("index_fingerprint") or "")
    if not fingerprint:
        return "unknown"
    return "compatible" if fingerprint == current_fingerprint else "stale"


class LibraryBridge(BridgeBase):
    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path
        self._tasks = BatchTasks()
        self._ocr_tasks = BatchTasks()

    def api_ocr_review_start(self, library_id: int, pdf_page: int, model: str, consent: bool = False) -> dict:
        if consent is not True:
            raise ValueError("bad_payload: 请先确认单页双模型调用与费用说明")
        from ocr_review import compare_page, REVIEW_MODELS
        if type(library_id) is not int or type(pdf_page) is not int or pdf_page < 1 or model not in REVIEW_MODELS:
            raise ValueError("bad_payload: 复核参数无效")
        def review(progress):
            return compare_page(self._database_path, library_id, pdf_page, model,
                                runtime_config(database_override=self._database_path), progress)
        return self._ocr_tasks.start("ocr_review", review)

    def api_ocr_review_status(self, token: str) -> dict:
        return self._ocr_tasks.status(token)

    def api_ocr_review_active(self) -> dict:
        return {"task": self._ocr_tasks.active()}

    def api_ocr_review_history(self, library_id: int) -> dict:
        from ocr_review import history
        return {"items": history(self._database_path, library_id)}

    def api_ocr_review_report(self, library_id: int, review_id: str) -> dict:
        from ocr_review import get_report
        return get_report(self._database_path, library_id, review_id)

    def api_list(self) -> dict:
        profile = build_index_profile(runtime_config())
        current = index_fingerprint(profile)
        with DatabaseManager(self._database_path) as database:
            libraries = database.list_libraries()
        for row in libraries:
            row["index_state"] = _index_state(row, current)
            if row["index_state"] == "stale" and retrieval_compatible(row, profile):
                row["index_state"] = "compatible"
            row["file_error_summary"] = index_error_summary(row.get("file_error") or "")
            row["calibration"] = json.loads(row.get("calibration_json") or "{}")
        return {"libraries": libraries, "current_fingerprint": current}

    def api_pick_pdfs(self) -> dict:
        paths, _ = QFileDialog.getOpenFileNames(None, "批量选择教材 PDF（可多选）", "", PDF_FILE_FILTER)
        return {"paths": paths}

    def api_inspect_start(self, paths: list[str]) -> dict:
        if not isinstance(paths, list) or not 1 <= len(paths) <= 100 or any(not isinstance(p, str) for p in paths):
            raise ValueError("bad_payload: 每批请选择 1～100 个 PDF")
        def inspect(progress):
            config = runtime_config(database_override=self._database_path)
            from ocr_client import QwenOCRClient
            dash = config["dashscope"]
            ocr = QwenOCRClient(dash["api_key"], dash["ocr_model"], dash.get("base_url", "")) if dash.get("api_key") else None
            inspector, items, seen = TextbookInspector(ocr_client=ocr), [], set()
            for index, path in enumerate(paths):
                progress(f"识别教材 {index+1}/{len(paths)}", index, len(paths))
                source = Path(path).resolve()
                key = str(source).casefold()
                row = {"root_path": str(source), "file_name": source.name, "name": source.stem,
                       "subject": "", "version": "", "error": "", "warnings": []}
                try:
                    if key in seen:
                        raise ValueError("同一文件重复选择，本条不会导入")
                    seen.add(key)
                    row = inspector.inspect(source, lambda stage, current, total:
                        progress(f"第 {index+1}/{len(paths)} 本 · {stage}", current, total))
                except BatchCancelled:
                    raise
                except Exception as exc:
                    row["error"] = str(exc)
                items.append({**row, "index": index})
            progress("识别完成，等待确认", len(paths), len(paths))
            return {"items": items}
        return self._tasks.start("inspect", inspect)

    def api_inspect_status(self, token: str) -> dict:
        return self._tasks.status(token)

    def api_inspect_cancel(self, token: str) -> dict:
        return self._tasks.cancel(token)

    def api_inspect_active(self) -> dict:
        return {"task": self._tasks.active()}

    def api_commit_batch(self, token: str, items: list[dict], library_id: int | None = None) -> dict:
        draft = self._tasks.status(token)
        if draft["status"] != "completed" or draft["action"] != "inspect":
            raise ValueError("bad_payload: 请先完成教材识别")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("bad_payload: 请至少选择一本教材")
        source_rows = {row["index"]: row for row in draft["result"]["items"]}
        indexes = [row.get("index") for row in items]
        if len(set(indexes)) != len(indexes) or any(i not in source_rows for i in indexes):
            raise ValueError("bad_payload: 教材批次条目无效")
        if library_id is not None and len(items) != 1:
            raise ValueError("bad_payload: 每次只能校准一本已有教材")
        def commit(progress):
            prepared, results = [], []
            for position, form in enumerate(items):
                progress("核对文件是否变化", position, len(items))
                row = source_rows[form["index"]]
                try:
                    if row.get("error"):
                        raise ValueError(row["error"])
                    if file_identity(row["root_path"]) != row["identity"]:
                        raise ValueError("文件已变化，请重新识别")
                    name, subject = str(form.get("name", "")).strip(), str(form.get("subject", "")).strip()
                    _require({"名称": name, "学科": subject})
                    mode = form.get("mode", "auto")
                    if mode not in {"auto", "manual"}:
                        raise ValueError("未知页码校准方式")
                    offset = 0
                    if mode == "manual":
                        pdf, printed = form.get("pdf_anchor"), form.get("textbook_anchor")
                        if type(pdf) is not int or type(printed) is not int or not 1 <= pdf <= row["page_count"] or not 1 <= printed <= 20000:
                            raise ValueError("请填写有效的 PDF 页和课本页（整数）")
                        offset = pdf - printed
                    prepared.append({**row, "calibration": {**row["calibration"], "file_sha256": row["identity"]["sha256"]},
                                     "name": name, "subject": subject, "version": str(form.get("version", "")).strip(),
                                     "mode": mode, "page_offset": offset})
                except Exception as exc:
                    results.append({"index": row["index"], "status": "error", "message": str(exc)})
            progress("提交入库", 0, len(prepared))
            with DatabaseManager(self._database_path) as database, database.conn:
                existing = {str(Path(r["root_path"]).resolve()).casefold(): r for r in database.list_libraries(active_only=False)}
                for row in prepared:
                    if library_id is not None:
                        current = database.get_library(int(library_id))
                        if not current or not current["active"] or current["root_path"] != row["root_path"]:
                            raise ValueError("教材来源已变化，请关闭后重新校准")
                        if row["mode"] != "auto":
                            raise ValueError("已有教材的手工统一偏移请使用“编辑”")
                        if row["calibration"]["status"] != "recognized":
                            raise ValueError("没有可靠页码结果，保留原校准；可使用“编辑”手工设置")
                        for job in database.conn.execute("SELECT payload_json FROM jobs WHERE job_type='scan' AND status='running'"):
                            if json.loads(job[0]).get("library_id") == int(library_id):
                                raise ValueError("教材正在索引，请完成或暂停后再应用校准")
                        indexed = database.get_file_by_path(row["root_path"])
                        if indexed and indexed["sha256"] != row["identity"]["sha256"]:
                            raise ValueError("教材 PDF 与已有索引不同，请先重建索引再校准")
                        affected = database.apply_library_calibration(int(library_id), row["calibration"], commit=False)
                        results.append({"index": row["index"], "status": "calibrated", "library_id": library_id, "affected_questions": affected})
                        continue
                    key = row["root_path"].casefold()
                    if key in existing:
                        results.append({"index": row["index"], "status": "duplicate", "message": "教材已存在（含已移除记录），没有覆盖原记录"})
                        continue
                    new_id = database.conn.execute("INSERT INTO libraries(name, subject, version, root_path, page_offset) VALUES(?,?,?,?,?)",
                        (row["name"], row["subject"], row["version"], row["root_path"], row["page_offset"])).lastrowid
                    if row["mode"] == "auto":
                        database.apply_library_calibration(new_id, row["calibration"], commit=False)
                    existing[key] = row
                    results.append({"index": row["index"], "status": "imported", "library_id": new_id})
            return {"results": sorted(results, key=lambda r: r["index"]), "imported": sum(r["status"] == "imported" for r in results)}
        return self._tasks.start("commit", commit)

    def api_pick_pdf(self) -> dict:
        path, _ = QFileDialog.getOpenFileName(None, "选择教材 PDF", "", PDF_FILE_FILTER)
        return {"path": path or ""}

    def api_add(self, name: str = "", subject: str = "", root_path: str = "",
                version: str = "", page_offset: int = 0) -> dict:
        _require({"名称": name, "学科": subject, "PDF 文件": root_path})
        with DatabaseManager(self._database_path) as database:
            try:
                library_id = database.add_library(
                    name.strip(), subject.strip(), root_path.strip(),
                    (version or "").strip(), int(page_offset or 0))
            except ValueError as exc:
                raise ValueError(f"bad_payload: {exc}") from exc
        return {"library_id": library_id}

    def api_update(self, library_id: int, name: str = "", subject: str = "",
                   root_path: str = "", version: str = "", page_offset: int = 0) -> dict:
        _require({"名称": name, "学科": subject, "PDF 文件": root_path})
        with DatabaseManager(self._database_path) as database:
            if not database.get_library(int(library_id)):
                raise ValueError("not_found: 教材不存在")
            try:
                database.update_library(
                    int(library_id), name.strip(), subject.strip(),
                    (version or "").strip(), root_path.strip(), int(page_offset or 0))
            except ValueError as exc:
                raise ValueError(f"bad_payload: {exc}") from exc
        return {"updated": True}

    def api_remove(self, library_id: int) -> dict:
        with DatabaseManager(self._database_path) as database:
            if not database.get_library(int(library_id)):
                raise ValueError("not_found: 教材不存在")
            database.deactivate_library(int(library_id))
        return {"removed": True}

    def api_infer_page_offset(self, library_id: int) -> dict:
        with DatabaseManager(self._database_path) as database:
            if not database.get_library(int(library_id)):
                raise ValueError("not_found: 教材不存在")
            offset = database.infer_library_page_offset(int(library_id), force=True)
        return {"offset": offset}

    def api_cloud_notice(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            accepted = database.get_setting("cloud_notice_accepted") == "1"
        return {"accepted": accepted}

    def api_accept_cloud_notice(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            database.set_setting("cloud_notice_accepted", "1")
        return {"accepted": True}
