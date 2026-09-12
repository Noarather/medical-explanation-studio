"""API credential access backed by Windows Credential Manager via keyring."""

from __future__ import annotations

import os


SERVICE_NAME = "MedExplainStudio"
ENV_NAMES = {
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "custom": "CUSTOM_LLM_API_KEY",
}


class CredentialStore:
    def __init__(self) -> None:
        try:
            import keyring  # type: ignore
        except ImportError:
            keyring = None
        self._keyring = keyring

    def get(self, provider: str) -> str:
        env_value = os.getenv(ENV_NAMES[provider], "").strip()
        if env_value:
            return env_value
        if self._keyring is None:
            return ""
        try:
            return (self._keyring.get_password(SERVICE_NAME, provider) or "").strip()
        except Exception:
            return ""

    def set(self, provider: str, value: str) -> None:
        if self._keyring is None:
            raise RuntimeError("未安装 keyring，无法写入 Windows 凭据管理器")
        value = value.strip()
        if value:
            self._keyring.set_password(SERVICE_NAME, provider, value)
        else:
            try:
                self._keyring.delete_password(SERVICE_NAME, provider)
            except Exception:
                pass

    def configured(self, provider: str) -> bool:
        return bool(self.get(provider))
