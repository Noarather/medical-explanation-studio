"""Review domain: review workbench main chain (list/open/save/review/regenerate)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from db_manager import DatabaseManager
from services import (MemoryCardBackfillService, StudyPointBackfillService, TagBackfillService)
from ui_bridge.protocol import BridgeBase

MAX_PDF_BASE64_BYTES = 60 * 1024 * 1024


class ReviewBridge(BridgeBase):
    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path

    def api_auto_review(self, set_id: str = "", enabled: bool | None = None) -> dict:
        from auto_review import run
        with DatabaseManager(self._database_path) as database:
            if enabled is not None:
                database.set_setting("review.auto_enabled", "1" if enabled else "0")
            return run(database, set_id)

    def api_list(self, set_id: str = "", review_status: str = "", pipeline_status: str = "",
                 search: str = "", limit: int = 200, offset: int = 0) -> dict:
        limit = min(500, max(1, int(limit)))
        offset = max(0, int(offset))
        with DatabaseManager(self._database_path) as database:
            rows = database.list_questions_global(
                set_id, review_status, pipeline_status, "", "", "", search, limit, offset)
            total = database.count_questions_global(
                set_id, review_status, pipeline_status, "", "", "", search)
            from auto_review import reasons_for
            reasons = reasons_for(database, [row["id"] for row in rows])
            for row in rows:
                row["review_reason"] = reasons.get(row["id"], "")
        return {"rows": rows, "total": total}

    def api_open(self, question_pk: int) -> dict:
        with DatabaseManager(self._database_path) as database:
            row = database.get_imported_question(int(question_pk))
            if not row:
                raise ValueError("not_found: 题目不存在")
            database.mark_question_viewed(int(question_pk))
        return row

    def api_save(self, question_pk: int, explanation_blocks: list[dict],
                 knowledge_points: list | None = None, tags: list | None = None,
                 suggested_tags: list | None = None, mnemonic: str | None = None,
                 brief_explanation: str | None = None, subject: str = "") -> dict:
        with DatabaseManager(self._database_path) as database:
            current = database.get_imported_question(int(question_pk))
            if not current:
                raise ValueError("not_found: 题目不存在")
            if subject and subject.strip() and subject.strip() != current["subject"]:
                database.update_question_subject(int(question_pk), subject.strip())
            raw = current["raw"]
            result = database.update_question_exchange_fields(
                int(question_pk),
                explanation_blocks=list(explanation_blocks or []),
                knowledge_points=_take(knowledge_points, raw.get("knowledgePoints")),
                tags=_take(tags, raw.get("tags")),
                suggested_tags=_take(suggested_tags, raw.get("suggestedTags")),
                mnemonic=str(mnemonic if mnemonic is not None else raw.get("mnemonic") or ""),
                brief_explanation=str(
                    brief_explanation if brief_explanation is not None
                    else raw.get("briefExplanation") or ""),
            )
        return {"saved": True, "question": result}

    def api_review(self, question_pk: int, action: str, explanation: str = "", note: str = "") -> dict:
        if action not in {"approved", "rejected", "pending"}:
            raise ValueError("bad_payload: 无效的审核动作")
        with DatabaseManager(self._database_path) as database:
            try:
                database.review_question(int(question_pk), action, str(explanation or ""), str(note or ""))
            except ValueError as exc:
                raise ValueError(f"bad_state: {exc}") from exc
        return {"reviewed": action}

    def api_regenerate(self, question_pk: int, library_ids: list | None = None) -> dict:
        with DatabaseManager(self._database_path) as database:
            current = database.get_imported_question(int(question_pk))
            if not current:
                raise ValueError("not_found: 题目不存在")
            database.queue_question(int(question_pk))
            payload = {"set_id": current["set_id"], "question_ids": [int(question_pk)]}
            if library_ids:
                payload["library_ids"] = [int(item) for item in library_ids]
            job_id = database.create_job("generate", f"重新生成：{current['external_id']}", payload)
        return {"job_id": job_id}

    # --- quick review ---

    def api_quick_review_preview(self, set_id: str = "",
                                 minimum_score: float | None = None) -> dict:
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 快速审核需要先选择一个题目集")
        with DatabaseManager(self._database_path) as database:
            if minimum_score is None:
                minimum_score = float(database.get_setting("quick_review_min_score", "0.75"))
            return database.quick_review_candidates(set_id.strip(), float(minimum_score))

    def api_quick_review_approve(self, set_id: str = "", minimum_score: float = 0.75,
                                 expected_ids: list | None = None) -> dict:
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 快速审核需要先选择一个题目集")
        ids = [int(item) for item in (expected_ids or [])]
        with DatabaseManager(self._database_path) as database:
            result = database.bulk_approve_quick_review(
                set_id.strip(), float(minimum_score), ids or None)
            database.set_setting("quick_review_min_score", f"{float(minimum_score):.2f}")
        return result

    # --- general-knowledge generation ---

    def api_general_authorize(self, question_pk: int) -> dict:
        with DatabaseManager(self._database_path) as database:
            current = database.get_imported_question(int(question_pk))
            if not current:
                raise ValueError("not_found: 题目不存在")
            if current["pipeline_status"] not in {"unmatched", "no_library"}:
                raise ValueError("bad_state: 只有未匹配或缺少教材的题目可以授权通识生成。")
            job_id = database.create_job(
                "general", f"通识生成：{current['external_id']}",
                {"question_pk": int(question_pk)})
        return {"job_id": job_id}

    def api_general_batch_preview(self, set_id: str = "",
                                  question_ids: list | None = None) -> dict:
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 批量通识生成需要先选择一个题目集")
        ids = [int(item) for item in question_ids] if question_ids else None
        with DatabaseManager(self._database_path) as database:
            rows = database.general_generation_questions(set_id.strip(), ids)
        return {"count": len(rows)}

    # --- batch regenerate ---

    def api_regenerate_batch(self, set_id: str = "", question_ids: list | None = None,
                             library_ids: list | None = None) -> dict:
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 批量重新生成需要先选择一个题目集")
        ids = sorted({int(item) for item in (question_ids or [])})
        if not ids:
            raise ValueError("bad_payload: 请先选择题目")
        with DatabaseManager(self._database_path) as database:
            database.bulk_queue_questions(ids)
            payload: dict = {"set_id": set_id.strip(), "question_ids": ids}
            if library_ids:
                payload["library_ids"] = [int(item) for item in library_ids]
            job_id = database.create_job("generate", f"批量重新生成：{len(ids)} 题", payload)
        return {"job_id": job_id, "count": len(ids)}

    # --- backfill tasks ---

    _BACKFILL_SERVICES = {
        "tags": TagBackfillService,
        "study_points": StudyPointBackfillService,
        "memory_cards": MemoryCardBackfillService,
    }

    def api_backfill_estimate(self, kind: str = "", set_id: str = "") -> dict:
        service_class = self._BACKFILL_SERVICES.get(str(kind))
        if service_class is None:
            raise ValueError("bad_payload: 未知的补齐类型")
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 补齐任务需要先选择一个题目集")
        with DatabaseManager(self._database_path) as database:
            estimate = service_class(database, None).estimate(set_id.strip())
        return {**estimate, "kind": str(kind)}

    # --- generation library selection persistence ---

    def api_get_generation_libraries(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            raw = database.get_setting("generation_library_ids", "[]")
        try:
            ids = [int(item) for item in json.loads(raw)]
        except (ValueError, TypeError):
            ids = []
        return {"library_ids": ids}

    def api_save_generation_libraries(self, library_ids: list | None = None) -> dict:
        ids = sorted({int(item) for item in (library_ids or [])})
        with DatabaseManager(self._database_path) as database:
            database.set_setting("generation_library_ids", json.dumps(ids))
        return {"saved": True}

    # --- PDF preview (pdf.js on the frontend) ---

    def api_pdf_info(self, path: str) -> dict:
        record = self._registered_pdf(path)
        return {"path": str(path), "page_count": record["page_count"], "size": record["file_size"]}

    def api_read_pdf_base64(self, path: str) -> dict:
        record = self._registered_pdf(path)
        size = int(record["file_size"] or 0)
        if size > MAX_PDF_BASE64_BYTES:
            raise ValueError("bad_state: 教材文件过大，请使用 file:// 直接加载")
        data = Path(path).read_bytes()
        return {"base64": base64.b64encode(data).decode("ascii"), "size": len(data)}

    def _registered_pdf(self, path: str) -> dict:
        if not (path or "").strip():
            raise ValueError("bad_payload: 缺少文件路径")
        with DatabaseManager(self._database_path) as database:
            record = database.get_file_by_path(str(path))
        if not record:
            raise ValueError("not_found: 该文件不是已登记的教材 PDF")
        return record


def _take(value: list | None, fallback) -> list:
    items = value if value is not None else (fallback or [])
    return [str(item) for item in list(items)][:3]
