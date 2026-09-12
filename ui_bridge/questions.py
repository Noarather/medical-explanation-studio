"""Questions domain: cross-set question browsing, bulk operations and trash."""
from __future__ import annotations

import json

from db_manager import DatabaseManager
from question_format_v2 import make_envelope, normalize_question_v2, write_json_v2
from ui_bridge.protocol import BridgeBase


class QuestionsBridge(BridgeBase):
    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path

    # --- browsing ---

    def api_list(self, set_id: str = "", review_status: str = "", pipeline_status: str = "",
                 subject: str = "", tag: str = "", question_source: str = "", search: str = "",
                 limit: int = 200, offset: int = 0) -> dict:
        limit = min(500, max(1, int(limit)))
        offset = max(0, int(offset))
        with DatabaseManager(self._database_path) as database:
            rows = database.list_questions_global(
                set_id, review_status, pipeline_status, subject, tag, question_source,
                search, limit, offset)
            total = database.count_questions_global(
                set_id, review_status, pipeline_status, subject, tag, question_source, search)
        return {"rows": rows, "total": total}

    def api_ids(self, set_id: str = "", review_status: str = "", pipeline_status: str = "",
                subject: str = "", tag: str = "", question_source: str = "", search: str = "") -> dict:
        with DatabaseManager(self._database_path) as database:
            ids = database.list_question_ids_global(
                set_id, review_status, pipeline_status, subject, tag, question_source, search)
        return {"ids": ids}

    def api_filters(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            return {
                "sets": database.list_question_sets(),
                "subjects": database.all_question_subjects(),
                "sources": database.all_question_sources(),
                "tags": database.all_tag_labels(),
            }

    def api_detail(self, question_pk: int) -> dict:
        with DatabaseManager(self._database_path) as database:
            row = database.get_imported_question(int(question_pk))
        if not row:
            raise ValueError("not_found: 题目不存在")
        return row

    # --- bulk operations ---

    def api_bulk_set_subject(self, question_pks: list[int], subject: str) -> dict:
        ids = sorted({int(item) for item in (question_pks or [])})
        if not ids:
            raise ValueError("bad_payload: 请先选择题目")
        if not (subject or "").strip():
            raise ValueError("bad_payload: 请填写学科名称")
        with DatabaseManager(self._database_path) as database:
            updated = database.bulk_update_question_subject(ids, subject.strip())
        return {"updated": updated}

    def api_move_to_trash(self, question_pks: list[int]) -> dict:
        ids = sorted({int(item) for item in (question_pks or [])})
        if not ids:
            raise ValueError("bad_payload: 请先选择题目")
        with DatabaseManager(self._database_path) as database:
            moved = database.move_questions_to_trash(ids)
        return {"moved": moved}

    def api_delete(self, question_pks: list[int]) -> dict:
        ids = sorted({int(item) for item in (question_pks or [])})
        if not ids:
            raise ValueError("bad_payload: 请先选择题目")
        with DatabaseManager(self._database_path) as database:
            deleted = database.delete_imported_questions(ids)
        return {"deleted": deleted}

    def api_export_selected(self, question_pks: list[int], path: str) -> dict:
        ids = sorted({int(item) for item in (question_pks or [])})
        if not ids:
            raise ValueError("bad_payload: 请先选择要导出的题目")
        if not (path or "").strip():
            raise ValueError("bad_payload: 缺少保存路径")
        questions = []
        with DatabaseManager(self._database_path) as database:
            for question_pk in ids:
                row = database.get_imported_question(question_pk)
                if not row:
                    continue
                raw = row["raw"]
                raw["bank"] = raw.get("bank") or "school"
                raw["explanation"] = row["explanation"] or raw.get("explanation") or ""
                raw["explanationMeta"] = {
                    **(raw.get("explanationMeta") or {}),
                    "mode": row["generation_mode"],
                    "score": row["match_score"],
                    "evidence": row["evidence"],
                    "reviewedAt": row["reviewed_at"],
                }
                questions.append(normalize_question_v2(raw))
        if not questions:
            raise ValueError("not_found: 所选题目不存在")
        envelope = make_envelope(questions, name="题目管理导出", source="med-explain")
        write_json_v2(path, envelope)
        return {"path": path, "count": len(questions)}

    # --- trash (30-day recoverable deletion) ---

    def api_trash_list(self, search: str = "", limit: int = 200, offset: int = 0) -> dict:
        limit = min(500, max(1, int(limit)))
        offset = max(0, int(offset))
        with DatabaseManager(self._database_path) as database:
            database.purge_expired_trash()
            if search:
                # db 搜索只覆盖 external_id/subject/set_name，题干需先还原快照再过滤
                rows = database.list_trash("", 10000, 0)
                total = 0
            else:
                rows = database.list_trash("", limit, offset)
                total = database.count_trash("")
        for row in rows:
            snapshot = json.loads(row.pop("snapshot_json") or "{}")
            row.pop("review_actions_json", None)
            row["prompt_text"] = snapshot.get("prompt_text", "")
        if search:
            needle = str(search).lower()
            rows = [
                row for row in rows
                if needle in str(row.get("external_id", "")).lower()
                or needle in str(row.get("subject", "")).lower()
                or needle in str(row.get("set_name", "")).lower()
                or needle in str(row.get("prompt_text", "")).lower()
            ]
            total = len(rows)
            rows = rows[offset:offset + limit]
        return {"rows": rows, "total": total}

    def api_trash_restore(self, trash_ids: list[int]) -> dict:
        ids = sorted({int(item) for item in (trash_ids or [])})
        if not ids:
            raise ValueError("bad_payload: 请先选择要恢复的题目")
        with DatabaseManager(self._database_path) as database:
            return database.restore_trash(ids)

    def api_trash_purge(self, trash_ids: list[int]) -> dict:
        ids = sorted({int(item) for item in (trash_ids or [])})
        if not ids:
            raise ValueError("bad_payload: 请先选择要彻底清除的题目")
        with DatabaseManager(self._database_path) as database:
            purged = database.purge_trash(ids)
        return {"purged": purged}
