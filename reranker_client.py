"""DashScope reranking with bounded retries and provider-neutral traces."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_INSTRUCTION = "为医学试题检索能够直接支持正确答案的教材依据，优先定义、诊断标准、机制和治疗原则。"


class RerankError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class RerankResult:
    scores: dict[int, float]
    model: str
    request_id: str
    tokens: int
    elapsed_ms: int


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class DashScopeReranker:
    """Call qwen3-rerank once, retrying one transient provider failure."""

    def __init__(
        self, api_key: str, model: str = "qwen3-rerank", timeout_seconds: float = 30,
        instruction: str = DEFAULT_INSTRUCTION, caller: Callable[..., Any] | None = None,
    ):
        self.api_key = str(api_key or "")
        self.model = str(model or "qwen3-rerank")
        self.timeout_seconds = max(1.0, float(timeout_seconds or 30))
        self.instruction = str(instruction or DEFAULT_INSTRUCTION)
        self._caller = caller

    def _call(self, **kwargs):
        if self._caller is not None:
            return self._caller(**kwargs)
        if not self.api_key:
            raise RerankError("DashScope API key is not configured")
        import dashscope

        # The SDK reads this global only for the duration of the synchronous call.
        dashscope.api_key = self.api_key
        return dashscope.TextReRank.call(**kwargs)

    @staticmethod
    def _parse(response: Any, document_count: int) -> tuple[dict[int, float], str, int]:
        status = int(_value(response, "status_code", 200) or 0)
        if status and status != 200:
            message = str(_value(response, "message", "DashScope rerank failed"))
            raise RerankError(message, status_code=status, retryable=status in RETRYABLE_STATUS)
        output = _value(response, "output", {}) or {}
        rows = _value(output, "results", None)
        if rows is None:
            rows = _value(output, "result", None)
        if not isinstance(rows, list):
            raise RerankError("DashScope rerank response has no results")
        scores: dict[int, float] = {}
        for row in rows:
            try:
                index = int(_value(row, "index"))
                score = float(_value(row, "relevance_score"))
            except (TypeError, ValueError):
                raise RerankError("DashScope rerank result contains an invalid index or score")
            if index < 0 or index >= document_count or index in scores or not 0 <= score <= 1:
                raise RerankError("DashScope rerank result is out of range or duplicated")
            scores[index] = score
        if not scores:
            raise RerankError("DashScope rerank returned an empty result")
        if len(scores) != document_count:
            raise RerankError("DashScope rerank returned an incomplete result")
        request_id = str(_value(response, "request_id", "") or _value(output, "request_id", ""))
        usage = _value(response, "usage", {}) or _value(output, "usage", {}) or {}
        tokens = int(
            _value(usage, "total_tokens", 0)
            or _value(usage, "input_tokens", 0)
            or _value(usage, "tokens", 0)
            or 0
        )
        return scores, request_id, tokens

    def rerank(self, query: str, documents: list[str]) -> RerankResult:
        if not documents:
            raise RerankError("No documents were supplied for reranking")
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._call(
                    model=self.model,
                    query=str(query),
                    documents=[str(item) for item in documents],
                    top_n=len(documents),
                    instruct=self.instruction,
                    timeout=self.timeout_seconds,
                )
                scores, request_id, tokens = self._parse(response, len(documents))
                return RerankResult(
                    scores=scores, model=self.model,
                    request_id=request_id or f"local-{uuid.uuid4()}", tokens=tokens,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                last_error = exc
                status = int(getattr(exc, "status_code", 0) or 0)
                retryable = bool(getattr(exc, "retryable", False)) or status in RETRYABLE_STATUS
                name = type(exc).__name__.casefold()
                retryable = retryable or "timeout" in name or "connection" in name
                if attempt or not retryable:
                    break
        if isinstance(last_error, RerankError):
            raise last_error
        raise RerankError(str(last_error or "DashScope rerank failed")) from last_error
