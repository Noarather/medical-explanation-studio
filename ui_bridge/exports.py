"""Exports domain: v2 export, issue report, history and output folder."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QFileDialog

from app_paths import default_export_dir
from db_manager import DatabaseManager
from services import ExportService
from ui_bridge.protocol import BridgeBase


def _map_export_error(exc: ValueError) -> ValueError:
    message = str(exc)
    code = "not_found" if "不存在" in message else "bad_state"
    return ValueError(f"{code}: {message}")


class ExportsBridge(BridgeBase):
    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path

    def api_default_dir(self) -> dict:
        return {"path": str(default_export_dir())}

    def api_preflight(self, set_id: str, incremental: bool = True) -> dict:
        from incremental_export import preflight
        with DatabaseManager(self._database_path) as database:
            result = preflight(database, set_id, incremental)
            result.pop("questions")
            return result

    def api_export_incremental(self, set_id: str, output_dir: str = "") -> dict:
        from incremental_export import export
        with DatabaseManager(self._database_path) as database:
            return export(database, set_id, output_dir.strip() or str(default_export_dir()))

    def api_pick_directory(self, current: str = "") -> dict:
        path = QFileDialog.getExistingDirectory(
            None, "选择导出目录", current or str(default_export_dir()))
        return {"path": path or ""}

    def api_export(self, set_id: str = "", output_dir: str = "",
                   split_by_subject: bool = False) -> dict:
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 请先选择题目集")
        target = (output_dir or "").strip() or str(default_export_dir())
        Path(target).mkdir(parents=True, exist_ok=True)
        with DatabaseManager(self._database_path) as database:
            try:
                from auto_review import run
                run(database, set_id.strip())
                return ExportService(database).export(
                    set_id.strip(), target, bool(split_by_subject))
            except ValueError as exc:
                raise _map_export_error(exc) from exc

    def api_export_issues(self, set_id: str = "", output_dir: str = "") -> dict:
        if not (set_id or "").strip():
            raise ValueError("bad_payload: 请先选择题目集")
        target = (output_dir or "").strip() or str(default_export_dir())
        with DatabaseManager(self._database_path) as database:
            try:
                path = ExportService(database).export_issues(set_id.strip(), target)
            except ValueError as exc:
                raise _map_export_error(exc) from exc
        return {"path": path}

    def api_history(self, set_id: str = "", limit: int = 30) -> dict:
        try:
            limit_int = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("bad_payload: 无效的 limit 参数") from exc
        limit = min(100, max(1, limit_int))
        with DatabaseManager(self._database_path) as database:
            return {"records": database.list_export_records((set_id or "").strip(), limit)}

    def api_open_folder(self, path: str = "") -> dict:
        target = Path((path or "").strip() or default_export_dir())
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))  # Windows-only desktop app
        return {"opened": True, "path": str(target)}
