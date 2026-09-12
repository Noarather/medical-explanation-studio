"""Qwen-VL-OCR provider for image-only textbook pages."""

from __future__ import annotations

import time
from http import HTTPStatus
from pathlib import Path

import dashscope


class QwenOCRClient:
    def __init__(self, api_key: str, model: str = "qwen-vl-ocr", base_url: str = ""):
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY 未配置")
        self.api_key = api_key
        self.model = model
        if base_url:
            dashscope.base_http_api_url = base_url.rstrip("/")

    def recognize_image(self, path: str | Path, max_retries: int = 3) -> str:
        image_uri = Path(path).resolve().as_uri()
        messages = [{
            "role": "user",
            "content": [
                {"image": image_uri},
                {"text": "请仅输出图像中的教材正文，保持原有阅读顺序，不要补充、总结或解释。"},
            ],
        }]
        last_error = "未知错误"
        for attempt in range(max_retries):
            try:
                response = dashscope.MultiModalConversation.call(
                    api_key=self.api_key,
                    model=self.model,
                    messages=messages,
                )
                if getattr(response, "status_code", None) == HTTPStatus.OK:
                    content = response.output.choices[0].message.content
                    if content and isinstance(content[0], dict):
                        return str(content[0].get("text") or "").strip()
                    return str(content or "").strip()
                last_error = f"{getattr(response, 'code', '')}: {getattr(response, 'message', '')}"
            except Exception as exc:
                last_error = str(exc)
            if attempt + 1 < max_retries:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"OCR 调用失败：{last_error}")

