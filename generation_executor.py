"""Bounded, adaptive request execution for network-bound explanation generation."""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass

from llm_client import ExplanationPackageError, LLMRequestError


class FatalGenerationError(RuntimeError):
    """A provider/account error that should stop the whole generation job."""


class AdaptiveConcurrencyController:
    def __init__(
        self, target: int = 3, adaptive: bool = True, success_window: int = 20,
        *, clock=time.monotonic,
    ):
        self.target = min(16, max(1, int(target)))
        self.adaptive = bool(adaptive)
        self.success_window = max(1, int(success_window))
        self._clock = clock
        self._condition = threading.Condition()
        self._limit = self.target
        self._active = 0
        self._peak = 0
        self._successes = 0
        self._cooldown_until = 0.0
        self._throttle_events = 0

    def acquire(self) -> None:
        with self._condition:
            while True:
                delay = self._cooldown_until - self._clock()
                if delay <= 0 and self._active < self._limit:
                    self._active += 1
                    self._peak = max(self._peak, self._active)
                    return
                self._condition.wait(timeout=max(0.01, min(0.25, delay if delay > 0 else 0.1)))

    def release(self) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()

    def report_success(self) -> None:
        with self._condition:
            self._successes += 1
            if self.adaptive and self._limit < self.target and self._successes >= self.success_window:
                self._limit += 1
                self._successes = 0
            self._condition.notify_all()

    def report_throttle(self, retry_after: float) -> None:
        with self._condition:
            self._throttle_events += 1
            self._successes = 0
            if self.adaptive:
                self._limit = max(1, self._limit // 2)
            self._cooldown_until = max(self._cooldown_until, self._clock() + max(0.0, retry_after))
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "limit": self._limit,
                "active": self._active,
                "peak": self._peak,
                "throttle_events": self._throttle_events,
            }


@dataclass
class GenerationRoute:
    model: str
    thinking: bool
    complexity_score: int
    reasons: list[str]
    max_tokens: int
    fallback_model: str = "deepseek-v4-flash"

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "thinking": self.thinking,
            "complexityScore": self.complexity_score,
            "reasons": list(self.reasons),
            "maxTokens": self.max_tokens,
        }


def route_generation(question: dict, retrieval: dict, settings: dict) -> GenerationRoute:
    """Choose a model locally; retrieval confidence is deliberately not a difficulty signal."""
    flash = str(settings.get("flash_model") or settings.get("model") or "deepseek-v4-flash")
    pro = str(settings.get("hard_model") or "deepseek-v4-pro")
    smart = bool(settings.get("smart_routing", True))
    allow_pro = bool(settings.get("hard_use_pro", True)) and not bool(settings.get("force_flash", False))
    raw = question.get("raw") if isinstance(question.get("raw"), dict) else {}
    qtype = str(raw.get("type") or question.get("type") or "").upper()
    difficulty = str(raw.get("difficulty") or question.get("difficulty") or "").casefold()
    prompt = str(question.get("prompt_text") or raw.get("question") or "")
    answer = str(raw.get("answer") or "")
    score = 0
    reasons: list[str] = []
    if difficulty == "hard" or qtype in {"A3", "A4"}:
        score = 3
        reasons.append("hard_or_a3_a4")
    if qtype == "A2" or re.search(r"(?:患者|病人|主诉|现病史|查体|病例|入院)", prompt):
        score += 2
        reasons.append("case_context")
    if str(retrieval.get("evidence_grade") or "").upper() == "B":
        score += 2
        reasons.append("textbook_reasoning")
    if re.search(r"(?:剂量|浓度|半衰期|清除率|负荷量|维持量|mg/kg|ml/min|t1/2|t₁/₂)", prompt, re.I):
        score += 2
        reasons.append("calculation")
    if qtype in {"MULTIPLE", "X"} and len(set(re.findall(r"[A-E]", answer.upper()))) >= 3:
        score += 1
        reasons.append("multi_answer")
    if len(prompt) >= 320:
        score += 1
        reasons.append("long_stem")
    if not smart:
        thinking = bool(settings.get("default_thinking", False))
        return GenerationRoute(flash, thinking, score, ["smart_routing_off"], 6000 if thinking else 3200)
    if score >= 3 and allow_pro:
        return GenerationRoute(pro, True, score, reasons or ["complex"], 6000, flash)
    if score >= 1:
        return GenerationRoute(flash, True, score, reasons, 6000, flash)
    return GenerationRoute(flash, False, score, reasons or ["simple_direct"], 3200, flash)


@dataclass
class ExplanationResult:
    question: dict
    retrieval: dict
    explanation: str = ""
    package: dict | None = None
    error: str = ""
    error_kind: str = ""
    error_code: str = ""
    route: GenerationRoute | None = None
    attempts: int = 0
    model_fallback: bool = False
    http_status: int = 0
    generation_trace: list[dict] | None = None
    partial_response: str = ""


def _provider_error_code(exc: LLMRequestError) -> str:
    if exc.status_code == 429:
        return "provider_rate_limited"
    if exc.status_code in {500, 502, 503, 504} or (exc.retryable and not exc.status_code):
        return "provider_timeout"
    return "provider_fatal"


def _package_error_code(exc: ExplanationPackageError) -> str:
    return str(getattr(exc, "code", "") or "schema_validation_failed")


def _is_pro_route(route: GenerationRoute | None) -> bool:
    return bool(route and route.model != route.fallback_model and route.complexity_score >= 3)


