"""Library domain: textbook libraries, page-offset calibration and cloud notice."""
from __future__ import annotations

from PySide6.QtWidgets import QFileDialog

from db_manager import DatabaseManager
from index_profile import build_index_profile, index_fingerprint
from runtime_config import runtime_config
from ui_bridge.protocol import BridgeBase
from parser_health import index_error_summary

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

    def api_list(self) -> dict:
        current = index_fingerprint(build_index_profile(runtime_config()))
        with DatabaseManager(self._database_path) as database:
            libraries = database.list_libraries()
        for row in libraries:
            row["index_state"] = _index_state(row, current)
            row["file_error_summary"] = index_error_summary(row.get("file_error") or "")
        return {"libraries": libraries, "current_fingerprint": current}

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
