"""Imports domain: file import wizard, smart organize workspace and v2 upgrade."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog

from db_manager import DatabaseManager
from importers import (
    MAPPING_FIELDS,
    ImportValidationError,
    auto_mapping,
    excel_headers,
    extract_question_text,
    is_medical_question_collection_docx,
    validate_questions,
)
from runtime_config import runtime_config
from services import QuestionFormattingService, V2UpgradeService
from ui_bridge.protocol import BridgeBase

IMPORT_FILE_FILTER = "题目文件 (*.json *.xlsx *.xlsm)"
ORGANIZE_FILE_FILTER = "题目文件 (*.txt *.md *.docx *.xlsx *.xlsm)"


def _summarize_errors(errors: list[str], limit: int = 20) -> str:
    shown = list(errors[:limit])
    if len(errors) > limit:
        shown.append(f"…共 {len(errors)} 个问题，仅显示前 {limit} 条")
    return "\n".join(shown)


def _row_warnings(rows: list[dict], imported_ids: set[str] | None = None) -> list[list[str]]:
    """Per-row validation plus cross-row duplicate / already-imported ID checks."""
    warnings = QuestionFormattingService.validation_messages(rows)
    imported = {str(item) for item in (imported_ids or set())}
    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        external_id = str(row.get("id", "") or "").strip()
        if not external_id:
            continue
        if external_id in seen:
            warnings[index].append(f"题目 ID 与第 {seen[external_id]} 条重复：{external_id}")
        else:
            seen[external_id] = index + 1
        if external_id in imported:
            warnings[index].append(f"题目 ID 已导入：{external_id}")
    return warnings


class ImportsBridge(BridgeBase):
    organize_result = Signal(str)
    organize_progress = Signal(str)

    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path
        from batch_tasks import BatchTasks
        self._batch_tasks = BatchTasks()
        self._file_review = None

    def api_batch_start(self, action: str, payload: dict) -> dict:
        import batch_import
        allowed = {"preview": {"paths", "name", "profile"},
                   "resume": {"draft_id", "corrections"},
                   "commit": {"draft_id", "start_generation"}}
        if action not in allowed or not isinstance(payload, dict) or set(payload) - allowed[action]:
            raise ValueError("bad_payload: 不支持的批量操作或参数")
        # Detach request data before handing it to the worker.
        params = json.loads(json.dumps(payload))
        def execute(progress):
            with DatabaseManager(self._database_path) as database:
                start_generation = bool(params.pop("start_generation", False))
                result = getattr(batch_import, action)(database, **params, progress=progress)
                if action == "commit" and start_generation:
                    result["job_ids"] = [database.create_job("generate", "批量导入后自动生成", {"set_id": set_id})
                                         for set_id in result["result"].get("new_set_ids", [])]
                return result
        return self._batch_tasks.start(action, execute)

    def api_batch_status(self, token: str) -> dict:
        return self._batch_tasks.status(token)

    def api_batch_active(self) -> dict:
        return {"task": self._batch_tasks.active()}

    def api_batch_cancel(self, token: str) -> dict:
        return self._batch_tasks.cancel(token)

    # --- native file dialogs (invoked on the GUI thread via invoke) ---

    def api_pick_batch_files(self) -> dict:
        paths, _ = QFileDialog.getOpenFileNames(None, "批量选择题目文件", "", IMPORT_FILE_FILTER)
        return {"paths": paths}

    def api_import_profiles(self, name: str = "", profile: dict | None = None) -> dict:
        from batch_import import profiles
        with DatabaseManager(self._database_path) as database:
            return {"profiles": profiles(database, name.strip(), profile)}

    def api_batch_preview(self, paths: list[str], name: str, profile: dict | None = None) -> dict:
        from batch_import import preview
        with DatabaseManager(self._database_path) as database:
            return preview(database, paths, name, profile)

    def api_batch_resume(self, draft_id: str, corrections: dict | None = None) -> dict:
        from batch_import import resume
        with DatabaseManager(self._database_path) as database:
            return resume(database, draft_id, corrections)

    def api_batch_commit(self, draft_id: str, start_generation: bool = False) -> dict:
        from batch_import import commit
        with DatabaseManager(self._database_path) as database:
            result = commit(database, draft_id)
            if start_generation:
                result["job_ids"] = [database.create_job("generate", "批量导入后自动生成", {"set_id": set_id})
                                     for set_id in result["result"].get("new_set_ids", [])]
            return result

    def api_batch_drafts(self) -> dict:
        from batch_import import schema
        with DatabaseManager(self._database_path) as database:
            schema(database)
            rows = database.conn.execute("SELECT id,name,created_at FROM import_drafts ORDER BY created_at DESC LIMIT 50").fetchall()
            return {"drafts": [dict(row) for row in rows]}

    def api_pick_import_file(self) -> dict:
        path, _ = QFileDialog.getOpenFileName(None, "选择题目文件", "", IMPORT_FILE_FILTER)
        return {"path": path or ""}

    def api_pick_organize_file(self) -> dict:
        path, _ = QFileDialog.getOpenFileName(None, "选择原始题目文件", "", ORGANIZE_FILE_FILTER)
        return {"path": path or ""}

    def api_pick_save_path(self, default_name: str, file_filter: str) -> dict:
        path, _ = QFileDialog.getSaveFileName(None, "选择保存位置", default_name, file_filter)
        return {"path": path or ""}

    # --- file import wizard ---

    def api_inspect_file(self, path: str) -> dict:
        source = Path(path)
        suffix = source.suffix.lower()
        if suffix == ".json":
            return {"kind": "json", "name": source.stem, "headers": [], "mapping": {}}
        if suffix in {".xlsx", ".xlsm"}:
            headers = excel_headers(source)
            return {
                "kind": "excel",
                "name": source.stem,
                "headers": headers,
                "mapping": auto_mapping(headers),
                "fields": list(MAPPING_FIELDS),
            }
        raise ValueError("bad_payload: 仅支持 JSON、XLSX 和 XLSM 文件")

    def api_import_file(self, path: str, name: str = "", mapping: dict | None = None) -> dict:
        if not (name or "").strip():
            raise ValueError("bad_payload: 请填写题目集名称")
        from file_import_review import FileImportReview
        try:
            review = FileImportReview.from_file(path, name.strip(), mapping)
        except ImportValidationError as exc:
            raise ValueError(f"import_invalid: {_summarize_errors(exc.errors)}") from exc
        self._file_review = review
        if review.errors or review.repaired:
            return {"needs_review": True, "review": review.snapshot()}
        with DatabaseManager(self._database_path) as database:
            return review.commit(database)

    def _require_file_review(self, draft_id):
        if self._file_review is None or self._file_review.id != draft_id:
            raise ValueError("bad_state: 导入预览已失效，请重新选择文件并检查")
        return self._file_review

    def api_resolve_file_import(self, draft_id: str, action: str, index: int, row: dict | None = None) -> dict:
        return self._require_file_review(draft_id).resolve(action, index, row)

    def api_commit_file_import(self, draft_id: str) -> dict:
        review = self._require_file_review(draft_id)
        with DatabaseManager(self._database_path) as database:
            return review.commit(database)

    def api_list_sets(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            return {"sets": database.list_question_sets()}

    # --- v2 upgrade (enqueue itself goes through the jobs domain) ---

    def api_upgrade_estimate(self, set_id: str) -> dict:
        with DatabaseManager(self._database_path) as database:
            return V2UpgradeService(database, runtime_config(), None).estimate(set_id)

    # --- smart organize workspace ---

    def api_read_organize_source(self, path: str) -> dict:
        source = Path(path)
        suffix = source.suffix.lower()
        if suffix == ".docx" and re.search(r"统计汇总", source.stem):
            raise ValueError(
                "bad_payload: 该文件只包含题量汇总，请选择文件名以“_题目集合.docx”结尾的学科文档")
        resolved = str(source.resolve())
        if suffix in {".xlsx", ".xlsm"}:
            return {"mode": "structured", "kind": "pharmacology-xlsx", "path": resolved,
                    "name": source.stem, "subject": ""}
        if is_medical_question_collection_docx(resolved):
            subject = re.sub(r"[_\-\s]*题目集合\s*$", "", source.stem).strip(" _-")
            return {"mode": "structured", "kind": "structured-docx", "path": resolved,
                    "name": source.stem, "subject": subject}
        return {"mode": "text", "text": extract_question_text(resolved),
                "name": source.stem, "subject": ""}

    def api_organize(self, token: str, raw_text: str = "", default_subject: str = "",
                     use_ai: bool = True, source_path: str = "") -> dict:
        if not str(raw_text).strip() and not source_path:
            raise ValueError("bad_payload: 请粘贴题目文本或读取题目文件")
        threading.Thread(
            target=self._run_organize,
            args=(str(token), raw_text, default_subject, bool(use_ai), source_path),
            daemon=True,
        ).start()
        return {"started": True, "token": str(token)}

    def _run_organize(self, token: str, raw_text: str, default_subject: str,
                      use_ai: bool, source_path: str) -> None:
        try:
            def progress(current: int, total: int) -> None:
                self.organize_progress.emit(json.dumps(
                    {"token": token, "current": current, "total": total}, ensure_ascii=False))

            if source_path:
                result = QuestionFormattingService().organize_file(
                    source_path, default_subject, progress)
            else:
                llm = None
                section = runtime_config(database_override=self._database_path)["llm"]
                if use_ai and section.get("api_key"):
                    from llm_client import LLMClient
                    llm = LLMClient(
                        section["api_key"], section["base_url"], section["model"],
                        section["temperature"], max(8000, int(section["max_tokens"])),
                        provider=section["provider"], protocol=section["protocol"],
                    )
                result = QuestionFormattingService(llm).organize(
                    raw_text, default_subject, use_ai, progress)
            self.organize_result.emit(json.dumps(
                {"token": token, "ok": True, "data": result}, ensure_ascii=False, default=str))
        except Exception as exc:  # noqa: BLE001 - report to UI, never crash the thread
            self.organize_result.emit(json.dumps(
                {"token": token, "ok": False,
                 "error": {"code": "organize_failed", "message": str(exc)}},
                ensure_ascii=False))

    def api_validate_rows(self, rows: list[dict], imported_ids: list[str] | None = None) -> dict:
        warnings = _row_warnings(list(rows or []), {str(item) for item in (imported_ids or [])})
        return {"warnings": warnings, "valid_count": sum(1 for item in warnings if not item)}

    def api_import_rows(self, name: str = "", rows: list[dict] | None = None,
                        source_path: str = "", set_id: str = "") -> dict:
        rows = list(rows or [])
        if not rows:
            raise ValueError("bad_payload: 当前没有可导入的题目")
        if not set_id and not (name or "").strip():
            raise ValueError("bad_payload: 请填写题目集名称")
        try:
            validated = validate_questions(rows)
        except ImportValidationError as exc:
            raise ValueError(f"import_invalid: {_summarize_errors(exc.errors)}") from exc
        with DatabaseManager(self._database_path) as database:
            try:
                if set_id:
                    database.append_questions_to_set(set_id, validated)
                else:
                    set_id = database.create_question_set(
                        name.strip(), source_path or "pasted://manual", "formatted", validated)
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"import_invalid: 题目 ID 与题目集中已有题目重复（{exc}）") from exc
        return {"set_id": set_id, "imported": len(validated)}

    def api_save_rows_json(self, path: str, rows: list[dict]) -> dict:
        try:
            validated = validate_questions(list(rows or []))
        except ImportValidationError as exc:
            raise ValueError(f"import_invalid: {_summarize_errors(exc.errors)}") from exc
        if not validated:
            raise ValueError("bad_payload: 当前没有可导出的题目")
        Path(path).write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": path, "count": len(validated)}

    def api_save_issue_report(self, path: str, rows: list[dict]) -> dict:
        rows = list(rows or [])
        warnings = _row_warnings(rows)
        invalid = [(index, errors) for index, errors in enumerate(warnings) if errors]
        if not invalid:
            raise ValueError("bad_state: 当前没有待调整题目")
        with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["题目序号", "源文件", "工作表", "源行号", "题目ID", "问题", "题干"])
            for index, errors in invalid:
                row = rows[index]
                extensions = row.get("extensions") if isinstance(row.get("extensions"), dict) else {}
                writer.writerow([
                    index + 1,
                    extensions.get("sourceDocument", ""), extensions.get("sourceSheet", ""),
                    extensions.get("sourceRow", ""), row.get("id", ""),
                    "；".join(errors), row.get("question", ""),
                ])
        return {"path": path, "count": len(invalid)}