def generate_with_retry(
    llm_client, question: dict, retrieval: dict,
    controller: AdaptiveConcurrencyController, max_retries: int = 3,
    *, route: GenerationRoute | None = None, pro_gate: threading.Semaphore | None = None,
    base_delay: float = 1.0, sleep=time.sleep, random_value=random.random,
) -> ExplanationResult:
    attempts = max(1, int(max_retries))
    fallback_used = False
    for attempt in range(attempts + 1):
        controller.acquire()
        gate_acquired = False
        try:
            if pro_gate and _is_pro_route(route):
                pro_gate.acquire()
                gate_acquired = True
            if hasattr(llm_client, "generate_explanation_package"):
                try:
                    package = llm_client.generate_explanation_package(
                        question["prompt_text"], retrieval["matched_chunks"],
                        question.get("official_tags", []), route=route,
                    )
                except TypeError as exc:
                    # Keep third-party and test clients written for the pre-1.5 signature usable.
                    if "route" not in str(exc):
                        raise
                    package = llm_client.generate_explanation_package(
                        question["prompt_text"], retrieval["matched_chunks"], question.get("official_tags", [])
                    )
                explanation = package["explanation"]
            else:
                package = None
                explanation = llm_client.generate_explanation(
                    question["prompt_text"], retrieval["matched_chunks"]
                )
        except LLMRequestError as exc:
            if _is_pro_route(route) and exc.status_code in {400, 404, 422}:
                route = GenerationRoute(
                    route.fallback_model, True, route.complexity_score,
                    [*route.reasons, "model_fallback"], 6000, route.fallback_model,
                )
                fallback_used = True
                continue
            if exc.fatal_global:
                raise FatalGenerationError(str(exc)) from exc
            provider_attempt = attempt - (1 if fallback_used else 0)
            if not exc.retryable or provider_attempt + 1 >= attempts:
                return ExplanationResult(
                    question, retrieval, error=str(exc), error_kind="provider",
                    error_code=_provider_error_code(exc), route=route, attempts=attempt + 1,
                    model_fallback=fallback_used,
                    http_status=exc.status_code,
                )
            fallback = base_delay * (2**attempt) * (0.5 + max(0.0, min(1.0, random_value())))
            delay = max(exc.retry_after, fallback)
            controller.report_throttle(delay)
        except ExplanationPackageError as exc:
            return ExplanationResult(
                question, retrieval, error=str(exc), error_kind="validation",
                error_code=_package_error_code(exc), route=route, attempts=attempt + 1,
                model_fallback=fallback_used,
                generation_trace=list(getattr(exc, "request_meta", {}).get("requests") or []),
                partial_response=str(getattr(exc, "partial_content", "") or "")[:12000],
            )
        except Exception as exc:
            return ExplanationResult(
                question, retrieval, error=str(exc), error_kind="unexpected",
                error_code="provider_fatal", route=route, attempts=attempt + 1,
                model_fallback=fallback_used,
            )
        else:
            controller.report_success()
            return ExplanationResult(
                question, retrieval, explanation=explanation, package=package,
                route=route, attempts=attempt + 1, model_fallback=fallback_used,
            )
        finally:
            if gate_acquired:
                pro_gate.release()
            controller.release()
        # The shared controller enforces the cooldown before the next acquire.
        sleep(0)
    return ExplanationResult(
        question, retrieval, error="解析生成失败", error_kind="provider",
        error_code="provider_fatal", route=route, attempts=attempts,
        model_fallback=fallback_used,
    )


def generate_general_with_retry(
    llm_client, question: dict, controller: AdaptiveConcurrencyController,
    max_retries: int = 3, *, base_delay: float = 1.0,
    sleep=time.sleep, random_value=random.random,
) -> ExplanationResult:
    """Generate one concise general-knowledge package with the same adaptive limits."""
    attempts = max(1, int(max_retries))
    for attempt in range(attempts):
        controller.acquire()
        try:
            if hasattr(llm_client, "generate_fallback_package"):
                package = llm_client.generate_fallback_package(question["prompt_text"])
                explanation = package["explanation"]
            else:
                package = None
                explanation = llm_client.generate_fallback_explanation(question["prompt_text"])
        except LLMRequestError as exc:
            if exc.fatal_global:
                raise FatalGenerationError(str(exc)) from exc
            if not exc.retryable or attempt + 1 >= attempts:
                return ExplanationResult(
                    question, {}, error=str(exc), error_kind="provider",
                    error_code=_provider_error_code(exc), attempts=attempt + 1,
                    http_status=exc.status_code,
                )
            fallback = base_delay * (2**attempt) * (0.5 + max(0.0, min(1.0, random_value())))
            controller.report_throttle(max(exc.retry_after, fallback))
        except ExplanationPackageError as exc:
            return ExplanationResult(
                question, {}, error=str(exc), error_kind="validation",
                error_code=_package_error_code(exc), attempts=attempt + 1,
            )
        except Exception as exc:
            return ExplanationResult(
                question, {}, error=str(exc), error_kind="unexpected",
                error_code="provider_fatal", attempts=attempt + 1,
            )
        else:
            controller.report_success()
            return ExplanationResult(
                question, {}, explanation=explanation, package=package, attempts=attempt + 1,
            )
        finally:
            controller.release()
        sleep(0)
    return ExplanationResult(
        question, {}, error="通识解析生成失败", error_kind="provider",
        error_code="provider_fatal", attempts=attempts,
    )
