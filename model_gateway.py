"""Small native REST adapters for Anthropic Messages and Gemini generateContent."""
from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from model_config import normalized_url, safe_error


class GatewayError(RuntimeError):
    def __init__(self, message, status_code=0, retry_after=None):
        super().__init__(message)
        self.status_code, self.retry_after = status_code, retry_after


def complete_native(*, protocol, base_url, api_key, model, system, prompt, temperature, max_tokens, timeout, client=None):
    base = normalized_url(base_url, protocol)
    if protocol == "anthropic":
        url = base + "/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body = {"model": model, "system": system, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
        # Temperature is omitted for native Claude: some model families require the default.
    elif protocol == "gemini":
        url = base + "/models/" + quote(model.removeprefix("models/"), safe="") + ":generateContent"
        headers = {"x-goog-api-key": api_key}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}}
    else:
        raise ValueError("不支持的原生接口")
    owned = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False)
    started = time.monotonic()
    try:
        response = client.post(url, headers=headers, json=body)
        if not response.is_success:
            try:
                details = response.json().get("error", {})
                message = details.get("message", "") if isinstance(details, dict) else str(details)
            except (ValueError, AttributeError):
                message = "服务返回了非 JSON 错误；请核对接口地址"
            raise GatewayError(f"HTTP {response.status_code}: {safe_error(message, api_key)}", response.status_code, response.headers.get("retry-after"))
        try:
            data = response.json()
            if protocol == "anthropic":
                text = "\n".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
                usage = data.get("usage") or {}
                finish = "length" if data.get("stop_reason") == "max_tokens" else data.get("stop_reason", "stop")
                inputs, outputs = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
                cached, thoughts = usage.get("cache_read_input_tokens", 0), 0
            else:
                candidate = (data.get("candidates") or [{}])[0]
                text = "\n".join(x.get("text", "") for x in (candidate.get("content") or {}).get("parts", []) if not x.get("thought"))
                usage = data.get("usageMetadata") or {}
                finish = "length" if candidate.get("finishReason") == "MAX_TOKENS" else candidate.get("finishReason", "stop").lower()
                inputs, outputs = usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
                cached, thoughts = usage.get("cachedContentTokenCount", 0), usage.get("thoughtsTokenCount", 0)
        except (ValueError, AttributeError, TypeError) as exc:
            raise GatewayError("服务返回格式不匹配，请核对所选 API 协议") from exc
        if not text.strip():
            reason = finish or "empty"
            raise GatewayError(f"模型未返回可用文本（{reason}），请检查模型、输出限制或服务端拦截")
        return text.strip(), {"httpStatus": response.status_code, "requestId": str(data.get("id") or data.get("responseId") or ""),
            "finishReason": finish, "model": str(data.get("model") or data.get("modelVersion") or model),
            "elapsedMs": int((time.monotonic()-started)*1000), "promptTokens": int(inputs or 0), "completionTokens": int(outputs or 0),
            "cachedPromptTokens": int(cached or 0), "reasoningTokens": int(thoughts or 0), "thinking": bool(thoughts),
            "cacheStatus": "hit" if cached else "miss"}
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise GatewayError("连接超时或网络不可达，请检查 URL、代理和网络") from exc
    finally:
        if owned:
            client.close()
