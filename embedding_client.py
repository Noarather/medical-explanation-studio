"""DashScope text-embedding-v4 client with batching and exponential retry.

Dependencies: dashscope.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import Iterable

import dashscope


class EmbeddingClient:
    def __init__(self, api_key: str, model: str = "text-embedding-v4", dimension: int = 1024, batch_size: int = 10, base_url: str = ""):
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY is not configured")
        dashscope.api_key = api_key
        if base_url:
            dashscope.base_http_api_url = base_url.rstrip("/")
        self.model = model
        self.dimension = int(dimension)
        self.batch_size = int(batch_size)

    def get_embeddings(self, text_list: Iterable[str], max_retries: int = 3) -> list[list[float]]:
        texts = [str(text) for text in text_list]
        if not texts:
            return []
        results: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            results.extend(self._request_batch(batch, max_retries))
        return results

    def _request_batch(self, batch: list[str], max_retries: int) -> list[list[float]]:
        last_error = "unknown error"
        for attempt in range(max_retries):
            try:
                response = dashscope.TextEmbedding.call(
                    model=self.model,
                    input=batch,
                    dimension=self.dimension,
                )
                if response.status_code == HTTPStatus.OK:
                    items = sorted(response.output["embeddings"], key=lambda item: item.get("text_index", 0))
                    vectors = [item["embedding"] for item in items]
                    if len(vectors) != len(batch):
                        raise RuntimeError("DashScope returned an incomplete embedding batch")
                    return vectors
                last_error = f"{response.code}: {response.message}"
            except Exception as exc:  # network and SDK errors share retry policy
                last_error = str(exc)
            if attempt + 1 < max_retries:
                time.sleep(2**attempt)
        raise RuntimeError(f"Embedding request failed after {max_retries} attempts: {last_error}")
