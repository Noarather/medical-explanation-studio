"""Tags domain: tag catalog management (rename/activate/merge/delete)."""
from __future__ import annotations

from db_manager import DatabaseManager
from ui_bridge.protocol import BridgeBase


def _map_tag_error(exc: ValueError) -> ValueError:
    message = str(exc)
    if "不存在" in message:
        return ValueError(f"not_found: {message}")
    if "3 道题" in message:
        return ValueError(f"bad_state: {message}")
    return ValueError(f"bad_payload: {message}")


class TagsBridge(BridgeBase):
    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path

    def api_list(self, set_id: str = "", subject: str = "", status: str = "",
                 search: str = "") -> dict:
        if not (set_id or "").strip():
            # list_question_tags 会先跑 sync_question_tags；空 set_id 会把全局标签
            # usage 重算为 0 并全部降级，必须拒绝。
            raise ValueError("bad_payload: 请先选择题目集")
        with DatabaseManager(self._database_path) as database:
            rows = database.list_question_tags(set_id.strip(), subject, status)
        needle = (search or "").strip().casefold()
        if needle:
            rows = [
                row for row in rows
                if needle in row["label"].casefold()
                or any(needle in str(alias).casefold() for alias in row.get("aliases") or [])
            ]
        return {"tags": rows}

    def api_subjects(self, set_id: str) -> dict:
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 请先选择题目集")
        with DatabaseManager(self._database_path) as database:
            return {"subjects": database.question_subjects(set_id.strip())}

    def api_rename(self, tag_id: int, label: str = "") -> dict:
        with DatabaseManager(self._database_path) as database:
            try:
                database.update_question_tag(int(tag_id), label=str(label or ""))
            except ValueError as exc:
                raise _map_tag_error(exc) from exc
        return {"updated": True}

    def api_activate(self, tag_id: int) -> dict:
        with DatabaseManager(self._database_path) as database:
            try:
                database.update_question_tag(int(tag_id), status="active")
            except ValueError as exc:
                raise _map_tag_error(exc) from exc
        return {"updated": True}

    def api_merge(self, source_id: int, target_id: int) -> dict:
        try:
            source_int = int(source_id)
            target_int = int(target_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("bad_payload: 无效的标签 ID") from exc
        with DatabaseManager(self._database_path) as database:
            target = database.get_question_tag(target_int)
            if not target:
                raise ValueError("not_found: 目标标签不存在")
            if target["status"] != "active":
                # 合并到 candidate 会绕过 ≥3 题门槛、合并到 merged 行会复活旧行。
                raise ValueError("bad_state: 只能合并到正式标签")
            try:
                changed = database.merge_question_tags(source_int, target_int)
            except ValueError as exc:
                raise _map_tag_error(exc) from exc
        return {"changed": changed}

    def api_delete(self, tag_id: int, confirm_label: str = "") -> dict:
        try:
            tag_int = int(tag_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("bad_payload: 无效的标签 ID") from exc
        with DatabaseManager(self._database_path) as database:
            row = database.get_question_tag(tag_int)
            if not row:
                raise ValueError("not_found: 标签不存在")
            if str(confirm_label or "").strip() != row["label"]:
                raise ValueError("bad_payload: 确认名称与标签不一致")
            result = database.delete_question_tag(tag_int)
        return {"deleted": result["label"], "changed": result["changed"]}
