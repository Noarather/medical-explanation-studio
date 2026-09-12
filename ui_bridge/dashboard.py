"""Dashboard domain: aggregate counts and credential status."""
from __future__ import annotations

from credentials import CredentialStore
from db_manager import DatabaseManager
from ui_bridge.protocol import BridgeBase


class DashboardBridge(BridgeBase):
    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path

    def api_counts(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            return database.dashboard_counts()

    def api_api_status(self) -> dict:
        store = CredentialStore()
        with DatabaseManager(self._database_path) as database:
            provider = database.get_setting("llm_provider", "deepseek")
        return {
            "llm": store.configured(provider) if provider in {"deepseek", "claude", "gemini", "custom"} else False,
            "deepseek": store.configured("deepseek"),
            "dashscope": store.configured("dashscope"),
        }
