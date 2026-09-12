"""Settings domain: app_settings table plus Windows credential store."""
from __future__ import annotations

import json
import threading
import time
import uuid

from PySide6.QtCore import Signal

from credentials import CredentialStore
from model_config import MODEL_SETTINGS_DEFAULTS, PROVIDERS, normalized_url, select_model, safe_error, require_key_for_new_endpoint
from db_manager import DatabaseManager
from ui_bridge.protocol import BridgeBase

# Keys mirror main.py resolved_config exactly; defaults mirror config.yaml.
SETTINGS_DEFAULTS: dict[str, str] = {
    **MODEL_SETTINGS_DEFAULTS,
    "deepseek_model": "deepseek-v4-flash",
    "deepseek_base_url": "https://api.deepseek.com/v1",
    "embedding_model": "text-embedding-v4",
    "ocr_model": "qwen-vl-ocr",
    "dashscope_base_url": "https://dashscope.aliyuncs.com/api/v1",
    "similarity_threshold": "0.5",
    "rerank_enabled": "true",
    "rerank_model": "qwen3-rerank",
    "rerank_candidate_count": "20",
    "rerank_timeout_seconds": "30",
    "batch_process_size": "50",
    "generation_concurrency": "8",
    "generation_adaptive_concurrency": "true",
    "generation_smart_routing": "true",
    "generation_hard_use_pro": "true",
    "generation_force_flash": "false",
    "generation_hard_model": "deepseek-v4-pro",
    "generation_pro_concurrency": "2",
    "automatic_general_fallback": "false",
}


class SettingsBridge(BridgeBase):
    api_test_result = Signal(str)

    def __init__(self, database_path: str, parent=None) -> None:
        super().__init__(parent)
        self._database_path = database_path

    def api_build_info(self) -> dict:
        from build_metadata import build_info
        return build_info()

    def api_get(self) -> dict:
        with DatabaseManager(self._database_path) as database:
            settings = {
                key: database.get_setting(key, default)
                for key, default in SETTINGS_DEFAULTS.items()
            }
        store = CredentialStore()
        return {
            "settings": settings,
            "credentials": {provider: store.configured(provider) for provider in (*PROVIDERS, "dashscope")},
        }

    def api_save(self, settings: dict, credentials: dict) -> dict:
        self._validate_keys(settings)
        unknown = set(credentials or {}) - set((*PROVIDERS, "dashscope"))
        if unknown:
            raise ValueError("bad_provider: 不支持的密钥服务")
        store = CredentialStore()
        before = self.api_get()["settings"]
        merged = {**before, **(settings or {})}
        for provider in (*PROVIDERS, "dashscope"):
            supplied = str((credentials or {}).get(provider, "")).strip()
            if store.configured(provider):
                require_key_for_new_endpoint(before, merged, provider, supplied)
            if provider in PROVIDERS:
                select_model(merged, store, provider=provider)
            elif merged.get("dashscope_base_url"):
                normalized_url(merged["dashscope_base_url"])
        select_model(merged, store)  # Validate active provider as well.
        # Persist secrets first: a failed credential write must not activate new settings.
        for provider in (*PROVIDERS, "dashscope"):
            value = str((credentials or {}).get(provider, "")).strip()
            if value:
                store.set(provider, value)
        with DatabaseManager(self._database_path) as database:
            for key, value in (settings or {}).items():
                database.set_setting(key, str(value))
        return {"saved": True}

    @staticmethod
    def _validate_keys(settings: dict | None) -> None:
        unknown = sorted(set(settings or {}) - set(SETTINGS_DEFAULTS))
        if unknown:
            raise ValueError(f"bad_settings: 未知设置键 {', '.join(unknown)}")

    def api_test(self, provider: str, settings: dict | None = None, api_key: str = "", request_id: str = "") -> dict:
        provider = str(provider)
        if provider not in {*PROVIDERS, "dashscope"}:
            raise ValueError(f"bad_provider: 不支持的服务 {provider}")
        self._validate_keys(settings)
        before = self.api_get()["settings"]
        draft = {**before, **(settings or {})}
        store = CredentialStore()
        if store.configured(provider):
            require_key_for_new_endpoint(before, draft, provider, api_key)
        key = api_key.strip() or store.get(provider)
        token = str(request_id or uuid.uuid4())
        # Capture the draft now; the worker never reads or saves changed settings.
        threading.Thread(target=self._run_test, args=(provider, draft, key, token), daemon=True).start()
        return {"started": True, "request_id": token}

    def _run_test(self, provider: str, settings: dict, api_key: str, request_id: str) -> None:
        started = time.monotonic()
        result = {"provider": provider, "request_id": request_id, "ok": False}
        try:
            if provider in PROVIDERS:
                from llm_client import LLMClient
                section = select_model(settings, CredentialStore(), provider=provider, api_key=api_key, require_ready=True)
                client = LLMClient(api_key, section["base_url"], section["model"], max_tokens=1024,
                                   timeout_seconds=20, provider=provider, protocol=section["protocol"])
                try:
                    content = client._complete("Reply with exactly OK.", thinking=False)
                finally:
                    http_client = getattr(client._local, "client", None)
                    if http_client is not None:
                        http_client.close()
                result["model"] = section["model"]
                message = f"模型 {section['model']} 调用成功：{safe_error(content[:100], api_key)}"
            else:
                import httpx
                if not api_key or not settings.get("embedding_model", "").strip():
                    raise ValueError("请填写 DashScope API Key 和 Embedding 模型")
                base = normalized_url(settings["dashscope_base_url"])
                with httpx.Client(timeout=20, follow_redirects=False) as client:
                    response = client.post(base + "/services/embeddings/text-embedding/text-embedding",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": settings["embedding_model"], "input": {"texts": ["ping"]}})
                if not response.is_success:
                    raise RuntimeError(f"HTTP {response.status_code}: {safe_error(response.text, api_key)}")
                if not (response.json().get("output") or {}).get("embeddings"):
                    raise ValueError("接口未返回向量，请检查 DashScope 地址和模型")
                message = "DashScope 向量模型调用成功"
            result.update(ok=True, message=message)
        except Exception as exc:  # noqa: BLE001 - report to UI, never crash the thread
            message = safe_error(exc, api_key)
            status = getattr(exc, "status_code", 0)
            hint = {401: "请检查 API Key", 402: "请检查账户余额", 403: "请检查服务权限或地区限制",
                    404: "请检查 URL 和模型名称", 429: "请求过多或额度不足，稍后重试"}.get(status, "")
            result["message"] = f"{hint}；{message}" if hint else message
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        self.api_test_result.emit(json.dumps(result, ensure_ascii=False))

    def api_parser_health(self) -> dict:
        from parser_health import parser_health
        return parser_health()
