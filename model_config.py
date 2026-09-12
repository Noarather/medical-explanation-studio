"""User-selected text generation providers; secrets stay in CredentialStore."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

PROVIDERS = ("deepseek", "claude", "gemini", "custom")
PROTOCOLS = ("openai", "anthropic", "gemini")
MODEL_SETTINGS_DEFAULTS = {
    "llm_provider": "deepseek",
    "claude_base_url": "https://api.anthropic.com/v1", "claude_model": "", "claude_hard_model": "",
    "gemini_base_url": "https://generativelanguage.googleapis.com/v1beta", "gemini_model": "", "gemini_hard_model": "",
    "custom_base_url": "", "custom_model": "", "custom_hard_model": "", "custom_protocol": "openai",
}


def normalized_url(value: str, protocol: str = "openai") -> str:
    if protocol not in PROTOCOLS:
        raise ValueError("bad_protocol: 请选择支持的接口协议")
    try:
        parts = urlsplit(str(value).strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError("bad_url: API URL 格式无效") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("bad_url: API URL 必须以 http:// 或 https:// 开头")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("bad_url: URL 只填写接口地址，密钥请填入 API Key")
    path = parts.path.rstrip("/")
    endings = {"openai": "/chat/completions", "anthropic": "/messages", "gemini": "/models"}
    if protocol == "gemini":
        path = re.sub(r"/models/[^/]+:generateContent$", "", path)
    if path.endswith(endings[protocol]):
        path = path[:-len(endings[protocol])]
    if not path:
        path = "/v1beta" if protocol == "gemini" else "/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def select_model(settings: dict, store, legacy: dict | None = None, *, provider: str | None = None, api_key: str | None = None, require_ready: bool = False) -> dict:
    values = {**MODEL_SETTINGS_DEFAULTS, **settings}
    provider = provider or str(values.get("llm_provider") or "deepseek")
    if provider not in PROVIDERS:
        raise ValueError("bad_provider: 不支持的模型服务")
    protocol = {"deepseek": "openai", "claude": "anthropic", "gemini": "gemini"}.get(provider, values["custom_protocol"])
    if protocol not in PROTOCOLS:
        raise ValueError("bad_protocol: 不支持的接口协议")
    legacy = legacy or {}
    url = str(values.get(f"{provider}_base_url", legacy.get("base_url", ""))).strip()
    model = str(values.get(f"{provider}_model", legacy.get("model", ""))).strip()
    key = api_key.strip() if api_key is not None else (store.get(provider) or (legacy.get("api_key", "") if provider == "deepseek" else ""))
    if url:
        url = normalized_url(url, protocol)
    if require_ready and (not url or not model or not key):
        raise ValueError("missing_config: 请填写 API URL、API Key 和模型名称")
    if any(c in model for c in "\r\n?#"):
        raise ValueError("bad_model: 模型名称格式无效")
    return {**legacy, "provider": provider, "protocol": protocol, "base_url": url, "model": model, "api_key": key,
            "temperature": legacy.get("temperature", .3), "max_tokens": legacy.get("max_tokens", 2200),
            "hard_model": str(values.get("generation_hard_model" if provider == "deepseek" else f"{provider}_hard_model", "")).strip()}


def safe_error(error: object, api_key: str = "") -> str:
    message = str(error)
    if api_key:
        message = message.replace(api_key, "[已隐藏密钥]")
    message = re.sub(r"(?i)(bearer\s+|[?&](?:key|api_key)=)[^\s\"&]+", r"\1[已隐藏密钥]", message)
    return message[:800]


def require_key_for_new_endpoint(before: dict, after: dict, provider: str, supplied_key: str) -> None:
    """A saved credential must not silently follow a URL draft to another endpoint."""
    key = f"{provider}_base_url"
    def canonical(values):
        protocol = {"claude": "anthropic", "gemini": "gemini", "custom": values.get("custom_protocol", "openai")}.get(provider, "openai")
        url = str(values.get(key, "")).strip()
        return normalized_url(url, protocol) if url else ""
    old, new = canonical(before), canonical(after)
    if old != new and not supplied_key.strip():
        raise ValueError("key_required: 更换 API 地址时，请重新填写该服务的 API Key")
